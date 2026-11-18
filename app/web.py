"""Interface Web do MAG Tracker — FastAPI + Jinja2 (Fase 2 MVP).

Telas: Login, Dashboard de pendências, Tabela de consulta com filtros,
Detalhamento com timeline histórica e Auditoria (admin).
Autenticação por sessão assinada (cookie HttpOnly/SameSite=Lax).
Agendador APScheduler embutido: coleta a cada 30 minutos.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .scraper import executar_coleta, INTERVALO_MINUTOS

BASE = Path(__file__).resolve().parent
SECRET = os.environ.get("MAG_SECRET", secrets.token_hex(16))
COOKIE = "mag_session"
ALERTA_DIAS = int(os.environ.get("MAG_ALERTA_DIAS", "30"))

app = FastAPI(title="MAG Tracker", version="0.2.0")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# ---------------------------------------------------------------- sessão
def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def set_session(resp: RedirectResponse, user: dict) -> None:
    payload = base64_json(user)
    resp.set_cookie(COOKIE, f"{payload}.{_sign(payload)}", httponly=True,
                    samesite="lax", max_age=8 * 3600)


def base64_json(d: dict) -> str:
    import base64
    return base64.b64encode(json.dumps(d).encode()).decode()


def get_session(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE)
    if not raw or "." not in raw:
        return None
    payload, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        import base64
        return json.loads(base64.b64decode(payload))
    except Exception:
        return None


def require_user(request: Request) -> dict:
    u = get_session(request)
    if not u:
        raise HTTPException(302, headers={"Location": "/login"})
    return u


def require_admin(request: Request) -> dict:
    u = require_user(request)
    if u.get("perfil") != "admin":
        raise HTTPException(403, "Área restrita a administradores")
    return u


# ---------------------------------------------------------------- boot
@app.on_event("startup")
def startup() -> None:
    db.init_db()
    with db.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM usuarios").fetchone()["c"]
    if n == 0:  # seed inicial p/ MVP (trocar em produção!)
        db.criar_usuario("admin@magtracker.local", "admin123", "Administrador", "admin")
        db.criar_usuario("consulta@magtracker.local", "consulta123", "Usuário Consulta", "consulta")
    _start_scheduler()


_scheduler = None


def _start_scheduler():
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        _scheduler = BackgroundScheduler()
        _scheduler.add_job(lambda: executar_coleta(demo=_modo_demo()),
                           "interval", minutes=INTERVALO_MINUTOS, id="coleta_mag")
        _scheduler.start()
    except Exception:
        _scheduler = None


def _modo_demo() -> bool:
    return os.environ.get("MAG_DEMO", "1") == "1"


# ---------------------------------------------------------------- rotas páginas
@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    if get_session(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"erro": None},
    )


@app.post("/login")
def do_login(request: Request, email: str = Form(...), senha: str = Form(...)):
    user = db.autenticar(email, senha)
    ip = request.client.host if request.client else ""
    if not user:
        db.log_acesso(email, "login_falhou", "credenciais inválidas", ip)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"erro": "Credenciais inválidas"},
            status_code=401,
        )
    db.log_acesso(user["email"], "login", f"perfil={user['perfil']}", ip)
    resp = RedirectResponse("/", status_code=302)
    set_session(resp, user)
    return resp


@app.get("/logout")
def do_logout(request: Request):
    u = get_session(request)
    if u:
        db.log_acesso(u["email"], "logout")
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request, user: dict = Depends(require_user)):
    stats = db.dashboard_stats(ALERTA_DIAS)
    procs = db.listar_processos(limite=5)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"user": user, "stats": stats, "recentes": procs,
                 "alerta_dias": ALERTA_DIAS},
    )


@app.get("/processos", response_class=HTMLResponse)
def page_processos(request: Request, status: str | None = None, q: str | None = None,
                   user: dict = Depends(require_user)):
    processos = db.listar_processos(status=status, busca=q)
    statuses = ["Em análise", "Pendente", "Concluído", "Aguardando documentação",
                "Deferido", "Indeferido"]
    db.log_acesso(user["email"], "listagem", f"status={status} q={q}")
    return templates.TemplateResponse(
        request=request,
        name="processos.html",
        context={"user": user, "processos": processos, "statuses": statuses,
                 "status_sel": status or "", "q": q or "", "alerta_dias": ALERTA_DIAS},
    )


@app.get("/processos/{numero}", response_class=HTMLResponse)
def page_processo(request: Request, numero: str, user: dict = Depends(require_user)):
    proc, hist = db.obter_processo(numero)
    if not proc:
        raise HTTPException(404, "Processo não encontrado")
    db.log_acesso(user["email"], "detalhe", f"processo={numero}",
                  request.client.host if request.client else "")
    return templates.TemplateResponse(
        request=request,
        name="detalhe.html",
        context={"user": user, "proc": proc, "hist": hist,
                 "alerta_dias": ALERTA_DIAS},
    )


@app.get("/auditoria", response_class=HTMLResponse)
def page_auditoria(request: Request, user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        logs = conn.execute("SELECT * FROM logs_acesso ORDER BY id DESC LIMIT 200").fetchall()
        execs = conn.execute("SELECT * FROM execucoes_scraper ORDER BY id DESC LIMIT 50").fetchall()
    return templates.TemplateResponse(
        request=request,
        name="auditoria.html",
        context={"user": user, "logs": [dict(l) for l in logs],
                 "execs": [dict(e) for e in execs]},
    )


# ---------------------------------------------------------------- API JSON
@app.get("/api/processos")
def api_processos(status: str | None = None, q: str | None = None,
                  user: dict = Depends(require_user)):
    return JSONResponse(db.listar_processos(status=status, busca=q))


@app.post("/api/coleta/run")
def api_coleta_now(user: dict = Depends(require_admin)):
    resultado = executar_coleta(demo=_modo_demo())
    return JSONResponse(resultado)


@app.get("/health")
def health():
    return {"ok": True, "agora": datetime.now(timezone.utc).isoformat(),
            "scheduler": bool(_scheduler and _scheduler.running),
            "demo": _modo_demo()}
