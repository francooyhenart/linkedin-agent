"""Tests de la API de content contra la base de test, con Ollama simulado a nivel HTTP (httpx.MockTransport)."""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.modules.content.dependencies import get_post_writer
from app.modules.content.repository import ContentRepository
from app.modules.content.writer import OllamaPostWriter
from tests.helpers import ollama_client, ollama_says

API = "/api/content"
OLLAMA_REPLY = "Draft from the mocked Ollama. Plain text."


@pytest.fixture
def client(session_factory):
    """TestClient con el cliente REAL de Ollama pero con el transporte simulado (no hace falta Ollama)."""
    writer = OllamaPostWriter(ollama_client(ollama_says(OLLAMA_REPLY)))
    app.dependency_overrides[get_post_writer] = lambda: writer
    yield TestClient(app)
    app.dependency_overrides.clear()


def make_post(client, **overrides):
    body = {"text": "hola", "scheduled_at": "2026-11-12T09:00", "status": "draft", **overrides}
    response = client.post(f"{API}/posts", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- ideas
def test_ciclo_de_una_idea(client):
    created = client.post(f"{API}/ideas", json={"note": "  migré un cron a Redis  "})
    assert created.status_code == 200
    idea = created.json()
    assert idea["note"] == "migré un cron a Redis"  # se limpia con strip
    assert idea["used"] is False

    assert [i["id"] for i in client.get(f"{API}/ideas").json()] == [idea["id"]]

    assert client.delete(f"{API}/ideas/{idea['id']}").json() == {"ok": True}
    assert client.get(f"{API}/ideas").json() == []


def test_idea_vacia_es_400_con_el_formato_de_error_del_panel(client):
    response = client.post(f"{API}/ideas", json={"note": "   "})
    assert response.status_code == 400
    assert response.json() == {"detail": "La idea está vacía."}


def test_las_ideas_sin_usar_salen_primero(client):
    first = client.post(f"{API}/ideas", json={"note": "primera"}).json()
    client.post(f"{API}/ideas", json={"note": "segunda"})
    make_post(client, idea_id=first["id"])  # usa la primera
    assert [i["note"] for i in client.get(f"{API}/ideas").json()] == ["segunda", "primera"]


# ---------------------------------------------------------------- posts
def test_crear_post_desde_una_idea_la_marca_como_usada(client):
    idea = client.post(f"{API}/ideas", json={"note": "algo"}).json()
    post = make_post(client, idea_id=idea["id"])
    assert post["idea_id"] == idea["id"]
    assert client.get(f"{API}/ideas").json()[0]["used"] is True


def test_fechas_viajan_en_hora_local_sin_offset(client):
    post = make_post(client, scheduled_at="2026-11-12T09:00")
    assert post["scheduled_at"] == "2026-11-12T09:00"


def test_post_con_idea_inexistente_es_404(client):
    response = client.post(f"{API}/posts", json={"scheduled_at": "2026-11-12T09:00", "idea_id": 999})
    assert response.status_code == 404


def test_fecha_invalida_es_400(client):
    response = client.post(f"{API}/posts", json={"scheduled_at": "mañana"})
    assert response.status_code == 400
    assert "Fecha inválida" in response.json()["detail"]


def test_estado_invalido_es_422(client):
    response = client.post(f"{API}/posts", json={"scheduled_at": "2026-11-12T09:00", "status": "basura"})
    assert response.status_code == 422


def test_patch_a_published_pone_published_at_y_borra_el_error(client):
    post = make_post(client)
    patched = client.patch(f"{API}/posts/{post['id']}", json={"status": "published"}).json()
    assert patched["status"] == "published"
    assert patched["published_at"] is not None
    assert patched["error"] is None


def test_patch_de_un_post_inexistente_es_404(client):
    assert client.patch(f"{API}/posts/999", json={"text": "x"}).status_code == 404


def test_listar_posts_filtra_por_rango_y_estado(client):
    make_post(client, scheduled_at="2026-11-02T09:00")
    make_post(client, scheduled_at="2026-11-12T09:00", status="published")
    make_post(client, scheduled_at="2026-11-20T09:00")

    in_range = client.get(f"{API}/posts", params={"start": "2026-11-10", "end": "2026-11-20"}).json()
    assert [p["scheduled_at"] for p in in_range] == ["2026-11-12T09:00"]  # end es exclusivo

    published = client.get(f"{API}/posts", params={"status": "published"}).json()
    assert len(published) == 1


def test_borrar_un_post_borra_sus_metricas(client, session_factory):
    post = make_post(client)
    client.put(f"{API}/posts/{post['id']}/metrics", json={"impressions": 5})
    assert client.delete(f"{API}/posts/{post['id']}").json() == {"ok": True}
    assert client.get(f"{API}/posts/{post['id']}/metrics").status_code == 404
    with session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM post_metrics")).scalar() == 0


# ---------------------------------------------------------------- métricas
def test_el_panel_ve_el_ultimo_snapshot_aplanado(client, session_factory):
    post = make_post(client, status="published")
    assert (post["impressions"], post["metrics_captured_at"]) == (0, None)

    updated = client.put(f"{API}/posts/{post['id']}/metrics", json={"impressions": 100, "reactions": 7}).json()
    assert (updated["impressions"], updated["reactions"], updated["comments"]) == (100, 7, 0)
    assert updated["metrics_captured_at"] is not None


def test_cargar_metricas_varias_veces_el_mismo_dia_corrige_el_snapshot_de_hoy(client):
    post = make_post(client, status="published")
    for impressions in (10, 20, 30):
        client.put(f"{API}/posts/{post['id']}/metrics", json={"impressions": impressions})
    history = client.get(f"{API}/posts/{post['id']}/metrics").json()
    assert [h["impressions"] for h in history] == [30]


def test_metricas_negativas_son_422(client):
    post = make_post(client)
    assert client.put(f"{API}/posts/{post['id']}/metrics", json={"impressions": -1}).status_code == 422


# ---------------------------------------------------------------- ajustes
def test_get_ajustes_devuelve_los_defaults_y_la_lista_de_frases_prohibidas(client):
    settings = client.get(f"{API}/settings").json()
    assert settings["post_days"] == "1,3"
    assert settings["max_chars"] == 300
    assert "thrilled" in settings["banned"]


def test_put_ajustes_valida_y_acota(client):
    ok = client.put(f"{API}/settings", json={"post_days": "0,2,4", "max_chars": 99999, "model": " mistral "}).json()
    assert (ok["post_days"], ok["max_chars"], ok["model"]) == ("0,2,4", 3000, "mistral")
    assert client.put(f"{API}/settings", json={"post_time": "9am"}).status_code == 400
    assert client.put(f"{API}/settings", json={"post_time": "25:00"}).status_code == 400
    assert client.get(f"{API}/settings").json()["post_days"] == "0,2,4"  # persistió


def test_ajustes_nunca_exponen_tokens_ni_datos_de_linkedin(client, session_factory):
    with session_factory() as s:  # simula una conexión de LinkedIn ya hecha
        ContentRepository(s).upsert_settings(
            {"li_token": "SECRETO-123", "li_sub": "abc", "li_name": "Franco", "li_expires_at": "2027-01-01T00:00"}
        )
        s.commit()

    body = client.get(f"{API}/settings")
    assert not any(key.startswith("li_") for key in body.json())
    assert "SECRETO-123" not in body.text and "Franco" not in body.text


def test_put_ajustes_no_puede_escribir_tokens(client, session_factory):
    client.put(f"{API}/settings", json={"li_token": "intruso", "max_chars": 200})
    with session_factory() as s:
        assert "li_token" not in ContentRepository(s).get_settings_map()


# ---------------------------------------------------------------- generación y planificación
def test_generate_usa_el_escritor_inyectado(client):
    result = client.post(f"{API}/generate", json={"note": "una nota"}).json()
    assert result["text"] == OLLAMA_REPLY
    assert result["warnings"] == []


def test_generate_con_ollama_apagado_es_502_con_mensaje_claro(client):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    app.dependency_overrides[get_post_writer] = lambda: OllamaPostWriter(ollama_client(down))
    response = client.post(f"{API}/generate", json={"note": "una nota"})
    assert response.status_code == 502
    assert "No pude hablar con Ollama" in response.json()["detail"]


def test_generate_con_modelo_inexistente_es_502_y_explica_el_pull(client):
    handler = lambda request: httpx.Response(404, json={"error": "not found"})  # noqa: E731
    app.dependency_overrides[get_post_writer] = lambda: OllamaPostWriter(ollama_client(handler))
    response = client.post(f"{API}/generate", json={"note": "una nota"})
    assert response.status_code == 502
    assert "ollama pull llama3.1:8b" in response.json()["detail"]


def test_generate_sin_nota_ni_borrador_es_400(client):
    assert client.post(f"{API}/generate", json={}).status_code == 400


def test_plan_sin_ideas_es_400(client):
    response = client.post(f"{API}/plan")
    assert response.status_code == 400
    assert "No hay ideas sin usar" in response.json()["detail"]


def test_plan_crea_borradores_en_dias_libres_y_marca_las_ideas(client):
    for note in ("uno", "dos", "tres"):
        client.post(f"{API}/ideas", json={"note": note})

    body = client.post(f"{API}/plan").json()
    posts = body["created"]

    assert body["failed"] == []
    assert len(posts) == 2  # días por defecto "1,3" = 2 por semana -> toma 2 ideas
    assert {p["status"] for p in posts} == {"draft"}
    assert {p["text"] for p in posts} == {OLLAMA_REPLY}
    assert len({p["scheduled_at"][:10] for p in posts}) == 2  # un post por día
    assert all(p["scheduled_at"].endswith("T09:00") for p in posts)
    used = [i["used"] for i in client.get(f"{API}/ideas").json()]
    assert sorted(used) == [False, True, True]


def test_plan_con_ollama_apagado_responde_200_con_el_motivo_por_idea(client):
    """Contrato nuevo: {created, failed}. Si todo falla, created viene vacío y failed trae cada motivo."""

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    app.dependency_overrides[get_post_writer] = lambda: OllamaPostWriter(ollama_client(down))
    ids = [client.post(f"{API}/ideas", json={"note": n}).json()["id"] for n in ("uno", "dos")]

    response = client.post(f"{API}/plan")

    assert response.status_code == 200
    body = response.json()
    assert body["created"] == []
    assert sorted(f["idea_id"] for f in body["failed"]) == sorted(ids)
    assert all("No pude hablar con Ollama" in f["reason"] for f in body["failed"])
    assert all(not i["used"] for i in client.get(f"{API}/ideas").json())
