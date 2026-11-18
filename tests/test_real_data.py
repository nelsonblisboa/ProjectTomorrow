from fastapi.testclient import TestClient

from app.web import app
from tests.test_web import _csrf


def _admin_client():
    client = TestClient(app)
    client.__enter__()
    token = _csrf(client)
    client.post("/login", data={"email": "admin@magtracker.local", "senha": "admin123",
                                "csrf_token": token})
    return client


def test_admin_can_open_manual_process_form():
    with TestClient(app) as client:
        token = _csrf(client)
        client.post("/login", data={"email": "admin@magtracker.local", "senha": "admin123",
                                    "csrf_token": token})
        response = client.get("/processos/novo")
        assert response.status_code == 200
        assert "Cadastro manual" in response.text
        assert "Consulta real desativada" in response.text


def test_real_scraper_is_disabled_by_default():
    with TestClient(app) as client:
        token = _csrf(client)
        client.post("/login", data={"email": "admin@magtracker.local", "senha": "admin123",
                                    "csrf_token": token})
        form_token = _csrf(client)
        response = client.post("/processos/raspar", data={
            "numero_processo": "SIM123456", "cpf": "", "data_nascimento": "",
            "csrf_token": form_token,
        })
        assert response.status_code == 403
