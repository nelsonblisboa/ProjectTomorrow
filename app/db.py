"""Módulo de persistência do MAG Tracker (Fase 2 — MVP com SQLite).

Schema conforme ARQUITETURA.md:
- processos            : estado atual de cada processo consultado
- historico_processos  : snapshot SCD-2 a cada mudança detectada (hash_conteudo)
- usuarios             : autenticação local (senha com hash pbkdf2)
- logs_acesso          : trilha de auditoria (LGPD art. 37)

CPF e nome do cliente são armazenados criptografados em repouso (Fernet),
com um campo hash determinístico (blind index) para permitir busca por CPF
sem expor o dado em claro no banco.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("MAG_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DB_PATH = BASE_DIR / "mag_tracker.db"

# ---------------------------------------------------------------- crypto LGPD
_KEY_FILE = BASE_DIR / ".fernet.key"


def _load_key() -> bytes | None:
    """Carrega/gera a chave de criptografia em repouso (fora do controle de versão)."""
    env = os.environ.get("MAG_FERNET_KEY")
    if env:
        return env.encode()
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not _KEY_FILE.exists():
        from cryptography.fernet import Fernet
        _KEY_FILE.write_bytes(Fernet.generate_key())
        os.chmod(_KEY_FILE, 0o600)
    return _KEY_FILE.read_bytes()


try:
    from cryptography.fernet import Fernet
    _fernet = Fernet(_load_key())
except Exception:  # pragma: no cover - fallback p/ ambientes sem cryptography
    _fernet = None


def enc(value: str | None) -> str | None:
    if value is None:
        return None
    if _fernet is None:
        return value
    return _fernet.encrypt(value.encode()).decode()


def dec(token: str | None) -> str | None:
    if token is None:
        return None
    if _fernet is None:
        return token
    try:
        return _fernet.decrypt(token.encode()).decode()
    except Exception:
        return None


def blind_index(value: str | None) -> str | None:
    """Hash determinístico para buscar por CPF sem descriptografar."""
    if not value:
        return None
    salt = os.environ.get("MAG_BLIND_SALT", "mag-tracker-dev-salt")
    norm = re.sub(r"\D", "", value)
    return hashlib.sha256(f"{salt}:{norm}".encode()).hexdigest()


# ---------------------------------------------------------------- schema
_SCHEMA = """
CREATE TABLE IF NOT EXISTS processos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_processo   TEXT NOT NULL UNIQUE,
    beneficio_solicitado TEXT,
    codigo_produtor   TEXT,
    cliente_nome_enc  TEXT,
    cpf_enc           TEXT,
    cpf_hash          TEXT,
    data_nascimento   TEXT,
    data_solicitacao  TEXT,
    ultima_atualizacao TEXT,
    dias_corridos     INTEGER,
    status            TEXT,
    comentarios       TEXT,
    hash_conteudo     TEXT,
    primeira_consulta TEXT,
    ultima_coleta     TEXT
);
CREATE INDEX IF NOT EXISTS idx_processos_cpf    ON processos(cpf_hash);
CREATE INDEX IF NOT EXISTS idx_processos_status ON processos(status);

CREATE TABLE IF NOT EXISTS historico_processos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    processo_id   INTEGER NOT NULL REFERENCES processos(id),
    numero_processo TEXT,
    status        TEXT,
    comentarios   TEXT,
    ultima_atualizacao TEXT,
    dias_corridos INTEGER,
    hash_conteudo TEXT,
    capturado_em  TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_processo ON historico_processos(processo_id);

CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    nome  TEXT,
    senha_hash TEXT NOT NULL,
    perfil TEXT NOT NULL DEFAULT 'consulta',   -- admin | consulta
    criado_em TEXT
);

CREATE TABLE IF NOT EXISTS logs_acesso (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_email TEXT,
    acao TEXT,
    detalhe TEXT,
    ip TEXT,
    quando TEXT
);

