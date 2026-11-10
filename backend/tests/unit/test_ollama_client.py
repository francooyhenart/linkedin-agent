"""Cliente de Ollama, con el transporte HTTP simulado (no hace falta Ollama ni red)."""
import json

import httpx
import pytest

from app.core.exceptions import ExternalServiceError
from app.shared.llm.ollama import CONNECT_TIMEOUT, READ_TIMEOUT, OllamaClient
from tests.helpers import ollama_client, ollama_says


def test_chat_devuelve_el_texto_y_manda_lo_que_corresponde():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "Hola!"}})

    client = ollama_client(handler, base_url="http://ollama.test/")  # la barra final se ignora
    assert client.chat("llama3.1:8b", "sistema", "usuario") == "Hola!"

    request = seen[0]
    assert (request.method, str(request.url)) == ("POST", "http://ollama.test/api/chat")
    body = json.loads(request.content)
    assert body["model"] == "llama3.1:8b"
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.7}
    assert body["messages"] == [
        {"role": "system", "content": "sistema"},
        {"role": "user", "content": "usuario"},
    ]


def test_timeouts_por_defecto_conexion_corta_y_lectura_larga():
    client = OllamaClient("http://127.0.0.1:11434")
    timeout = client._http.timeout
    assert timeout.connect == CONNECT_TIMEOUT == 5.0
    assert timeout.read == READ_TIMEOUT == 300.0


def test_ollama_apagado_da_un_mensaje_claro():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ExternalServiceError, match="No pude hablar con Ollama en http://ollama.test. ¿Está abierto"):
        ollama_client(handler).chat("m", "s", "u")


def test_timeout_de_conexion_tambien_es_ollama_apagado():
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(ExternalServiceError, match="No pude hablar con Ollama"):
        ollama_client(handler).chat("m", "s", "u")


def test_timeout_de_lectura_dice_que_tardo_demasiado():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ExternalServiceError, match="tardó más de 300 segundos"):
        ollama_client(handler).chat("m", "s", "u")


def test_conexion_cortada_a_mitad_de_respuesta():
    def handler(request):
        raise httpx.RemoteProtocolError("closed", request=request)

    with pytest.raises(ExternalServiceError, match="Se cortó la conexión"):
        ollama_client(handler).chat("m", "s", "u")


def test_modelo_inexistente_explica_como_bajarlo():
    handler = lambda request: httpx.Response(404, json={"error": "model 'foo' not found"})  # noqa: E731
    with pytest.raises(ExternalServiceError) as error:
        ollama_client(handler).chat("foo", "s", "u")
    assert error.value.message == "Ollama no tiene el modelo 'foo'. Corré: ollama pull foo"


def test_otro_error_http_incluye_el_codigo_y_un_pedazo_del_cuerpo():
    handler = lambda request: httpx.Response(500, text="boom" * 100)  # noqa: E731
    with pytest.raises(ExternalServiceError) as error:
        ollama_client(handler).chat("m", "s", "u")
    assert error.value.message.startswith("Ollama respondió 500: boomboom")
    assert len(error.value.message) < 260  # el cuerpo se recorta


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="esto no es json"),
        httpx.Response(200, json={"sin": "message"}),
        httpx.Response(200, json={"message": {"content": 123}}),
        httpx.Response(200, json=["lista"]),
    ],
)
def test_respuesta_con_forma_inesperada(response):
    with pytest.raises(ExternalServiceError, match="no pude entender"):
        ollama_client(lambda request: response).chat("m", "s", "u")


def test_todos_los_errores_son_external_service_error_y_el_texto_vacio_pasa():
    assert ollama_client(ollama_says("")).chat("m", "s", "u") == ""  # igual que la app original


# ---------------------------------------------------------------- status
def test_status_ok_lista_los_modelos():
    handler = lambda request: httpx.Response(200, json={"models": [{"name": "a:1"}, {"name": "b:2"}]})  # noqa: E731
    status = ollama_client(handler).status()
    assert (status.ok, status.models) == (True, ["a:1", "b:2"])


def test_status_nunca_lanza():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    assert ollama_client(down).status().ok is False
    assert ollama_client(lambda request: httpx.Response(500)).status().ok is False
    assert ollama_client(lambda request: httpx.Response(200, text="no json")).status().ok is False
