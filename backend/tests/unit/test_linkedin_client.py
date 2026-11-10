"""Cliente de LinkedIn, con el transporte HTTP simulado.

Lo más importante de este archivo: la clasificación de errores de publish_text.
  - 4xx                         -> LinkedInRejectedError       (seguro NO se publicó)
  - timeout / conexión / 5xx    -> LinkedInOutcomeUnknownError (NO SABEMOS: no se reintenta solo)
  - 2xx                         -> éxito, y nunca lanza después
"""
import json
import logging
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.exceptions import ExternalServiceError
from app.integrations.linkedin.client import (
    AUTHORIZE_URL,
    POSTS_URL,
    api_version_age_months,
    escape_commentary,
)
from app.integrations.linkedin.exceptions import (
    LinkedInError,
    LinkedInOutcomeUnknownError,
    LinkedInRejectedError,
)
from tests.helpers import LinkedInCalls, linkedin_client

TOKEN = "AQX-token-super-secreto-123"


def publish(client, text="Hola"):
    return client.publish_text(TOKEN, "abc123", text)


# ---------------------------------------------------------------- el request que se manda
def test_publica_en_rest_posts_con_los_headers_y_el_cuerpo_de_la_posts_api():
    calls = LinkedInCalls()
    result = publish(linkedin_client(calls, api_version="202610"), "Un post")

    request = calls.requests[0]
    assert (request.method, str(request.url)) == ("POST", POSTS_URL) and POSTS_URL.endswith("/rest/posts")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Linkedin-Version"] == "202610"
    assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert json.loads(request.content) == {
        "author": "urn:li:person:abc123",
        "commentary": "Un post",
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    assert result.post_urn == "urn:li:share:123"
    assert result.url == "https://www.linkedin.com/feed/update/urn:li:share:123/"


def test_la_version_del_header_es_la_configurada():
    calls = LinkedInCalls()
    publish(linkedin_client(calls, api_version="202701"))
    assert calls.requests[0].headers["Linkedin-Version"] == "202701"


# ---------------------------------------------------------------- escapado del texto
@pytest.mark.parametrize("char", list("|{}@[]()<>#*_~\\"))
def test_cada_caracter_reservado_se_escapa(char):
    assert escape_commentary(f"a{char}b") == f"a\\{char}b"


def test_el_texto_comun_y_los_saltos_de_linea_no_se_tocan():
    text = "Moved a cron job to Redis.\n\nTook two days, 100% worth it! Ñandú, ¿sí?"
    assert escape_commentary(text) == text


def test_el_escapado_viaja_en_el_commentary_pero_el_texto_original_no_cambia():
    calls = LinkedInCalls()
    original = "Fixed a_bug (finally) #devops"
    publish(linkedin_client(calls), original)
    sent = json.loads(calls.requests[0].content)["commentary"]
    assert sent == r"Fixed a\_bug \(finally\) \#devops"
    assert original == "Fixed a_bug (finally) #devops"


def test_escapado_ida_y_vuelta():
    import re

    text = "x|{}@[]()<>#*_~\\y"
    assert re.sub(r"\\(.)", r"\1", escape_commentary(text)) == text


# ---------------------------------------------------------------- 4xx: seguro NO se publicó
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_un_4xx_es_rechazo_definitivo(status):
    client = linkedin_client(lambda request: httpx.Response(status, json={"message": "no"}))
    with pytest.raises(LinkedInRejectedError):
        publish(client)


def test_401_pide_reconectar():
    client = linkedin_client(lambda request: httpx.Response(401, json={"code": "REVOKED_ACCESS_TOKEN"}))
    with pytest.raises(LinkedInRejectedError, match="Reconectá LinkedIn en Ajustes"):
        publish(client)


def test_403_explica_el_permiso():
    client = linkedin_client(lambda request: httpx.Response(403, json={"code": "ACCESS_DENIED"}))
    with pytest.raises(LinkedInRejectedError, match="w_member_social"):
        publish(client)


def test_426_version_inexistente_manda_a_revisar_la_variable():
    body = {"status": 426, "code": "NONEXISTENT_VERSION", "message": "Requested version 202401 is not active"}
    client = linkedin_client(lambda request: httpx.Response(426, json=body), api_version="202401")
    with pytest.raises(LinkedInRejectedError) as error:
        publish(client)
    assert "LINKEDIN_API_VERSION" in error.value.message and "202401" in error.value.message


def test_400_por_version_faltante_tambien_manda_a_revisar_la_variable():
    body = {"status": 400, "code": "VERSION_MISSING", "message": "A version must be present."}
    client = linkedin_client(lambda request: httpx.Response(400, json=body))
    with pytest.raises(LinkedInRejectedError, match="LINKEDIN_API_VERSION"):
        publish(client)


def test_un_400_comun_no_se_confunde_con_el_de_version():
    client = linkedin_client(lambda request: httpx.Response(400, json={"code": "FIELD_LENGTH_TOO_LONG"}))
    with pytest.raises(LinkedInRejectedError) as error:
        publish(client)
    assert "LINKEDIN_API_VERSION" not in error.value.message
    assert "400" in error.value.message


def test_429_dice_que_es_el_limite_y_es_rechazo_definitivo():
    client = linkedin_client(lambda request: httpx.Response(429, text="slow down"))
    with pytest.raises(LinkedInRejectedError, match="demasiados pedidos"):
        publish(client)


# ---------------------------------------------------------------- 408 y 409: son 4xx pero NO sabemos qué pasó
@pytest.mark.parametrize("status", [408, 409])
def test_408_y_409_son_resultado_desconocido_aunque_sean_4xx(status):
    """408 es 'timeout' y 409 es 'conflicto de escritura, reintentá': LinkedIn pudo haber creado el post."""
    client = linkedin_client(lambda request: httpx.Response(status, json={"message": "retry"}))
    with pytest.raises(LinkedInOutcomeUnknownError, match=f"respondió {status}.*verificá en LinkedIn") as error:
        publish(client)
    assert not isinstance(error.value, LinkedInRejectedError)


def test_los_demas_4xx_siguen_siendo_rechazo_definitivo():
    for status in (400, 401, 403, 404, 410, 413, 422, 429):
        client = linkedin_client(lambda request, s=status: httpx.Response(s, json={"message": "x"}))
        with pytest.raises(LinkedInRejectedError):
            publish(client)


# ---------------------------------------------------------------- contenido duplicado: "ya estaba publicado"
@pytest.mark.parametrize("urn", ["urn:li:share:7000000000000000001", "urn:li:ugcPost:7000000000000000002"])
def test_422_de_contenido_duplicado_se_trata_como_ya_publicado_con_el_urn_del_original(urn):
    body = {"status": 422, "message": f"Content is a duplicate of {urn}"}
    client = linkedin_client(lambda request: httpx.Response(422, json=body))

    result = publish(client)

    assert result.was_duplicate is True
    assert result.post_urn == urn
    assert result.url == f"https://www.linkedin.com/feed/update/{urn}/"


def test_un_post_nuevo_no_es_duplicado():
    result = publish(linkedin_client(LinkedInCalls()))
    assert result.was_duplicate is False


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(422, json={"message": "Error validating the post: commentary is required"}),
        httpx.Response(422, json={"message": "Content is a duplicate"}),  # sin URN: no se puede confirmar
        httpx.Response(422, text="duplicate of something"),
        httpx.Response(400, json={"message": "Do not post duplicate content"}),  # formato viejo, sin URN
        httpx.Response(429, json={"message": "Content is a duplicate of urn:li:share:1"}),  # otro código: no cuenta
    ],
)
def test_un_rechazo_que_no_trae_el_urn_del_original_sigue_siendo_failed(response):
    with pytest.raises(LinkedInRejectedError):
        publish(linkedin_client(lambda request: response))