CREATE TABLE IF NOT EXISTS execucoes_scraper (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iniciado_em TEXT,
    finalizado_em TEXT,
    status TEXT,              -- ok | erro | bloqueado
    processos_vistos INTEGER DEFAULT 0,
    novos INTEGER DEFAULT 0,
    alterados INTEGER DEFAULT 0,
    mensagem TEXT
);
"""


@contextmanager
def get_db():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------- auth helpers
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return dk.hex() == dk_hex


def criar_usuario(email: str, senha: str, nome: str = "", perfil: str = "consulta") -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO usuarios (email, nome, senha_hash, perfil, criado_em) VALUES (?,?,?,?,?)",
            (email.lower().strip(), nome, hash_password(senha), perfil, _now()),
        )
        return cur.lastrowid


def autenticar(email: str, senha: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM usuarios WHERE email = ?", (email.lower().strip(),)).fetchone()
    if row and verify_password(senha, row["senha_hash"]):
        d = dict(row)
        d.pop("senha_hash", None)
        return d
    return None


def log_acesso(usuario_email: str, acao: str, detalhe: str = "", ip: str = "") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO logs_acesso (usuario_email, acao, detalhe, ip, quando) VALUES (?,?,?,?,?)",
            (usuario_email, acao, detalhe, ip, _now()),
        )


# ---------------------------------------------------------------- CRUD processos
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_processo(d: dict) -> tuple[int, str]:
    """Insere ou atualiza; retorna (processo_id, 'novo'|'alterado'|'igual')."""
    conteudo = "|".join(str(d.get(k, "")) for k in ("status", "comentarios", "ultima_atualizacao", "dias_corridos"))
    novo_hash = hashlib.sha256(conteudo.encode()).hexdigest()
    with get_db() as conn:
        old = conn.execute("SELECT id, hash_conteudo FROM processos WHERE numero_processo = ?",
                           (d["numero_processo"],)).fetchone()
        if old is None:
            cur = conn.execute(
                """INSERT INTO processos (numero_processo, beneficio_solicitado, codigo_produtor,
                       cliente_nome_enc, cpf_enc, cpf_hash, data_nascimento, data_solicitacao,
                       ultima_atualizacao, dias_corridos, status, comentarios, hash_conteudo,
                       primeira_consulta, ultima_coleta)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["numero_processo"], d.get("beneficio_solicitado"), d.get("codigo_produtor"),
                 enc(d.get("cliente_nome")), enc(d.get("cpf")), blind_index(d.get("cpf")),
                 d.get("data_nascimento"), d.get("data_solicitacao"), d.get("ultima_atualizacao"),
                 d.get("dias_corridos"), d.get("status"), d.get("comentarios"),
                 novo_hash, _now(), _now()),
            )
            pid = cur.lastrowid
            _add_historico(conn, pid, d, novo_hash)
            return pid, "novo"
        pid = old["id"]
        if old["hash_conteudo"] == novo_hash:
            conn.execute("UPDATE processos SET ultima_coleta = ? WHERE id = ?", (_now(), pid))
            return pid, "igual"
        conn.execute(
            """UPDATE processos SET beneficio_solicitado=?, codigo_produtor=?, cliente_nome_enc=?,
               cpf_enc=?, cpf_hash=?, data_nascimento=?, data_solicitacao=?, ultima_atualizacao=?,
               dias_corridos=?, status=?, comentarios=?, hash_conteudo=?, ultima_coleta=? WHERE id=?""",
            (d.get("beneficio_solicitado"), d.get("codigo_produtor"), enc(d.get("cliente_nome")),
             enc(d.get("cpf")), blind_index(d.get("cpf")), d.get("data_nascimento"),
             d.get("data_solicitacao"), d.get("ultima_atualizacao"), d.get("dias_corridos"),
             d.get("status"), d.get("comentarios"), novo_hash, _now(), pid),
        )
        _add_historico(conn, pid, d, novo_hash)
        return pid, "alterado"


