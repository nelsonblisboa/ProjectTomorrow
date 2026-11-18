"""Módulo de Coleta Automatizada (Scraper) — MAG Tracker Fase 2.

Fluxo:
1. Abre https://mag.com.br/acompanhar-beneficio/ com Playwright (Chromium headless).
2. Localiza o iframe do widget `widgets.mag.com.br/widget-timeline-beneficio-*`.
3. Preenche os campos de consulta (nº processo / CPF + data de nascimento) e envia.
4. Extrai a timeline de acompanhamento e normaliza os campos.
5. Persiste via app.db.upsert_processo (detecção de mudança por hash_conteudo).

Resiliência:
- Retries com backoff exponencial para falhas de rede.
- Seletor adaptativo: procura labels/textos em vez de IDs fixos.
- Detecta bloqueio ("ERRO 403-BK") e registra status='bloqueado' para auditoria.
- Screenshot de evidência em caso de falha crítica (fallback p/ OCR futuro).

IMPORTANTE (estado atual): o widget público exige um token de sessão gerado pelo
portal; consultas anônimas retornam "ERRO 403-BK". Enquanto a MAG não fornecer
credenciais/parâmetros válidos, use --demo para popular o banco com dados
fictícios e desenvolver as telas (nenhum dado real é coletado na demo).
"""
from __future__ import annotations

import json
import logging
import random
import re
import string
import time
from datetime import datetime, timezone, timedelta

from . import db

log = logging.getLogger("mag.scraper")

URL_MAG = "https://mag.com.br/acompanhar-beneficio/"
INTERVALO_MINUTOS = 30


# ---------------------------------------------------------------- normalização
def _norm_data(valor: str | None) -> str | None:
    """Normaliza dd/mm/aaaa ou variações para ISO yyyy-mm-dd."""
    if not valor:
        return None
    m = re.search(r"(\d{2})[/\-.](\d{2})[/\-.](\d{4})", valor)
    if m:
        d, mo, a = m.groups()
        try:
            return f"{a}-{mo}-{d}"
        except Exception:
            return None
    return None


def _norm_cpf(valor: str | None) -> str | None:
    if not valor:
        return None
    digits = re.sub(r"\D", "", valor)
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return valor.strip()


def _parse_timeline(texto: str) -> dict:
    """Extrai campos da timeline em texto puro (layout da planilha MAG)."""
    def busca(label: str) -> str | None:
        m = re.search(rf"{label}\s*[:\-]?\s*(.+)", texto, re.IGNORECASE)
        return m.group(1).strip() if m else None

    dias = busca("dias corridos") or ""
    m_dias = re.search(r"\d+", dias)
    status_raw = (busca("status") or "").strip()
    return {
        "numero_processo": (busca("processo") or busca("nº") or "").split()[0] if busca("processo") else None,
        "beneficio_solicitado": busca("benefício solicitado") or busca("beneficio solicitado"),
        "codigo_produtor": busca("código do produtor") or busca("codigo do produtor"),
        "cliente_nome": busca("cliente"),
        "cpf": _norm_cpf(busca("cpf")),
        "data_nascimento": _norm_data(busca("data de nascimento")),
        "data_solicitacao": _norm_data(busca("data da solicitação")),
        "ultima_atualizacao": _norm_data(busca("última atualização") or busca("ultima atualizacao")),
        "dias_corridos": int(m_dias.group()) if m_dias else None,
        "status": status_raw.title() if status_raw else None,
        "comentarios": busca("pendência") or busca("comentário") or busca("comentario"),
    }


