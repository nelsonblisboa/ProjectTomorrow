"""MAG Tracker web application: pages, session authentication and API."""
from __future__ import annotations

import hashlib
import hmac
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
SECRET = os.environ.get("MAG_SECRET") or secrets.token_urlsafe(32)
COOKIE = "mag_session"
CSRF_COOKIE = "mag_csrf"
ALERTA_DIAS = int(os.environ.get("MAG_ALERTA_DIAS", "30"))
app = FastAPI(title="MAG Tracker", version="0.3.0")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _modo_demo() -> bool:
    """Demo must be opted into; production is the safe default."""
    return os.environ.get("MAG_DEMO", "0").strip().lower() in {"1", "true", "yes"}


def _cookie_secure() -> bool:
    configured = os.environ.get("MAG_COOKIE_SECURE")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes"}
    return not _modo_demo()


def _set_csrf_cookie(response, token: str) -> None:
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=_cookie_secure(),
                        samesite="strict", max_age=3600)


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


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    if get_session(request):
        return RedirectResponse("/", status_code=302)
    token = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request=request, name="login.html", context={"erro": None, "csrf_token": token}
    )
    _set_csrf_cookie(response, token)
    return response


@app.post("/login")
def do_login(request: Request, email: str = Form(...), senha: str = Form(...),
             csrf_token: str = Form(...)):
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if not cookie_token or not hmac.compare_digest(cookie_token, csrf_token):
        raise HTTPException(403, "Token de segurança inválido. Atualize a página e tente novamente.")

    user = db.autenticar(email, senha)
    ip = request.client.host if request.client else ""
    if not user:
        db.log_acesso(email, "login_falhou", "credenciais inválidas", ip)
        response = templates.TemplateResponse(
            request=request, name="login.html",
            context={"erro": "Credenciais inválidas", "csrf_token": csrf_token},
            status_code=401,
        )
        _set_csrf_cookie(response, csrf_token)
        return response
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
    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={"user": user, "stats": stats, "recentes": processes,
                 "alerta_dias": ALERTA_DIAS},
    )


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
def api_coleta_now(user: dict = Depends(require_admin)):
    return JSONResponse(executar_coleta(demo=_modo_demo()))


@app.get("/health")
def health():
    return {"ok": True, "agora": datetime.now(timezone.utc).isoformat(),
            "scheduler": bool(_scheduler and _scheduler.running), "demo": _modo_demo()}
