"""API de LinkedIn y estado: OAuth, publicar ahora, desconectar y GET /status. LinkedIn y Ollama simulados."""
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.integrations.linkedin.client import AUTHORIZE_URL
from app.main import app
from app.modules.content.dependencies import get_linkedin, get_post_writer
from app.modules.content.repository import ContentRepository
from app.modules.content.writer import OllamaPostWriter
from tests.helpers import LinkedInCalls, future_expiry, linkedin_client, ollama_client, ollama_says

API = "/api/content"
TOKEN = "AQX-token-super-secreto-123"


def oauth_handler(request: httpx.Request) -> httpx.Response:
    """LinkedIn simulado: canje del código, perfil y publicación."""
    if request.url.path == "/oauth/v2/accessToken":
        return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 5184000})
    if request.url.path == "/v2/userinfo":
        return httpx.Response(200, json={"sub": "abc123", "name": "Franco Test"})
    return httpx.Response(201, headers={"x-restli-id": "urn:li:share:555"})


class Env:
    """Un TestClient con el cliente de LinkedIn y el de Ollama simulados (un cliente de LinkedIn por test,
    porque guarda en memoria los 'state' del login)."""

    def __init__(self, session_factory, linkedin=None, ollama_handler=None):
        self.session_factory = session_factory
        self.linkedin = linkedin or linkedin_client(oauth_handler)
        writer = OllamaPostWriter(ollama_client(ollama_handler or ollama_says("x")))
        app.dependency_overrides[get_linkedin] = lambda: self.linkedin
        app.dependency_overrides[get_post_writer] = lambda: writer
        self.http = TestClient(app, follow_redirects=False)

    def login(self) -> None:
        """Hace el flujo OAuth completo (login + callback) y deja LinkedIn conectado."""
        redirect = self.http.get("/auth/linkedin")
        state = parse_qs(urlparse(redirect.headers["location"]).query)["state"][0]
        callback = self.http.get("/auth/linkedin/callback", params={"code": "el-codigo", "state": state})
        assert callback.status_code in (302, 307), callback.text


@pytest.fixture
def env(session_factory):
    environment = Env(session_factory)
    yield environment
    app.dependency_overrides.clear()


# ---------------------------------------------------------------- OAuth
def test_login_redirige_a_linkedin_con_el_scope_y_un_state(env):
    response = env.http.get("/auth/linkedin")
    assert response.status_code in (302, 307)
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTHORIZE_URL
    assert query["scope"] == ["openid profile w_member_social"]
    assert query["client_id"] == ["client-id"] and query["state"][0]


