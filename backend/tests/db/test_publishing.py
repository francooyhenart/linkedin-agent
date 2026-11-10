"""Publicación en LinkedIn contra Postgres real, con LinkedIn simulado a nivel HTTP.

Lo que más importa acá: NUNCA publicar dos veces el mismo post, pase lo que pase.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.modules.content.exceptions import (
    EmptyPostError,
    LinkedInNotConnectedError,
    PostAlreadyPublishedError,
    PostBeingPublishedError,
    PostNotFoundError,
)
from app.modules.content.models import PostStatus
from app.modules.content.publishing import PUBLISHING_NOTE, PublishingService
from app.modules.content.repository import ContentRepository
from app.integrations.linkedin.exceptions import LinkedInOutcomeUnknownError, LinkedInRejectedError
from tests.helpers import TZ, LinkedInCalls, future_expiry, linkedin_client

TOKEN = "AQX-token-super-secreto-123"
MAX_LATE = timedelta(hours=12)


# ---------------------------------------------------------------- armado
def connect_linkedin(session_factory, expires: str | None = None):
    with session_factory() as s:
        ContentRepository(s).upsert_settings(
            {"li_token": TOKEN, "li_sub": "abc123", "li_name": "Franco", "li_expires_at": expires or future_expiry()}
        )
        s.commit()


def add_post(session_factory, *, text="Un post", status=PostStatus.APPROVED, minutes_ago=5) -> int:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    with session_factory() as s:
        post = ContentRepository(s).add_post(text, when, status, None)
        s.commit()
        return post.id


def state_of(session_factory, post_id) -> tuple[PostStatus, str | None, str | None]:
    with session_factory() as s:
        post = ContentRepository(s).get_post(post_id)
        return post.status, post.error, post.linkedin_url


def tick(session_factory, calls: LinkedInCalls, max_late=MAX_LATE) -> None:
    """Una pasada del scheduler: sesión propia y service nuevo, como en producción."""
    with session_factory() as s:
        PublishingService(s, linkedin_client(calls), TZ, max_late).publish_due_posts()


def manual(session_factory, calls: LinkedInCalls, post_id):
    with session_factory() as s:
        return PublishingService(s, linkedin_client(calls), TZ, MAX_LATE).publish_post_now(post_id)


# ---------------------------------------------------------------- camino feliz y reglas del scheduler
def test_publica_un_post_programado_cuya_hora_llego(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory)
    calls = LinkedInCalls()

    tick(session_factory, calls)

    status, error, url = state_of(session_factory, post_id)
    assert (status, error) == (PostStatus.PUBLISHED, None)
    assert url == "https://www.linkedin.com/feed/update/urn:li:share:123/"
    assert calls.count == 1
    with session_factory() as s:
        assert ContentRepository(s).get_post(post_id).published_at is not None


def test_no_toca_lo_que_todavia_no_es_su_hora_ni_lo_que_no_esta_programado(session_factory):
    connect_linkedin(session_factory)
    futuro = add_post(session_factory, minutes_ago=-60)
    borrador = add_post(session_factory, status=PostStatus.DRAFT)
    calls = LinkedInCalls()

    tick(session_factory, calls)

    assert calls.count == 0
    assert state_of(session_factory, futuro)[0] == PostStatus.APPROVED
    assert state_of(session_factory, borrador)[0] == PostStatus.DRAFT


def test_muy_atrasado_pasa_a_ready_sin_llamar_a_linkedin(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory, minutes_ago=13 * 60)  # MAX_LATE_HOURS es 12
    calls = LinkedInCalls()

    tick(session_factory, calls)

    status, error, _ = state_of(session_factory, post_id)
    assert status == PostStatus.READY and "Se pasó la hora" in error
    assert calls.count == 0


def test_justo_dentro_del_limite_de_atraso_si_se_publica(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory, minutes_ago=11 * 60 + 59)
    tick(session_factory, LinkedInCalls())
    assert state_of(session_factory, post_id)[0] == PostStatus.PUBLISHED


def test_sin_linkedin_conectado_pasa_a_ready(session_factory):
    post_id = add_post(session_factory)
    calls = LinkedInCalls()
    tick(session_factory, calls)
    status, error, _ = state_of(session_factory, post_id)
    assert status == PostStatus.READY and "LinkedIn no está conectado" in error
    assert calls.count == 0


def test_permiso_vencido_cuenta_como_no_conectado(session_factory):
    connect_linkedin(session_factory, expires=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    post_id = add_post(session_factory)
    tick(session_factory, LinkedInCalls())
    assert state_of(session_factory, post_id)[0] == PostStatus.READY


def test_post_vacio_pasa_a_failed(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory, text="   ")
    calls = LinkedInCalls()
    tick(session_factory, calls)
    status, error, _ = state_of(session_factory, post_id)
    assert (status, error) == (PostStatus.FAILED, "El post está vacío.")
    assert calls.count == 0


def test_publica_varios_en_orden_y_uno_que_falla_no_frena_a_los_demas(session_factory):
    connect_linkedin(session_factory)
    primero = add_post(session_factory, text="rechazado", minutes_ago=30)
    segundo = add_post(session_factory, text="ok", minutes_ago=20)
    calls = LinkedInCalls(
        lambda request: httpx.Response(400, json={})
        if b"rechazado" in request.content
        else httpx.Response(201, headers={"x-restli-id": "urn:li:share:2"})
    )

    tick(session_factory, calls)

    assert state_of(session_factory, primero)[0] == PostStatus.FAILED
    assert state_of(session_factory, segundo)[0] == PostStatus.PUBLISHED
    assert calls.count == 2


# ---------------------------------------------------------------- el reclamo se confirma ANTES de llamar a LinkedIn
def test_el_post_ya_esta_en_publishing_y_commiteado_cuando_se_llama_a_linkedin(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory)
    seen = {}

    def handler(request):
        # Se anota en un dict (no se hace assert acá: una excepción dentro del handler la tragaría el service).
        with session_factory() as other:  # OTRA sesión: solo ve lo que ya está commiteado
            seen["status_visto_desde_afuera"] = ContentRepository(other).get_post(post_id).status
        seen["transaccion_abierta"] = service_session.in_transaction()
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"})

    with session_factory() as service_session:
        PublishingService(service_session, linkedin_client(handler), TZ, MAX_LATE).publish_due_posts()

    assert seen["status_visto_desde_afuera"] == PostStatus.PUBLISHING
    assert seen["transaccion_abierta"] is False


# ---------------------------------------------------------------- 4xx: seguro no se publicó -> failed
@pytest.mark.parametrize(
    ("status_code", "fragment"),
    [(401, "Reconectá LinkedIn"), (403, "w_member_social"), (426, "LINKEDIN_API_VERSION"), (422, "422")],
)
def test_un_4xx_deja_el_post_en_failed_con_un_mensaje_claro(session_factory, status_code, fragment):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory)
    calls = LinkedInCalls(lambda request: httpx.Response(status_code, json={"code": "X"}))

    tick(session_factory, calls)

    status, error, url = state_of(session_factory, post_id)
    assert status == PostStatus.FAILED and fragment in error and url is None


# ---------------------------------------------------------------- resultado desconocido -> publishing, nunca se reintenta
def read_timeout(request):
    raise httpx.ReadTimeout("sin respuesta", request=request)


def connection_reset(request):
    raise httpx.ReadError("conexión reiniciada", request=request)


@pytest.mark.parametrize(
    "respond",
    [
        read_timeout,
        connection_reset,
        lambda request: httpx.Response(500),
        lambda request: httpx.Response(503),
        lambda request: httpx.Response(408, json={"message": "timeout"}),
        lambda request: httpx.Response(409, json={"message": "write conflict, retry"}),
    ],
    ids=["timeout", "conexion-cortada", "http-500", "http-503", "http-408", "http-409"],
)
def test_resultado_desconocido_deja_el_post_en_publishing_y_nunca_se_reintenta(session_factory, respond):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory)
    calls = LinkedInCalls(respond)

    tick(session_factory, calls)

    status, error, _ = state_of(session_factory, post_id)
    assert status == PostStatus.PUBLISHING
    assert "verificá en LinkedIn" in error
    assert calls.count == 1

    # Pasan los minutos, el scheduler corre muchas veces más... y no vuelve a llamar a LinkedIn.
    for _ in range(3):
        tick(session_factory, calls)
    assert calls.count == 1
    assert state_of(session_factory, post_id)[0] == PostStatus.PUBLISHING


def test_429_deja_el_post_en_failed_y_se_puede_reintentar(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory)
    tick(session_factory, LinkedInCalls(lambda request: httpx.Response(429, json={})))

    status, error, _ = state_of(session_factory, post_id)
    assert status == PostStatus.FAILED and "demasiados pedidos" in error

    # failed sí se puede reintentar a mano (no hay duda de que no salió)
    row = manual(session_factory, LinkedInCalls(), post_id)
    assert row.post.status == PostStatus.PUBLISHED


@pytest.mark.parametrize("urn", ["urn:li:share:7000000000000000001", "urn:li:ugcPost:7000000000000000002"])
def test_si_linkedin_dice_que_el_contenido_ya_existia_se_marca_published_con_ese_urn(session_factory, urn):
    """El escenario: un intento anterior sí publicó pero no pudimos confirmarlo. Al reintentar a mano (o si el post
    se repite), LinkedIn contesta 'duplicado de <urn>': el post ya estaba publicado, se marca published con ese URN."""
    connect_linkedin(session_factory)
    post_id = add_post(session_factory)
    calls = LinkedInCalls(lambda request: httpx.Response(422, json={"message": f"Content is a duplicate of {urn}"}))

    tick(session_factory, calls)

    status, error, url = state_of(session_factory, post_id)
    assert (status, error) == (PostStatus.PUBLISHED, None)
    assert url == f"https://www.linkedin.com/feed/update/{urn}/"
    assert calls.count == 1
    tick(session_factory, calls)  # y no se vuelve a llamar
    assert calls.count == 1


def test_duplicado_tambien_funciona_con_publicar_ahora(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory, status=PostStatus.DRAFT)
    urn = "urn:li:share:42"
    calls = LinkedInCalls(lambda request: httpx.Response(422, json={"message": f"Content is a duplicate of {urn}"}))

    row = manual(session_factory, calls, post_id)

    assert row.post.status == PostStatus.PUBLISHED and row.post.linkedin_url.endswith(f"{urn}/")


def test_un_422_comun_si_deja_el_post_en_failed(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory)
    tick(session_factory, LinkedInCalls(lambda request: httpx.Response(422, json={"message": "Error validating the post"})))
    assert state_of(session_factory, post_id)[0] == PostStatus.FAILED


def test_el_timeout_simulado_no_pasa_a_failed_aunque_el_cliente_lance_la_excepcion_de_desconocido(session_factory):
    """Misma garantía vista desde el cliente: ReadTimeout -> LinkedInOutcomeUnknownError, no Rejected."""
    with pytest.raises(LinkedInOutcomeUnknownError):
        linkedin_client(LinkedInCalls(read_timeout)).publish_text(TOKEN, "abc123", "x")
    with pytest.raises(LinkedInRejectedError):
        linkedin_client(LinkedInCalls(lambda request: httpx.Response(400))).publish_text(TOKEN, "abc123", "x")


# ---------------------------------------------------------------- LA APP SE CAE entre LinkedIn y el commit
class ProcessDied(BaseException):
    """Simula que el proceso muere (kill, corte de luz): no es una Exception común y nadie la atrapa."""


@pytest.mark.parametrize("crash", [ProcessDied("kill -9"), RuntimeError("se cayó la base al anotar")], ids=["proceso-muerto", "error-de-base"])
def test_si_la_app_se_cae_despues_de_que_linkedin_publico_el_post_no_se_publica_de_nuevo(
    session_factory, monkeypatch, crash
):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory)
    calls = LinkedInCalls()  # LinkedIn contesta 201: el post SÍ salió

    def explota(self, post_id, result):
        raise crash

    monkeypatch.setattr(PublishingService, "_mark_published", explota)

    # --- la app "se cae" justo después de la respuesta de LinkedIn, antes de anotar "published"
    try:
        tick(session_factory, calls)
    except ProcessDied:
        pass  # el proceso murió: la excepción escapa, como pasaría con un kill

    assert calls.count == 1  # LinkedIn recibió el post una vez
    status, error, url = state_of(session_factory, post_id)
    assert status == PostStatus.PUBLISHING  # el reclamo ya estaba commiteado: nunca volvió a "approved"
    assert url is None  # ...y la app no llegó a saber la URL

    # --- la app se reinicia (el monkeypatch se va: ahora todo funciona normal)
    monkeypatch.undo()
    for _ in range(3):
        tick(session_factory, calls)

    assert calls.count == 1, "¡se publicó de nuevo! el scheduler no debe tocar un post en 'publishing'"
    assert state_of(session_factory, post_id)[0] == PostStatus.PUBLISHING
    assert "verificá en LinkedIn" in state_of(session_factory, post_id)[1]

    # --- tampoco lo publica el botón "Publicar ahora"
    with pytest.raises(PostBeingPublishedError):
        manual(session_factory, calls, post_id)
    assert calls.count == 1


# ---------------------------------------------------------------- SKIP LOCKED: dos procesos no reclaman el mismo post
def test_si_otra_transaccion_tiene_bloqueado_el_post_el_scheduler_lo_saltea(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory)
    calls = LinkedInCalls()

    with session_factory() as otra:  # "otro scheduler" a mitad de reclamar este post
        assert ContentRepository(otra).lock_next_due_post(datetime.now(timezone.utc)) is not None

        tick(session_factory, calls)  # este scheduler no espera ni lo toma
        assert calls.count == 0
        assert state_of_unlocked(session_factory, post_id) == PostStatus.APPROVED

        otra.rollback()  # el otro suelta el bloqueo

    tick(session_factory, calls)
    assert calls.count == 1
    assert state_of(session_factory, post_id)[0] == PostStatus.PUBLISHED


def state_of_unlocked(session_factory, post_id) -> PostStatus:
    """Lee el estado sin pedir bloqueos (un SELECT común no se frena con FOR UPDATE)."""
    return state_of(session_factory, post_id)[0]


def test_dos_schedulers_a_la_vez_publican_una_sola_vez(session_factory):
    """Con threads de verdad: ambos intentan reclamar el mismo post."""
    import threading

    connect_linkedin(session_factory)
    post_id = add_post(session_factory)
    calls = LinkedInCalls()
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        tick(session_factory, calls)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]

    assert calls.count == 1
    assert state_of(session_factory, post_id)[0] == PostStatus.PUBLISHED


# ---------------------------------------------------------------- "Publicar ahora"
def test_publicar_ahora_un_borrador(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory, status=PostStatus.DRAFT)
    calls = LinkedInCalls()

    row = manual(session_factory, calls, post_id)

    assert row.post.status == PostStatus.PUBLISHED
    assert row.post.linkedin_url.endswith("urn:li:share:123/")
    assert calls.count == 1


def test_publicar_ahora_acepta_failed_y_ready_para_reintentar(session_factory):
    connect_linkedin(session_factory)
    for status in (PostStatus.FAILED, PostStatus.READY):
        post_id = add_post(session_factory, status=status)
        assert manual(session_factory, LinkedInCalls(), post_id).post.status == PostStatus.PUBLISHED


def test_publicar_ahora_rechaza_publishing_y_published(session_factory):
    connect_linkedin(session_factory)
    calls = LinkedInCalls()
    publishing = add_post(session_factory, status=PostStatus.PUBLISHING)
    published = add_post(session_factory, status=PostStatus.PUBLISHED)

    with pytest.raises(PostBeingPublishedError):
        manual(session_factory, calls, publishing)
    with pytest.raises(PostAlreadyPublishedError):
        manual(session_factory, calls, published)
    assert calls.count == 0


def test_publicar_ahora_sin_linkedin_conectado(session_factory):
    post_id = add_post(session_factory, status=PostStatus.DRAFT)
    with pytest.raises(LinkedInNotConnectedError):
        manual(session_factory, LinkedInCalls(), post_id)


def test_publicar_ahora_post_inexistente_o_vacio(session_factory):
    connect_linkedin(session_factory)
    with pytest.raises(PostNotFoundError):
        manual(session_factory, LinkedInCalls(), 9999)
    vacio = add_post(session_factory, text="", status=PostStatus.DRAFT)
    with pytest.raises(EmptyPostError):
        manual(session_factory, LinkedInCalls(), vacio)
    assert state_of(session_factory, vacio)[0] == PostStatus.DRAFT  # no se toca


def test_publicar_ahora_con_4xx_deja_failed_y_lanza_el_error(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory, status=PostStatus.DRAFT)
    calls = LinkedInCalls(lambda request: httpx.Response(401, json={}))

    with pytest.raises(LinkedInRejectedError, match="Reconectá"):
        manual(session_factory, calls, post_id)

    assert state_of(session_factory, post_id)[0] == PostStatus.FAILED


def test_publicar_ahora_con_timeout_deja_publishing_y_lanza_el_error(session_factory):
    connect_linkedin(session_factory)
    post_id = add_post(session_factory, status=PostStatus.DRAFT)
    calls = LinkedInCalls(read_timeout)

    with pytest.raises(LinkedInOutcomeUnknownError):
        manual(session_factory, calls, post_id)

    assert state_of(session_factory, post_id)[0] == PostStatus.PUBLISHING
    with pytest.raises(PostBeingPublishedError):  # reintentar a mano también está bloqueado
        manual(session_factory, calls, post_id)
    assert calls.count == 1


# ---------------------------------------------------------------- el usuario resuelve "verificar en LinkedIn" a mano
def test_el_usuario_puede_marcar_como_publicado_un_post_que_quedo_en_publishing(session_factory):
    from app.modules.content.schemas import PostUpdate
    from app.modules.content.service import ContentService
    from tests.test_support import NoWriter

    post_id = add_post(session_factory, status=PostStatus.PUBLISHING)
    with session_factory() as s:
        row = ContentService(s, NoWriter(), TZ, "m").update_post(post_id, PostUpdate(status=PostStatus.PUBLISHED))
    assert row.post.status == PostStatus.PUBLISHED and row.post.error is None


def test_si_el_usuario_cambia_el_post_mientras_se_publica_no_se_pisa(session_factory):
    """Mientras se espera a LinkedIn el usuario lo marca como publicado a mano: el resultado no lo pisa."""
    connect_linkedin(session_factory)
    post_id = add_post(session_factory)

    def handler(request):
        with session_factory() as other:  # el usuario, desde el panel, mientras LinkedIn tarda
            post = ContentRepository(other).get_post(post_id)
            post.status = PostStatus.READY
            post.error = "cambiado a mano"
            other.commit()
        raise httpx.ReadTimeout("t", request=request)

    tick(session_factory, LinkedInCalls(handler))

    status, error, _ = state_of(session_factory, post_id)
    assert (status, error) == (PostStatus.READY, "cambiado a mano")


def test_el_estado_publishing_existe_en_la_base_y_el_aviso_es_el_esperado(session_factory):
    post_id = add_post(session_factory, status=PostStatus.PUBLISHING)
    assert state_of(session_factory, post_id)[0] == PostStatus.PUBLISHING
    assert "verificá en LinkedIn" in PUBLISHING_NOTE


# ---------------------------------------------------------------- antigüedad de la versión de la API
@pytest.mark.parametrize(
    ("today", "age", "outdated"),
    [
        (datetime(2026, 11, 10, tzinfo=TZ), 1, False),
        (datetime(2027, 8, 31, tzinfo=TZ), 10, False),  # 10 meses: todavía no avisa
        (datetime(2027, 9, 1, tzinfo=TZ), 11, True),  # más de 10: avisa
        (datetime(2027, 10, 15, tzinfo=TZ), 12, True),  # LinkedIn la da de baja al año
    ],
)
def test_status_avisa_cuando_la_version_de_la_api_tiene_mas_de_10_meses(session_factory, today, age, outdated):
    with session_factory() as s:
        service = PublishingService(s, linkedin_client(LinkedInCalls(), api_version="202610"), TZ, MAX_LATE, now=lambda: today)
        status = service.get_linkedin_status()
    assert (status.api_version, status.api_version_age_months, status.api_version_outdated) == ("202610", age, outdated)


# ---------------------------------------------------------------- los tokens no se filtran
def test_el_token_no_aparece_en_los_logs_ni_en_los_errores_guardados(session_factory, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    connect_linkedin(session_factory)
    ok = add_post(session_factory, text="uno", minutes_ago=30)
    roto = add_post(session_factory, text="dos", minutes_ago=20)
    calls = LinkedInCalls(
        lambda request: httpx.Response(503) if b"dos" in request.content else httpx.Response(201, headers={"x-restli-id": "urn:li:share:7"})
    )

    tick(session_factory, calls)

    assert TOKEN not in caplog.text
    for post_id in (ok, roto):
        assert TOKEN not in str(state_of(session_factory, post_id))
