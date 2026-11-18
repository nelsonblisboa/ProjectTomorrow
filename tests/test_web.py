import re

from fastapi.testclient import TestClient

from app.web import app


def _csrf(client: TestClient) -> str:
    response = client.get("/login")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_login_page_renders_and_sets_csrf_cookie():
    with TestClient(app) as client:
        response = client.get("/login")
        assert response.status_code == 200
        assert "MAG Tracker" in response.text
        assert client.cookies.get("mag_csrf")


def test_login_rejects_missing_or_invalid_csrf():
    with TestClient(app) as client:
        response = client.post("/login", data={"email": "admin@magtracker.local",
                                               "senha": "admin123", "csrf_token": "wrong"})
        assert response.status_code == 403


def test_demo_login_and_protected_dashboard():
    with TestClient(app) as client:
        token = _csrf(client)
        response = client.post("/login", data={"email": "admin@magtracker.local",
                                               "senha": "admin123", "csrf_token": token},
                              follow_redirects=False)
        assert response.status_code == 303
        assert "mag_session" in response.cookies
        dashboard = client.get("/", follow_redirects=False)
        assert dashboard.status_code == 200
        assert "Dashboard" in dashboard.text


def test_protected_route_redirects_to_login():
    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
