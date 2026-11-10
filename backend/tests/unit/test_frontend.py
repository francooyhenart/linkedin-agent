"""El panel (frontend/index.html) y la API tienen que seguir de acuerdo.

No hay tests de JavaScript, pero este chequeo estático atrapa el error más común al mover rutas: que el HTML llame
a una URL que ya no existe.
"""
import re

from fastapi.testclient import TestClient

from app.core.config import PROJECT_ROOT
from app.main import app

HTML = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def normalize(path: str) -> str:
    """'/posts/${cur.id}/publish-now' y '/posts/{post_id}/publish-now' -> '/posts/{}/publish-now'."""
    return re.sub(r"\$\{[^}]*\}|\{[^}]*\}", "{}", path)


def frontend_api_paths() -> set[str]:
    # Todas las llamadas son `${API}/algo...` (template literals). Se corta en el '?' de la query o en la comilla.
    raw = re.findall(r"\$\{API\}(/[^`\"'?]*)", HTML)
    return {normalize("/api/content" + path) for path in raw}


def test_el_panel_usa_las_rutas_nuevas_y_ninguna_vieja():
    assert 'const API = "/api/content"' in HTML
    # Cualquier texto que arranque con "/api/..." salvo la definición de la constante (que es '"/api/content').
    literal_api_paths = re.findall(r"[\"'`]/api/[^\"'`]*", HTML)
    old_style = [m for m in literal_api_paths if m != '"/api/content']
    assert old_style == [], f"quedaron llamadas con la ruta vieja: {old_style}"


def test_cada_ruta_que_usa_el_panel_existe_en_la_api():
    api_paths = {normalize(path) for path in app.openapi()["paths"]}
    used = frontend_api_paths()
    assert len(used) >= 10  # el regex realmente encontró las llamadas
    missing = sorted(used - api_paths)
    assert missing == [], f"el panel llama a rutas que no existen: {missing}"


def test_las_rutas_de_oauth_del_panel_siguen_igual():
    assert 'href="/auth/linkedin"' in HTML
    paths = {r.path for r in app.router.routes if hasattr(r, "path")}
    assert "/health" in paths  # (las de /auth/linkedin viven en un router incluido; se prueban en test_linkedin_api)


def test_el_panel_conoce_el_estado_publishing():
    assert "publishing:" in HTML and "s-publishing" in HTML
    assert 'id="edSalio"' in HTML and 'id="edNoSalio"' in HTML  # "Sí, salió" / "No salió"


def test_get_raiz_sirve_el_panel():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Postline</title>" in response.text
    assert response.text == HTML