# ---------------------------------------------------------------- timeout / conexión / 5xx: NO SABEMOS
def test_timeout_de_lectura_es_resultado_desconocido():
    def handler(request):
        raise httpx.ReadTimeout("el servidor no contestó", request=request)

    with pytest.raises(LinkedInOutcomeUnknownError, match="verificá en LinkedIn"):
        publish(linkedin_client(handler))


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("t"),
        httpx.WriteTimeout("t"),
        httpx.PoolTimeout("t"),
        httpx.ConnectTimeout("t"),
        httpx.ConnectError("c"),
        httpx.ReadError("r"),
        httpx.RemoteProtocolError("p"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_cualquier_error_de_red_es_resultado_desconocido(error):
    def handler(request):
        raise error

    with pytest.raises(LinkedInOutcomeUnknownError):
        publish(linkedin_client(handler))


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_un_5xx_es_resultado_desconocido(status):
    client = linkedin_client(lambda request: httpx.Response(status, text="error del servidor"))
    with pytest.raises(LinkedInOutcomeUnknownError, match=f"respondió {status}.*verificá en LinkedIn"):
        publish(client)


def test_las_dos_clases_se_distinguen_y_ninguna_es_subclase_de_la_otra():
    assert not issubclass(LinkedInRejectedError, LinkedInOutcomeUnknownError)
    assert not issubclass(LinkedInOutcomeUnknownError, LinkedInRejectedError)
    # Las dos salen como 502 en la API (son ExternalServiceError).
    assert issubclass(LinkedInRejectedError, ExternalServiceError) and issubclass(LinkedInError, ExternalServiceError)


# ---------------------------------------------------------------- 2xx: ya está publicado, nunca lanza
def test_2xx_sin_header_de_id_devuelve_url_none_en_vez_de_un_error():
    result = publish(linkedin_client(lambda request: httpx.Response(201)))
    assert (result.post_urn, result.url) == (None, None)


def test_cualquier_2xx_cuenta_como_publicado():
    assert publish(linkedin_client(lambda request: httpx.Response(200, headers={"x-restli-id": "urn:li:share:9"})))


# ---------------------------------------------------------------- los tokens nunca aparecen
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text=f"token {TOKEN} inválido"),
        httpx.Response(403, json={}),
        httpx.Response(400, json={"message": "mal"}),
        httpx.Response(500),
    ],
)
def test_los_mensajes_de_error_no_incluyen_el_token(response):
    with pytest.raises(LinkedInError) as error:
        publish(linkedin_client(lambda request: response))
    # Un 4xx genérico puede incluir un pedazo del cuerpo de LinkedIn, pero nunca nuestros headers:
    assert "Bearer" not in error.value.message
    if response.status_code != 401:
        assert TOKEN not in error.value.message


