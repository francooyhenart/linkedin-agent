"""El interruptor de notificaciones en Ajustes: activado por defecto y guardado en la tabla settings."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app

API = "/api/content"


@pytest.fixture
def client(session_factory):
    return TestClient(app)


def test_por_defecto_esta_activado(client):
    assert client.get(f"{API}/settings").json()["notify_ready"] is True


def test_se_puede_apagar_y_volver_a_encender(client, session_factory):
    assert client.put(f"{API}/settings", json={"notify_ready": False}).json()["notify_ready"] is False
    assert client.get(f"{API}/settings").json()["notify_ready"] is False
    with session_factory() as s:  # guardado como texto en la tabla settings
        assert s.execute(text("SELECT value FROM settings WHERE key = 'notify_ready'")).scalar() == "false"

    assert client.put(f"{API}/settings", json={"notify_ready": True}).json()["notify_ready"] is True


def test_guardar_otros_ajustes_no_cambia_el_interruptor(client):
    client.put(f"{API}/settings", json={"notify_ready": False})
    client.put(f"{API}/settings", json={"post_time": "10:30"})
    assert client.get(f"{API}/settings").json()["notify_ready"] is False
