"""El scheduler avisa cuando un post pasa a "Publicar a mano" (ready). Postgres real, LinkedIn y notificador simulados."""
from datetime import datetime, timedelta, timezone


from app.modules.content.models import PostStatus
from app.modules.content.publishing import PublishingService
from app.modules.content.repository import ContentRepository
from tests.helpers import TZ, LinkedInCalls, future_expiry, linkedin_client

URL = "http://127.0.0.1:8000/#calendario"


class FakeNotifier:
    def __init__(self, on_notify=None):
        self.calls: list[tuple[str, str, str | None]] = []
        self.on_notify = on_notify

    def notify(self, title, body, url=None):
        self.calls.append((title, body, url))
        if self.on_notify:
            self.on_notify()


def add_post(session_factory, text="Un post", status=PostStatus.APPROVED, minutes_ago=5) -> int:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    with session_factory() as s:
        post = ContentRepository(s).add_post(text, when, status, None)
        s.commit()
        return post.id


def connect(session_factory):
    with session_factory() as s:
        ContentRepository(s).upsert_settings(
            {"li_token": "tok", "li_sub": "abc", "li_name": "Franco", "li_expires_at": future_expiry()}
        )
        s.commit()


def set_notify(session_factory, value: str):
    with session_factory() as s:
        ContentRepository(s).upsert_settings({"notify_ready": value})
        s.commit()


def status_of(session_factory, post_id):
    with session_factory() as s:
        return ContentRepository(s).get_post(post_id).status


def tick(session_factory, notifier, max_late=timedelta(hours=12)):
    with session_factory() as s:
        PublishingService(s, linkedin_client(LinkedInCalls()), TZ, max_late, notifier=notifier, notify_url=URL).publish_due_posts()


def test_un_post_que_queda_para_publicar_a_mano_dispara_un_aviso(session_factory):
    post_id = add_post(session_factory, text="Hoy migré un cron viejo a una cola con Redis")  # sin LinkedIn conectado
    notifier = FakeNotifier()

    tick(session_factory, notifier)

    assert status_of(session_factory, post_id) == PostStatus.READY
    assert notifier.calls == [("Post para publicar a mano", "Hoy migré un cron viejo a una cola con Redis", URL)]


def test_varios_posts_del_mismo_ciclo_son_un_solo_aviso(session_factory):
    for n in range(3):
        add_post(session_factory, text=f"post {n}", minutes_ago=10 + n)
    notifier = FakeNotifier()

    tick(session_factory, notifier)

    assert len(notifier.calls) == 1
    title, body, url = notifier.calls[0]
    assert title == "3 posts para publicar a mano"
    assert body.count("\n") == 2 and url == URL


def test_un_post_muy_atrasado_tambien_avisa(session_factory):
    connect(session_factory)
    add_post(session_factory, minutes_ago=13 * 60)  # MAX_LATE = 12 h
    notifier = FakeNotifier()

    tick(session_factory, notifier)

    assert [c[0] for c in notifier.calls] == ["Post para publicar a mano"]


def test_si_se_publica_solo_no_hay_aviso(session_factory):
    connect(session_factory)
    post_id = add_post(session_factory)
    notifier = FakeNotifier()

    tick(session_factory, notifier)

    assert status_of(session_factory, post_id) == PostStatus.PUBLISHED
    assert notifier.calls == []


def test_solo_avisa_por_los_que_quedaron_para_publicar_a_mano(session_factory):
    connect(session_factory)
    add_post(session_factory, text="este sale solo", minutes_ago=5)
    add_post(session_factory, text="este quedó atrasado", minutes_ago=13 * 60)
    notifier = FakeNotifier()

    tick(session_factory, notifier)

    assert notifier.calls == [("Post para publicar a mano", "este quedó atrasado", URL)]


def test_sin_posts_para_publicar_a_mano_no_avisa(session_factory):
    add_post(session_factory, minutes_ago=-60)  # es en el futuro
    notifier = FakeNotifier()
    tick(session_factory, notifier)
    assert notifier.calls == []


def test_el_interruptor_apagado_no_avisa_pero_el_post_igual_pasa_a_ready(session_factory):
    set_notify(session_factory, "false")
    post_id = add_post(session_factory)
    notifier = FakeNotifier()

    tick(session_factory, notifier)

    assert status_of(session_factory, post_id) == PostStatus.READY
    assert notifier.calls == []


def test_el_interruptor_encendido_explicito_avisa(session_factory):
    set_notify(session_factory, "true")
    add_post(session_factory)
    notifier = FakeNotifier()
    tick(session_factory, notifier)
    assert len(notifier.calls) == 1


def test_si_el_notificador_falla_el_scheduler_sigue_y_el_estado_queda_guardado(session_factory):
    first = add_post(session_factory, minutes_ago=10)
    second = add_post(session_factory, minutes_ago=5)

    def boom():
        raise RuntimeError("no hay servidor de notificaciones")

    tick(session_factory, FakeNotifier(on_notify=boom))  # no debe lanzar

    assert status_of(session_factory, first) == PostStatus.READY
    assert status_of(session_factory, second) == PostStatus.READY


def test_el_aviso_sale_despues_del_commit(session_factory):
    post_id = add_post(session_factory)
    seen = []
    notifier = FakeNotifier(on_notify=lambda: seen.append(status_of(session_factory, post_id)))  # otra sesión

    tick(session_factory, notifier)

    assert seen == [PostStatus.READY]  # cuando suena el aviso, el cambio ya está guardado y visible


def test_sin_notificador_todo_sigue_igual(session_factory):
    post_id = add_post(session_factory)
    with session_factory() as s:
        PublishingService(s, linkedin_client(LinkedInCalls()), TZ, timedelta(hours=12)).publish_due_posts()
    assert status_of(session_factory, post_id) == PostStatus.READY


def test_el_aviso_no_deja_una_transaccion_abierta(session_factory):
    add_post(session_factory)
    with session_factory() as s:
        seen = []
        notifier = FakeNotifier(on_notify=lambda: seen.append(s.in_transaction()))
        PublishingService(s, linkedin_client(LinkedInCalls()), TZ, timedelta(hours=12), notifier=notifier).publish_due_posts()
    assert seen == [False]