def test_login_sin_credenciales_configuradas_es_400(session_factory):
    Env(session_factory, linkedin=linkedin_client(oauth_handler, client_id="", client_secret=""))
    try:
        response = TestClient(app).get("/auth/linkedin", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400
    assert "LINKEDIN_CLIENT_ID" in response.json()["detail"]


def test_el_flujo_completo_conecta_y_guarda_el_permiso(env, session_factory):
    env.login()

    status = env.http.get(f"{API}/status").json()["linkedin"]
    assert status["connected"] is True and status["name"] == "Franco Test"
    assert status["expires_at"]  # texto con fecha y offset

    with session_factory() as s:
        stored = ContentRepository(s).get_settings_map()
    assert stored["li_token"] == TOKEN and stored["li_sub"] == "abc123"
    assert "+" in stored["li_expires_at"] or "-" in stored["li_expires_at"][10:]  # guardado con zona horaria


def test_callback_redirige_al_panel_en_ajustes(env):
    state = parse_qs(urlparse(env.http.get("/auth/linkedin").headers["location"]).query)["state"][0]
    response = env.http.get("/auth/linkedin/callback", params={"code": "c", "state": state})
    assert response.headers["location"] == "/#ajustes"


def test_callback_con_state_invalido_es_400_y_no_guarda_nada(env, session_factory):
    response = env.http.get("/auth/linkedin/callback", params={"code": "c", "state": "inventado"})
    assert response.status_code == 400 and "Estado OAuth inválido" in response.json()["detail"]
    with session_factory() as s:
        assert "li_token" not in ContentRepository(s).get_settings_map()


def test_el_state_no_se_puede_reusar(env):
    state = parse_qs(urlparse(env.http.get("/auth/linkedin").headers["location"]).query)["state"][0]
    assert env.http.get("/auth/linkedin/callback", params={"code": "c", "state": state}).status_code in (302, 307)
    assert env.http.get("/auth/linkedin/callback", params={"code": "c", "state": state}).status_code == 400


def test_callback_cuando_el_usuario_rechaza_en_linkedin(env):
    response = env.http.get(
        "/auth/linkedin/callback", params={"error": "user_cancelled_authorize", "error_description": "Cancelaste"}
    )
    assert response.status_code == 400
    assert "Cancelaste" in response.json()["detail"]


def test_callback_si_linkedin_falla_al_canjear_el_codigo_es_502(session_factory):
    def handler(request):
        return httpx.Response(400, text="invalid code") if request.url.path.endswith("accessToken") else oauth_handler(request)

    environment = Env(session_factory, linkedin=linkedin_client(handler))
    try:
        state = parse_qs(urlparse(environment.http.get("/auth/linkedin").headers["location"]).query)["state"][0]
        response = environment.http.get("/auth/linkedin/callback", params={"code": "c", "state": state})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 502


def test_desconectar_borra_el_permiso(env, session_factory):
    env.login()
    assert env.http.post(f"{API}/linkedin/disconnect").json() == {"ok": True}

    assert env.http.get(f"{API}/status").json()["linkedin"]["connected"] is False
    with session_factory() as s:
        assert all(v == "" for k, v in ContentRepository(s).get_settings_map().items() if k.startswith("li_"))


def test_ajustes_nunca_exponen_el_token_ni_despues_de_conectar(env):
    env.login()
    response = env.http.get(f"{API}/settings")
    assert TOKEN not in response.text and "Franco Test" not in response.text
    assert not any(key.startswith("li_") for key in response.json())


# ---------------------------------------------------------------- GET /status
def test_status_informa_ollama_linkedin_y_la_version_de_la_api(env):
    body = env.http.get(f"{API}/status").json()
    assert set(body) == {"ollama", "linkedin"}
    assert body["linkedin"]["configured"] is True and body["linkedin"]["connected"] is False
    assert body["linkedin"]["api_version"] == "202610"
    assert body["linkedin"]["api_version_age_months"] >= 0
    assert body["linkedin"]["api_version_outdated"] is False
    assert body["linkedin"]["expires_at"] == ""


def test_status_de_ollama_ok_con_sus_modelos(session_factory):
    handler = lambda request: httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]})  # noqa: E731
    environment = Env(session_factory, ollama_handler=handler)
    try:
        ollama = environment.http.get(f"{API}/status").json()["ollama"]
    finally:
        app.dependency_overrides.clear()
    assert ollama == {"ok": True, "models": ["llama3.1:8b"]}


def test_status_de_ollama_apagado(session_factory):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    environment = Env(session_factory, ollama_handler=down)
    try:
        ollama = environment.http.get(f"{API}/status").json()["ollama"]
    finally:
        app.dependency_overrides.clear()
    assert ollama == {"ok": False, "models": []}


@pytest.mark.parametrize(("version", "outdated"), [("209912", False), ("201501", True)])
def test_status_marca_la_version_vieja_con_mas_de_10_meses(session_factory, version, outdated):
    """Se usan versiones muy lejanas (futura y de 2015) para que el resultado no dependa del reloj.
    El cálculo exacto en el límite de los 10 meses, con fechas fijas, está en tests/unit/test_linkedin_client.py."""
    environment = Env(session_factory, linkedin=linkedin_client(oauth_handler, api_version=version))
    try:
        linkedin = environment.http.get(f"{API}/status").json()["linkedin"]
    finally:
        app.dependency_overrides.clear()
    assert linkedin["api_version"] == version
    assert linkedin["api_version_outdated"] is outdated