def _add_historico(conn, pid, d, h):
    conn.execute(
        """INSERT INTO historico_processos (processo_id, numero_processo, status, comentarios,
            ultima_atualizacao, dias_corridos, hash_conteudo, capturado_em)
           VALUES (?,?,?,?,?,?,?,?)""",
        (pid, d["numero_processo"], d.get("status"), d.get("comentarios"),
         d.get("ultima_atualizacao"), d.get("dias_corridos"), h, _now()),
    )


def listar_processos(status=None, busca=None, limite=500):
    q = "SELECT * FROM processos"
    cond, params = [], []
    if status:
        cond.append("status = ?"); params.append(status)
    if busca:
        digits = re.sub(r"\D", "", busca or "")
        ors = ["numero_processo LIKE ?", "status LIKE ?", "beneficio_solicitado LIKE ?", "codigo_produtor LIKE ?"]
        vals = [f"%{busca}%"] * 4
        if len(digits) >= 3:
            ors.append("cpf_hash = ?"); vals.append(blind_index(busca))
        cond.append("(" + " OR ".join(ors) + ")"); params += vals
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += (" ORDER BY CASE WHEN status IN ('Pendente','Em análise') THEN 0 ELSE 1 END,"
          " dias_corridos DESC LIMIT ?")
    params.append(limite)
    with get_db() as conn:
        rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["cliente_nome"] = dec(item.pop("cliente_nome_enc"))
        cpf = dec(item.pop("cpf_enc"))
        if cpf:
            c = re.sub(r"\D", "", cpf)
            item["cpf"] = (c[:3] + "•••.•••.•••-" + c[-2:]) if len(c) >= 11 else cpf
        else:
            item["cpf"] = None
        item.pop("cpf_hash", None)
        item.pop("hash_conteudo", None)
        out.append(item)
    return out


def obter_processo(numero: str):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM processos WHERE numero_processo = ?", (numero,)).fetchone()
        if not r:
            return None, []
        hist = conn.execute(
            "SELECT * FROM historico_processos WHERE processo_id = ? ORDER BY capturado_em DESC",
            (r["id"],)).fetchall()
    item = dict(r)
    item["cliente_nome"] = dec(item.pop("cliente_nome_enc"))
    item["cpf"] = dec(item.pop("cpf_enc"))
    item.pop("cpf_hash", None)
    return item, [dict(h) for h in hist]


def dashboard_stats(limite_dias_alerta: int = 30):
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM processos").fetchone()["c"]
        pendentes = conn.execute(
            "SELECT COUNT(*) c FROM processos WHERE status LIKE '%pendente%'").fetchone()["c"]
        em_analise = conn.execute(
            "SELECT COUNT(*) c FROM processos WHERE status LIKE '%análise%' OR status LIKE '%analise%'").fetchone()["c"]
        concluidos_7d = conn.execute(
            """SELECT COUNT(*) c FROM processos p WHERE status LIKE '%conclu%'
               AND EXISTS (SELECT 1 FROM historico_processos h WHERE h.processo_id = p.id
                           AND h.capturado_em >= datetime('now','-7 days'))""").fetchone()["c"]
        alerta = conn.execute(
            "SELECT COUNT(*) c FROM processos WHERE status LIKE '%pendente%' AND dias_corridos > ?",
            (limite_dias_alerta,)).fetchone()["c"]
        ultima = conn.execute(
            "SELECT finalizado_em, status FROM execucoes_scraper ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "total": total, "pendentes": pendentes, "em_analise": em_analise,
        "concluidos_7d": concluidos_7d, "alerta_critico": alerta,
        "ultima_execucao": dict(ultima) if ultima else None,
    }


def registrar_execucao(iniciado_em, status, vistos, novos, alterados, mensagem=""):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO execucoes_scraper (iniciado_em, finalizado_em, status,
               processos_vistos, novos, alterados, mensagem) VALUES (?,?,?,?,?,?,?)""",
            (iniciado_em, _now(), status, vistos, novos, alterados, mensagem[:500]),
        )
