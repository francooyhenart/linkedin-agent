"""POST /api/content/ideas/suggest a través de HTTP, con el cliente REAL de Ollama y transporte simulado."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.content.dependencies import get_post_writer
from app.modules.content.writer import OllamaPostWriter
from tests.helpers import ollama_client, ollama_says

API = "/api/content"
QUESTION = "¿Cómo te fue con la cola de Redis después de unas semanas?"


@pytest.fixture
def make_client(session_factory):
    """Devuelve una función: dado lo que "dice" Ollama (o un handler), arma el TestClient."""

    def build(handler):
        writer = OllamaPostWriter(ollama_client(ollama_says(handler) if isinstance(handler, str) else handler))
        app.dependency_overrides[get_post_writer] = lambda: writer
        return TestClient(app)

    yield build
    app.dependency_overrides.clear()


def add_idea(client, note):
    return client.post(f"{API}/ideas", json={"note": note}).json()


def test_devuelve_preguntas_atadas_a_una_idea_y_no_guarda_nada(make_client):
    client = make_client(f"1|{QUESTION}")
    idea = add_idea(client, "migré un cron a Redis")

    response = client.post(f"{API}/ideas/suggest")

    assert response.status_code == 200
    assert response.json() == {
        "suggestions": [{"question": QUESTION, "idea_id": idea["id"], "idea_note": "migré un cron a Redis"}]
    }
    assert [i["note"] for i in client.get(f"{API}/ideas").json()] == ["migré un cron a Redis"]  # nada nuevo
    assert client.get(f"{API}/posts").json() == []


def test_sin_ideas_es_400_con_el_formato_de_error_del_panel(make_client):
    response = make_client(f"1|{QUESTION}").post(f"{API}/ideas/suggest")
    assert response.status_code == 400
    assert "Primero anotá al menos una idea" in response.json()["detail"]


def test_si_el_modelo_no_devuelve_nada_util_es_502(make_client):
    client = make_client("Escribí sobre liderazgo y productividad.")
    add_idea(client, "algo")
    response = client.post(f"{API}/ideas/suggest")
    assert response.status_code == 502
    assert response.json() == {"detail": "El modelo no devolvió sugerencias que sirvan. Probá de nuevo."}


def test_si_ollama_no_tiene_el_modelo_el_error_llega_al_panel(make_client):
    client = make_client(lambda request: httpx.Response(404, json={"error": "model not found"}))
    add_idea(client, "algo")
    response = client.post(f"{API}/ideas/suggest")
    assert response.status_code == 502
    assert "ollama pull" in response.json()["detail"]


def test_la_ruta_no_choca_con_crear_ni_borrar_ideas(make_client):
    client = make_client(f"1|{QUESTION}")
    idea = add_idea(client, "algo")
    assert client.post(f"{API}/ideas", json={"note": "otra"}).status_code == 200  # POST /ideas sigue funcionando
    assert client.delete(f"{API}/ideas/{idea['id']}").json() == {"ok": True}