# ---------------------------------------------------------------- publicar ahora
def make_post(env, text="Un post", status="draft"):
    response = env.http.post(
        f"{API}/posts", json={"text": text, "scheduled_at": "2026-11-12T09:00", "status": status}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_publish_now_publica_y_devuelve_el_post(env):
    env.login()
    post = make_post(env)

    response = env.http.post(f"{API}/posts/{post['id']}/publish-now")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published" and body["linkedin_url"].endswith("urn:li:share:555/")
    assert body["error"] is None and body["published_at"]


def test_publish_now_sin_linkedin_conectado_es_400(env):
    post = make_post(env)
    response = env.http.post(f"{API}/posts/{post['id']}/publish-now")
    assert response.status_code == 400 and "Conectá LinkedIn" in response.json()["detail"]


def test_publish_now_de_un_post_inexistente_es_404(env):
    env.login()
    assert env.http.post(f"{API}/posts/999/publish-now").status_code == 404


def test_publish_now_con_rechazo_de_linkedin_es_502_y_el_post_queda_failed(session_factory):
    calls = LinkedInCalls(lambda request: httpx.Response(401, json={}))

    def handler(request):
        return calls(request) if request.url.path == "/rest/posts" else oauth_handler(request)

    environment = Env(session_factory, linkedin=linkedin_client(handler))
    try:
        environment.login()
        post = make_post(environment)
        response = environment.http.post(f"{API}/posts/{post['id']}/publish-now")
        after = environment.http.get(f"{API}/posts", params={"status": "failed"}).json()
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 502 and "Reconectá LinkedIn" in response.json()["detail"]
    assert [p["id"] for p in after] == [post["id"]] and "Reconectá" in after[0]["error"]


def test_publish_now_con_timeout_es_502_el_post_queda_en_publishing_y_no_se_puede_reintentar(session_factory):
    calls = LinkedInCalls(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("t", request=request)))

    def handler(request):
        return calls(request) if request.url.path == "/rest/posts" else oauth_handler(request)

    environment = Env(session_factory, linkedin=linkedin_client(handler))
    try:
        environment.login()
        post = make_post(environment)
        first = environment.http.post(f"{API}/posts/{post['id']}/publish-now")
        second = environment.http.post(f"{API}/posts/{post['id']}/publish-now")
        listed = environment.http.get(f"{API}/posts", params={"status": "publishing"}).json()
    finally:
        app.dependency_overrides.clear()
    assert first.status_code == 502 and "verificá en LinkedIn" in first.json()["detail"]
    assert second.status_code == 400 and "ya se está publicando" in second.json()["detail"]
    assert [p["id"] for p in listed] == [post["id"]]
    assert calls.count == 1


def test_el_texto_que_se_publica_es_el_guardado_con_los_caracteres_escapados(session_factory):
    calls = LinkedInCalls()

    def handler(request):
        return calls(request) if request.url.path == "/rest/posts" else oauth_handler(request)

    environment = Env(session_factory, linkedin=linkedin_client(handler))
    try:
        environment.login()
        post = make_post(environment, text="Shipped it (finally) with snake_case")
        environment.http.post(f"{API}/posts/{post['id']}/publish-now")
        stored = environment.http.get(f"{API}/posts").json()[0]["text"]
    finally:
        app.dependency_overrides.clear()
    assert json.loads(calls.requests[0].content)["commentary"] == r"Shipped it \(finally\) with snake\_case"
    assert stored == "Shipped it (finally) with snake_case"  # lo guardado y lo que ve el usuario, sin escapes


# ---------------------------------------------------------------- 'publishing' lo maneja la app
def test_no_se_puede_crear_ni_pasar_a_mano_un_post_a_publishing(env):
    created = env.http.post(f"{API}/posts", json={"scheduled_at": "2026-11-12T09:00", "status": "publishing"})
    assert created.status_code == 400 and "lo maneja la app" in created.json()["detail"]

    post = make_post(env)
    patched = env.http.patch(f"{API}/posts/{post['id']}", json={"status": "publishing"})
    assert patched.status_code == 400


def test_el_usuario_resuelve_verificar_en_linkedin_marcandolo_como_publicado(session_factory):
    calls = LinkedInCalls(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("t", request=request)))

    def handler(request):
        return calls(request) if request.url.path == "/rest/posts" else oauth_handler(request)

    environment = Env(session_factory, linkedin=linkedin_client(handler))
    try:
        environment.login()
        post = make_post(environment)
        environment.http.post(f"{API}/posts/{post['id']}/publish-now")  # queda en publishing
        resolved = environment.http.patch(
            f"{API}/posts/{post['id']}",
            json={"status": "published", "linkedin_url": "https://www.linkedin.com/feed/update/urn:li:share:1/"},
        )
    finally:
        app.dependency_overrides.clear()
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "published" and resolved.json()["error"] is None