def test_el_cliente_no_escribe_el_token_en_los_logs(caplog):
    caplog.set_level(logging.DEBUG)
    publish(linkedin_client(LinkedInCalls()))
    with pytest.raises(LinkedInError):
        publish(linkedin_client(lambda request: httpx.Response(503)))
    assert TOKEN not in caplog.text and "Bearer" not in caplog.text


# ---------------------------------------------------------------- OAuth
def test_url_de_autorizacion():
    client = linkedin_client(LinkedInCalls())
    state = client.new_state()
    url = urlparse(client.authorization_url(state))
    query = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == AUTHORIZE_URL
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-id"]
    assert query["scope"] == ["openid profile w_member_social"]
    assert query["state"] == [state]
    assert query["redirect_uri"] == ["http://localhost:8000/auth/linkedin/callback"]


def test_el_state_es_de_un_solo_uso():
    client = linkedin_client(LinkedInCalls())
    state = client.new_state()
    assert client.consume_state(state) is True
    assert client.consume_state(state) is False
    assert client.consume_state("inventado") is False


def test_configured_necesita_id_y_secreto():
    assert linkedin_client(LinkedInCalls()).configured is True
    assert linkedin_client(LinkedInCalls(), client_secret="  ").configured is False


def test_canjear_el_codigo_y_leer_el_perfil():
    def handler(request):
        if request.url.path == "/oauth/v2/accessToken":
            assert parse_qs(request.content.decode())["code"] == ["el-codigo"]
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 5184000})
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json={"sub": "abc123", "name": "Franco"})

    client = linkedin_client(handler)
    grant = client.exchange_code("el-codigo")
    assert (grant.access_token, grant.expires_in) == (TOKEN, 5184000)
    profile = client.fetch_profile(grant.access_token)
    assert (profile.sub, profile.name) == ("abc123", "Franco")


def test_errores_de_oauth_no_repiten_secretos():
    client = linkedin_client(lambda request: httpx.Response(400, text="client_secret=client-secret mal"))
    with pytest.raises(LinkedInError) as error:
        client.exchange_code("x")
    assert "client-secret" not in error.value.message


# ---------------------------------------------------------------- antigüedad de la versión
@pytest.mark.parametrize(
    ("version", "today", "expected"),
    [
        ("202610", date(2026, 10, 15), 0),
        ("202610", date(2026, 11, 10), 1),
        ("202610", date(2027, 8, 31), 10),
        ("202610", date(2027, 9, 1), 11),
        ("202511", date(2026, 11, 10), 12),
        ("202612", date(2026, 11, 10), -1),  # una versión "del futuro" no rompe nada
    ],
)
def test_antiguedad_de_la_version_en_meses(version, today, expected):
    assert api_version_age_months(version, today) == expected