# ---------------------------------------------------------------- scraping real
def consultar_processo_real(numero_processo: str, cpf: str | None = None,
                            data_nascimento: str | None = None,
                            headless: bool = True) -> dict:
    """Consulta um processo no portal da MAG via Playwright.

    Retorna dict com 'ok', 'dados' ou 'erro'. Nunca lança para o chamador.
    """
    from playwright.sync_api import sync_playwright

    tentativa = 0
    max_tentativas = 4
    ultimo_erro = None

    while tentativa < max_tentativas:
        tentativa += 1
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                ctx = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                )
                page = ctx.new_page()
                page.goto(URL_MAG, timeout=60_000, wait_until="domcontentloaded")
                page.wait_for_timeout(5_000)

                widget = None
                for f in page.frames:
                    if "widget-timeline" in f.url:
                        widget = f
                        break
                if widget is None:
                    raise RuntimeError("iframe do widget timeline não encontrado")

                texto_widget = widget.evaluate(
                    "() => document.body ? document.body.innerText : ''") or ""
                if "ERRO 403" in texto_widget or "403-BK" in texto_widget:
                    browser.close()
                    return {"ok": False, "bloqueado": True,
                            "erro": "Widget respondeu ERRO 403-BK (exige sessão/token do portal)"}

                # Estratégia adaptativa: preenche inputs por rótulo/placeholder
                campos = {"processo": numero_processo, "cpf": cpf,
                          "nascimento": data_nascimento}
                inputs = widget.query_selector_all("input:not([type=hidden])")
                for inp in inputs:
                    hint = " ".join(filter(None, [
                        inp.get_attribute("placeholder"), inp.get_attribute("name"),
                        inp.get_attribute("aria-label")])).lower()
                    for chave, val in campos.items():
                        if val and any(k in hint for k in (chave[:4], chave[:5])):
                            inp.fill(val)
                            break
                btn = widget.query_selector("button[type=submit], button:has-text('Consultar'), button:has-text('Buscar')")
                if btn:
                    btn.click()
                    page.wait_for_timeout(6_000)
                    texto_widget = widget.evaluate("() => document.body ? document.body.innerText : ''") or ""

                dados = _parse_timeline(texto_widget)
                if not dados.get("numero_processo"):
                    dados["numero_processo"] = numero_processo
                browser.close()
                return {"ok": True, "dados": dados}

        except Exception as e:  # noqa: BLE001
            ultimo_erro = str(e)
            espera = min(2 ** tentativa + random.random(), 30)
            log.warning("Tentativa %d falhou (%s); aguardando %.1fs", tentativa, e, espera)
            time.sleep(espera)

    return {"ok": False, "erro": f"Falha após {max_tentativas} tentativas: {ultimo_erro}"}


# ---------------------------------------------------------------- dados demo
_STATUS = ["Em análise", "Pendente", "Concluído", "Aguardando documentação", "Deferido", "Indeferido"]
_BENEFICIOS = ["Renda por invalidez", "Morte / auxílio funeral", "Diária por incapacidade temporária",
               "Doença grave", "Amparo familiar"]
_NOMES = ["Ana Paula Souza", "Carlos Eduardo Lima", "Maria Fernanda Alves", "João Batista Rocha",
          "Juliana Mendes Castro", "Roberto Silva Nunes", "Patrícia Gomes Dias", "Marcos Vinícius Prado"]


def _cpf_ficticio() -> str:
    d = "".join(random.choice(string.digits) for _ in range(11))
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


def gerar_demo(n: int = 12) -> list[dict]:
    """Gera processos FICTÍCIOS para desenvolvimento das telas (não raspa o site)."""
    hoje = datetime.now(timezone.utc).date()
    out = []
    for i in range(n):
        solicitacao = hoje - timedelta(days=random.randint(5, 120))
        atualizacao = solicitacao + timedelta(days=random.randint(1, (hoje - solicitacao).days))
        status = random.choice(_STATUS)
        pendente = status in ("Pendente", "Aguardando documentação")
        out.append({
            "numero_processo": f"SIM{random.randint(100000, 999999)}",
            "beneficio_solicitado": random.choice(_BENEFICIOS),
            "codigo_produtor": f"P{random.randint(1000, 9999)}",
            "cliente_nome": random.choice(_NOMES),
            "cpf": _cpf_ficticio(),
            "data_nascimento": f"19{random.randint(50, 99)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
            "data_solicitacao": solicitacao.isoformat(),
            "ultima_atualizacao": atualizacao.isoformat(),
            "dias_corridos": (hoje - solicitacao).days,
            "status": status,
            "comentarios": ("Pendência: falta laudo médico atualizado e comprovante de renda."
                            if pendente else ""),
        })
    return out


# ---------------------------------------------------------------- rotina agendada
def executar_coleta(processos_para_consultar: list[dict] | None = None,
                    demo: bool = False) -> dict:
    """Rotina principal chamada a cada 30 minutos pelo APScheduler."""
    iniciado = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.init_db()
    novos = alterados = vistos = 0
    status_exec = "ok"
    mensagem = ""

    try:
        if demo:
            lista = gerar_demo()
        else:
            lista = []
            for q in (processos_para_consultar or []):
                res = consultar_processo_real(q["numero_processo"], q.get("cpf"),
                                              q.get("data_nascimento"))
                if res.get("bloqueado"):
                    status_exec = "bloqueado"
                    mensagem = res["erro"]
                    continue
                if res.get("ok"):
                    lista.append(res["dados"])
                else:
                    mensagem = res.get("erro", "")

        for d in lista:
            if not d.get("numero_processo"):
                continue
            vistos += 1
            _, tipo = db.upsert_processo(d)
            if tipo == "novo":
                novos += 1
            elif tipo == "alterado":
                alterados += 1
    except Exception as e:  # noqa: BLE001
        status_exec = "erro"
        mensagem = str(e)
        log.exception("Coleta falhou")

    db.registrar_execucao(iniciado, status_exec, vistos, novos, alterados, mensagem)
    resultado = {"status": status_exec, "vistos": vistos, "novos": novos,
                 "alterados": alterados, "mensagem": mensagem}
    log.info("Coleta finalizada: %s", json.dumps(resultado))
    return resultado
