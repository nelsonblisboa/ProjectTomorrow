"""MAG Tracker web application: pages, session authentication and API."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .scraper import executar_coleta, consultar_processo_real

BASE = Path(__file__).resolve().parent
SECRET = os.environ.get("MAG_SECRET") or secrets.token_urlsafe(32)
COOKIE = "mag_session"
CSRF_COOKIE = "mag_csrf"
ALERTA_DIAS = int(os.environ.get("MAG_ALERTA_DIAS", "30"))
app = FastAPI(title="MAG Tracker", version="0.5.0")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))
SCRAPER_ENABLED = os.environ.get("MAG_ENABLE_REAL_SCRAPER", "0").strip().lower() in {"1", "true", "yes"}
ALLOW_DEMO_DATA = os.environ.get("MAG_ALLOW_DEMO_DATA", "0").strip().lower() in {"1", "true", "yes"}
_scrape_lock = threading.Lock()
_last_scrape_by_process: dict[str, float] = {}


def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _modo_demo() -> bool:
    """Demo auth is opt-in; enabling it does not generate demo process records."""
    return os.environ.get("MAG_DEMO", "0").strip().lower() in {"1", "true", "yes"}


def _cookie_secure() -> bool:
    configured = os.environ.get("MAG_COOKIE_SECURE")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes"}
    return not _modo_demo()


def _set_csrf_cookie(response, token: str) -> None:
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=_cookie_secure(),
                        samesite="strict", max_age=3600)


def _check_csrf(request: Request, submitted: str) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if not cookie_token or not hmac.compare_digest(cookie_token, submitted):
        raise HTTPException(403, "Token de segurança inválido. Atualize a página e tente novamente.")


def _issue_template(request: Request, name: str, context: dict, status_code: int = 200):
    token = context.get("csrf_token") or secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request=request, name=name, context={**context, "csrf_token": token},
        status_code=status_code,
    )
    _set_csrf_cookie(response, token)
    return response


def _valid_cpf(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for length in (9, 10):
        total = sum(int(digits[i]) * (length + 1 - i) for i in range(length))
        check = (total * 10) % 11
        if check == 10:
            check = 0
        if check != int(digits[length]):
            return False
    return True


def _valid_process_number(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9./-]{1,30}", value.strip()))


def _validate_process_fields(numero: str, cpf: str, nascimento: str, data_solicitacao: str) -> None:
    if not _valid_process_number(numero):
        raise HTTPException(422, "Informe um número de processo válido (até 30 letras, números, ponto, barra ou hífen).")
    if cpf.strip() and not _valid_cpf(cpf):
        raise HTTPException(422, "CPF inválido. Confira os dígitos e tente novamente.")
    for field, value in (("nascimento", nascimento), ("data da solicitação", data_solicitacao)):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise HTTPException(422, f"Data de {field} inválida.") from exc


def set_session(resp: RedirectResponse, user: dict) -> None:
    import base64
    payload = base64.urlsafe_b64encode(json.dumps(user).encode()).decode()
    resp.set_cookie(COOKIE, f"{payload}.{_sign(payload)}", httponly=True,
                    secure=_cookie_secure(), samesite="lax", max_age=8 * 3600)


def get_session(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE)
    if not raw or "." not in raw:
        return None
    payload, signature = raw.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        import base64
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def require_user(request: Request) -> dict:
    user = get_session(request)
    if not user:
        raise HTTPException(303, headers={"Location": "/login"})
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user.get("perfil") != "admin":
        raise HTTPException(403, "Área restrita a administradores")
    return user


@app.on_event("startup")
def startup() -> None:
    demo = _modo_demo()
    if not demo:
        if len(os.environ.get("MAG_SECRET", "")) < 32:
            raise RuntimeError("Defina MAG_SECRET com pelo menos 32 caracteres fora do modo demo.")
        if not os.environ.get("MAG_FERNET_KEY"):
            raise RuntimeError("Defina MAG_FERNET_KEY fora do modo demo para criptografia em repouso.")
        if len(os.environ.get("MAG_BLIND_SALT", "")) < 32:
            raise RuntimeError("Defina MAG_BLIND_SALT com pelo menos 32 caracteres fora do modo demo.")

    db.init_db()
    with db.get_db() as conn:
        user_count = conn.execute("SELECT COUNT(*) c FROM usuarios").fetchone()["c"]
    if user_count == 0 and demo:
        db.criar_usuario("admin@magtracker.local", "admin123", "Administrador", "admin")
        db.criar_usuario("consulta@magtracker.local", "consulta123", "Usuário Consulta", "consulta")
    elif user_count == 0:
        email = os.environ.get("MAG_ADMIN_EMAIL", "").strip().lower()
        password = os.environ.get("MAG_ADMIN_PASSWORD", "")
        if not email or len(password) < 12:
            raise RuntimeError(
                "Banco sem usuários: configure MAG_ADMIN_EMAIL e MAG_ADMIN_PASSWORD "
                "(mínimo 12 caracteres) para criar o administrador inicial."
            )
        db.criar_usuario(email, password, "Administrador", "admin")
    # No scheduled demo scraper: real queries are initiated individually by an admin.


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    if get_session(request):
        return RedirectResponse("/", status_code=302)
    return _issue_template(request, "login.html", {"erro": None, "demo": _modo_demo()})


@app.post("/login")
def do_login(request: Request, email: str = Form(...), senha: str = Form(...),
             csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    user = db.autenticar(email, senha)
    ip = request.client.host if request.client else ""
    if not user:
        db.log_acesso(email, "login_falhou", "credenciais inválidas", ip)
        return _issue_template(request, "login.html",
                               {"erro": "Credenciais inválidas", "demo": _modo_demo(),
                                "csrf_token": csrf_token}, 401)
    db.log_acesso(user["email"], "login", f"perfil={user['perfil']}", ip)
    response = RedirectResponse("/", status_code=303)
    set_session(response, user)
    response.delete_cookie(CSRF_COOKIE)
    return response


@app.get("/logout")
def do_logout(request: Request):
    user = get_session(request)
    if user:
        db.log_acesso(user["email"], "logout")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request, user: dict = Depends(require_user)):
    stats = db.dashboard_stats(ALERTA_DIAS)
    processes = db.listar_processos(limite=5)
    return _issue_template(request, "dashboard.html",
                           {"user": user, "stats": stats, "recentes": processes,
                            "alerta_dias": ALERTA_DIAS})


@app.get("/processos", response_class=HTMLResponse)
def page_processos(request: Request, status: str | None = None, q: str | None = None,
                   user: dict = Depends(require_user)):
    processes = db.listar_processos(status=status, busca=q)
    statuses = ["Em análise", "Pendente", "Concluído", "Aguardando documentação",
                "Deferido", "Indeferido"]
    db.log_acesso(user["email"], "listagem", f"status={status} q={q}")
    return templates.TemplateResponse(
        request=request, name="processos.html",
        context={"user": user, "processos": processes, "statuses": statuses,
                 "status_sel": status or "", "q": q or "", "alerta_dias": ALERTA_DIAS},
    )


@app.get("/processos/novo", response_class=HTMLResponse)
def page_novo_processo(request: Request, user: dict = Depends(require_admin)):
    return _issue_template(request, "processo_novo.html",
                           {"user": user, "demo": _modo_demo(),
                            "scraper_enabled": SCRAPER_ENABLED, "resultado": None})


@app.post("/processos/novo")
def criar_processo_manual(
    request: Request,
    numero_processo: str = Form(...),
    status: str = Form(...),
    beneficio_solicitado: str = Form(""),
    codigo_produtor: str = Form(""),
    cliente_nome: str = Form(""),
    cpf: str = Form(""),
    data_nascimento: str = Form(""),
    data_solicitacao: str = Form(""),
    comentarios: str = Form(""),
    csrf_token: str = Form(...),
    user: dict = Depends(require_admin),
):
    _check_csrf(request, csrf_token)
    _validate_process_fields(numero_processo, cpf, data_nascimento, data_solicitacao)
    allowed_status = {"Em análise", "Pendente", "Concluído", "Aguardando documentação",
                      "Deferido", "Indeferido"}
    if status not in allowed_status:
        raise HTTPException(422, "Status inválido.")
    data = {"numero_processo": numero_processo.strip(), "status": status,
            "beneficio_solicitado": beneficio_solicitado.strip() or None,
            "codigo_produtor": codigo_produtor.strip() or None,
            "cliente_nome": cliente_nome.strip() or None,
            "cpf": cpf.strip() or None,
            "data_nascimento": data_nascimento or None,
            "data_solicitacao": data_solicitacao or None,
            "ultima_atualizacao": date.today().isoformat(),
            "dias_corridos": ((date.today() - date.fromisoformat(data_solicitacao)).days
                              if data_solicitacao else None),
            "comentarios": comentarios.strip() or None}
    _, result = db.upsert_processo(data)
    db.log_acesso(user["email"], "processo_manual", f"resultado={result}")
    return RedirectResponse(f"/processos/{data['numero_processo']}", status_code=303)


@app.post("/processos/raspar", response_class=HTMLResponse)
def testar_scraper(
    request: Request,
    numero_processo: str = Form(...),
    cpf: str = Form(""),
    data_nascimento: str = Form(""),
    csrf_token: str = Form(...),
    user: dict = Depends(require_admin),
):
    _check_csrf(request, csrf_token)
    if not SCRAPER_ENABLED:
        raise HTTPException(403, "Consulta real desativada. Habilite MAG_ENABLE_REAL_SCRAPER=1 após confirmar autorização de acesso.")
    if _modo_demo():
        raise HTTPException(409, "Desative MAG_DEMO antes de consultar o portal real.")
    _validate_process_fields(numero_processo, cpf, data_nascimento, "")
    key = numero_processo.strip()
    now = time.monotonic()
    with _scrape_lock:
        last = _last_scrape_by_process.get(key, 0)
        if now - last < 60:
            raise HTTPException(429, "Aguarde 60 segundos antes de consultar novamente este processo.")
        _last_scrape_by_process[key] = now

    result = consultar_processo_real(key, cpf or None, data_nascimento or None)
    if result.get("ok") and result.get("dados"):
        data = result["dados"]
        meaningful = any(data.get(field) for field in
                         ("status", "cliente_nome", "beneficio_solicitado", "comentarios",
                          "ultima_atualizacao", "data_solicitacao"))
        if meaningful:
            data["numero_processo"] = key
            db.upsert_processo(data)
            message = "Consulta concluída; dados retornados foram atualizados no sistema."
        else:
            message = "O portal respondeu, mas não foi possível reconhecer dados do processo. Nada foi salvo."
    elif result.get("bloqueado"):
        message = "O portal recusou a consulta ou exige uma sessão/autorização válida. Nada foi salvo."
    else:
        message = "Não foi possível concluir a consulta. Confira os dados e a autorização de acesso; nada foi salvo."
    db.log_acesso(user["email"], "scraper_teste",
                  f"resultado={'ok' if result.get('ok') else 'falha'}")
    return _issue_template(request, "processo_novo.html",
                           {"user": user, "demo": False, "scraper_enabled": True,
                            "resultado": message, "numero_teste": key})


@app.get("/processos/{numero}", response_class=HTMLResponse)
def page_processo(request: Request, numero: str, user: dict = Depends(require_user)):
    process, history = db.obter_processo(numero)
    if not process:
        raise HTTPException(404, "Processo não encontrado")
    db.log_acesso(user["email"], "detalhe", f"processo={numero}",
                  request.client.host if request.client else "")
    return templates.TemplateResponse(
        request=request, name="detalhe.html",
        context={"user": user, "proc": process, "hist": history,
                 "alerta_dias": ALERTA_DIAS},
    )


@app.get("/auditoria", response_class=HTMLResponse)
def page_auditoria(request: Request, user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        logs = conn.execute("SELECT * FROM logs_acesso ORDER BY id DESC LIMIT 200").fetchall()
        executions = conn.execute("SELECT * FROM execucoes_scraper ORDER BY id DESC LIMIT 50").fetchall()
    return templates.TemplateResponse(
        request=request, name="auditoria.html",
        context={"user": user, "logs": [dict(row) for row in logs],
                 "execs": [dict(row) for row in executions]},
    )


@app.get("/api/processos")
def api_processos(status: str | None = None, q: str | None = None,
                  user: dict = Depends(require_user)):
    return JSONResponse(db.listar_processos(status=status, busca=q))


@app.post("/api/coleta/run")
def api_coleta_now(request: Request, csrf_token: str = Form(...),
                   user: dict = Depends(require_admin)):
    _check_csrf(request, csrf_token)
    if not (ALLOW_DEMO_DATA and _modo_demo()):
        raise HTTPException(403, "Geração de dados fictícios desativada. Cadastre dados reais ou habilite MAG_ALLOW_DEMO_DATA=1 somente em ambiente de teste.")
    return JSONResponse(executar_coleta(demo=True))


@app.get("/health")
def health():
    return {"ok": True, "agora": datetime.now(timezone.utc).isoformat(),
            "scheduler": False, "demo": _modo_demo(),
            "real_scraper_enabled": SCRAPER_ENABLED,
            "demo_data_enabled": ALLOW_DEMO_DATA and _modo_demo()}
