"""Publicación en LinkedIn y conexión con la cuenta. Es un service más del módulo content.

LA REGLA QUE IMPORTA: nunca publicar dos veces el mismo post. Cada publicación pasa por estos pasos, y cada uno
es su propia transacción:

  1. RECLAMAR  Se bloquea el post (SELECT ... FOR UPDATE SKIP LOCKED), se pasa a "publishing" y se hace COMMIT.
               Si dos procesos intentan reclamar a la vez, uno solo lo consigue.
  2. LLAMAR    Recién con el commit hecho se llama a LinkedIn. Sin transacción abierta mientras se espera.
  3. ANOTAR    En una transacción nueva se marca el resultado, solo si el post sigue en "publishing":
                 - LinkedIn aceptó (2xx)                          -> "published"
                 - LinkedIn rechazó (4xx): seguro NO se publicó   -> "failed"   (el usuario puede reintentar)
                 - timeout / conexión / 5xx: NO SABEMOS           -> sigue en "publishing" con el aviso
                                                                     "verificá en LinkedIn"

Un post que queda en "publishing" NUNCA se reintenta solo. Si la app se cae entre el paso 2 y el 3, el post queda
en "publishing" (nunca vuelve a "approved"), así que el scheduler no lo toca y no hay duplicados.
El usuario lo resuelve a mano: mira en LinkedIn y lo marca como publicado, o lo reprograma.

Reglas de esta capa (igual que service.py): no conoce FastAPI, no hace consultas (usa el repository) y maneja las
transacciones. Los tokens de LinkedIn nunca se escriben en logs ni en mensajes de error.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.integrations.linkedin.client import LinkedInClient, PublishResult, api_version_age_months
from app.integrations.linkedin.exceptions import (
    LinkedInError,
    LinkedInOutcomeUnknownError,
    LinkedInRejectedError,
)
from app.modules.content.exceptions import (
    EmptyPostError,
    InvalidOAuthStateError,
    LinkedInAuthorizationDeniedError,
    LinkedInNotConfiguredError,
    LinkedInNotConnectedError,
    PostAlreadyPublishedError,
    PostBeingPublishedError,
    PostNotFoundError,
)
from app.modules.content.models import PostStatus
from app.modules.content.notifications import build_manual_publish_message
from app.modules.content.repository import ContentRepository, PostRow
from app.shared.notifications.system import Notifier

logger = logging.getLogger(__name__)

# Los datos de la conexión con LinkedIn viven en la tabla settings, con estas claves.
# NO están en EDITABLE_SETTINGS (service.py): por eso nunca salen por GET /settings.
LINKEDIN_KEYS = ["li_token", "li_sub", "li_name", "li_expires_at"]
# Interruptor de las notificaciones (Ajustes). Es un ajuste editable: vive en la tabla settings como "true"/"false".
NOTIFY_READY_KEY = "notify_ready"

API_VERSION_WARN_MONTHS = 10  # LinkedIn da de baja cada versión al año: se avisa con más de 10 meses
MAX_POSTS_PER_TICK = 20  # tope de seguridad por pasada del scheduler

PUBLISHING_NOTE = (
    "Publicando en LinkedIn. Si el post queda así, no se sabe si salió: "
    "verificá en LinkedIn antes de volver a intentarlo."
)
LATE_NOTE = "Se pasó la hora mientras la app estaba cerrada. Revisalo y publicalo a mano o reprogramalo."
NOT_CONNECTED_NOTE = "LinkedIn no está conectado: copiá el texto y publicalo a mano."


@dataclass(frozen=True)
class LinkedInStatus:
    configured: bool
    connected: bool
    name: str
    expires_at: datetime | None
    api_version: str
    api_version_age_months: int
    api_version_outdated: bool


@dataclass(frozen=True)
class _Connection:
    token: str
    person_id: str
    name: str
    expires_at: datetime


@dataclass(frozen=True)
class _Claim:
    """Lo que hace falta para llamar a LinkedIn después de reclamar el post (valores simples, sin objetos de la base)."""

    post_id: int
    text: str
    token: str
    person_id: str


class PublishingService:
    def __init__(
        self,
        session: Session,
        linkedin: LinkedInClient,
        tz: ZoneInfo,
        max_late: timedelta,
        now: Callable[[], datetime] | None = None,
        notifier: Notifier | None = None,
        notify_url: str | None = None,
    ):
        self.session = session  # solo para commit/rollback; las consultas las hace el repository
        self.repo = ContentRepository(session)
        self.linkedin = linkedin
        self.tz = tz
        self.max_late = max_late
        self._now = now or (lambda: datetime.now(tz))
        self.notifier = notifier  # None = sin avisos (por ejemplo, en los requests del panel)
        self.notify_url = notify_url  # a dónde lleva el aviso (en Windows se abre al tocarlo)
        self._became_manual: list[str] = []  # textos de los posts que esta pasada dejó en "ready"

    # ================================================================== conexión con LinkedIn
    def get_linkedin_status(self) -> LinkedInStatus:
        stored = self.repo.get_settings_map(keys=LINKEDIN_KEYS)
        age = api_version_age_months(self.linkedin.api_version, self._now().date())
        return LinkedInStatus(
            configured=self.linkedin.configured,
            connected=self._connection_from(stored) is not None,
            name=stored.get("li_name", ""),
            expires_at=self._parse_expiry(stored.get("li_expires_at", "")),
            api_version=self.linkedin.api_version,
            api_version_age_months=age,
            api_version_outdated=age > API_VERSION_WARN_MONTHS,
        )

    def start_login(self) -> str:
        """URL de LinkedIn a la que hay que mandar al usuario para que autorice la publicación."""
        if not self.linkedin.configured:
            raise LinkedInNotConfiguredError()
        return self.linkedin.authorization_url(self.linkedin.new_state())

    def finish_login(self, code: str, state: str, error: str = "", error_description: str = "") -> None:
        """Vuelta de LinkedIn: canjea el código por el permiso y lo guarda. El permiso es solo para publicar:
        no identifica usuarios de la app (no hay usuarios)."""
        if error:
            raise LinkedInAuthorizationDeniedError(f"LinkedIn rechazó la conexión: {error_description or error}")
        if not self.linkedin.consume_state(state):
            raise InvalidOAuthStateError()
        # Las dos llamadas HTTP van ANTES de tocar la base: no hay transacción abierta mientras se espera.
        grant = self.linkedin.exchange_code(code)
        profile = self.linkedin.fetch_profile(grant.access_token)
        expires_at = self._now() + timedelta(seconds=grant.expires_in)
        self.repo.upsert_settings(
            {
                "li_token": grant.access_token,
                "li_sub": profile.sub,
                "li_name": profile.name,
                "li_expires_at": expires_at.isoformat(timespec="minutes"),  # con offset de zona horaria
            }
        )
        self.session.commit()

    def disconnect(self) -> None:
        self.repo.upsert_settings({key: "" for key in LINKEDIN_KEYS})
        self.session.commit()

    # ================================================================== publicación
    def publish_due_posts(self) -> None:
        """Lo que hace el scheduler cada minuto: publica los posts programados cuya hora ya llegó.

        Nunca lanza: si una publicación falla, se registra y se sigue con la próxima.
        Al final, si algún post quedó "para publicar a mano" (ready), avisa UNA vez por toda la pasada."""
        self._became_manual = []
        try:
            self._publish_due_posts()
        finally:
            self._notify_manual(self._became_manual)

    def _publish_due_posts(self) -> None:
        for _ in range(MAX_POSTS_PER_TICK):
            try:
                claim, progressed = self._claim_next_due()
            except Exception:  # noqa: BLE001 - un fallo de base de datos no puede tumbar al scheduler
                logger.exception("No pude reclamar el siguiente post programado")
                self.session.rollback()
                return
            if not progressed:
                return
            if claim is not None:
                try:
                    self._publish_claimed(claim)
                except Exception:  # noqa: BLE001
                    # Casi seguro falló el paso 3 (anotar el resultado). El post sigue en "publishing" a propósito:
                    # no se reintenta solo. Ver la explicación al principio del archivo.
                    logger.exception(
                        "No pude anotar el resultado del post %s: queda en 'publishing', verificar en LinkedIn",
                        claim.post_id,
                    )
                    self.session.rollback()

    def _notify_manual(self, texts: list[str]) -> None:
        """Aviso de "hay posts para publicar a mano". Se llama DESPUÉS de los commits (el estado ya quedó guardado)
        y nunca lanza: si falla el aviso, el scheduler sigue como si nada."""
        if not texts or self.notifier is None:
            return
        try:
            enabled = self.repo.get_settings_map(keys=[NOTIFY_READY_KEY]).get(NOTIFY_READY_KEY, "true") != "false"
            self.session.rollback()  # fue una lectura: se cierra la transacción
            if enabled:
                title, body = build_manual_publish_message(texts)
                self.notifier.notify(title, body, self.notify_url)  # vuelve enseguida: el trabajo va en otro hilo
        except Exception:  # noqa: BLE001
            logger.warning("No pude avisar que hay posts para publicar a mano", exc_info=True)

    def publish_post_now(self, post_id: int) -> PostRow:
        """Botón "Publicar ahora". Usa el mismo camino de reclamo que el scheduler."""
        claim = self._claim_manual(post_id)
        failure = self._publish_claimed(claim)
        if failure is not None:
            raise failure  # 502 con el motivo (el estado del post ya quedó anotado)
        row = self.repo.get_post_row(post_id)
        assert row is not None
        return row

    # ------------------------------------------------------------------ paso 1: reclamar
    def _claim_next_due(self) -> tuple[_Claim | None, bool]:
        """Reclama el post programado más viejo cuya hora llegó, en una transacción.

        Devuelve (claim, hubo_progreso). Si el post no se puede publicar (muy atrasado, LinkedIn sin conectar,
        texto vacío) se lo pasa al estado que corresponde y se devuelve claim=None, progreso=True: así el
        bucle sigue con el siguiente y nunca vuelve a ver el mismo post."""
        now = self._now()
        post = self.repo.lock_next_due_post(now)
        if post is None:
            self.session.rollback()
            return None, False

        if now - post.scheduled_at > self.max_late:  # la compu estuvo apagada: que decida el usuario
            self._set_and_commit(post, PostStatus.READY, LATE_NOTE)
            self._became_manual.append(post.text)
            return None, True
        connection = self._read_connection()
        if connection is None:
            self._set_and_commit(post, PostStatus.READY, NOT_CONNECTED_NOTE)
            self._became_manual.append(post.text)
            return None, True
        if not post.text.strip():
            self._set_and_commit(post, PostStatus.FAILED, EmptyPostError.default_message)
            return None, True

        self._set_and_commit(post, PostStatus.PUBLISHING, PUBLISHING_NOTE)
        return _Claim(post.id, post.text, connection.token, connection.person_id), True

    def _claim_manual(self, post_id: int) -> _Claim:
        try:
            if self.repo.get_post(post_id) is None:
                raise PostNotFoundError()
            connection = self._read_connection()
            if connection is None:
                raise LinkedInNotConnectedError()
            post = self.repo.lock_post(post_id)
            if post is None:  # existe pero otra transacción lo tiene bloqueado: lo está tomando el scheduler
                raise PostBeingPublishedError()
            if post.status == PostStatus.PUBLISHED:
                raise PostAlreadyPublishedError()
            if post.status == PostStatus.PUBLISHING:
                raise PostBeingPublishedError()
            if not post.text.strip():
                raise EmptyPostError()
            self._set_and_commit(post, PostStatus.PUBLISHING, PUBLISHING_NOTE)
            return _Claim(post.id, post.text, connection.token, connection.person_id)
        except Exception:
            self.session.rollback()
            raise

    def _set_and_commit(self, post, status: PostStatus, error: str | None) -> None:
        self.repo.update_post(post, {"status": status, "error": error})
        self.session.commit()

    # ------------------------------------------------------------------ pasos 2 y 3: llamar y anotar
    def _publish_claimed(self, claim: _Claim) -> LinkedInError | None:
        """Llama a LinkedIn con el post ya reclamado (commit hecho: no hay transacción abierta) y anota el
        resultado. Devuelve None si se publicó, o el error si no."""
        failure: LinkedInError | None = None
        result: PublishResult | None = None
        try:
            result = self.linkedin.publish_text(claim.token, claim.person_id, claim.text)
        except LinkedInRejectedError as exc:  # 4xx: seguro no se publicó
            failure = exc
            self._mark_failed(claim.post_id, exc.message)
            return failure
        except LinkedInOutcomeUnknownError as exc:  # timeout / conexión / 5xx: no sabemos
            failure = exc
        except Exception:  # noqa: BLE001 - algo inesperado: ante la duda, se asume "no sabemos"
            logger.exception("Error inesperado al llamar a LinkedIn (post %s)", claim.post_id)
            failure = LinkedInOutcomeUnknownError(
                "Hubo un error inesperado al publicar. No sé si el post salió: verificá en LinkedIn antes de reintentar."
            )

        if result is not None:
            # LinkedIn aceptó el post. Pase lo que pase al anotarlo, este paso NO debe terminar en "failed"
            # (el post existe): si falla, el error sube y el post queda en "publishing".
            self._mark_published(claim.post_id, result)
            return None

        self._mark_unverified(claim.post_id, failure.message)
        return failure

    def _mark_published(self, post_id: int, result: PublishResult) -> None:
        self._finish(
            post_id,
            {"status": PostStatus.PUBLISHED, "error": None, "linkedin_url": result.url, "published_at": self._now()},
        )

    def _mark_failed(self, post_id: int, message: str) -> None:
        self._finish(post_id, {"status": PostStatus.FAILED, "error": message})

    def _mark_unverified(self, post_id: int, message: str) -> None:
        """El post sigue en 'publishing' (no cambia el estado), solo se actualiza el aviso."""
        self._finish(post_id, {"error": message})

    def _finish(self, post_id: int, fields: dict) -> None:
        """Anota el resultado en una transacción nueva, SOLO si el post sigue en 'publishing'.
        Si el usuario lo cambió a mano mientras tanto (por ejemplo lo marcó como publicado), no se pisa."""
        post = self.repo.lock_post_with_status(post_id, PostStatus.PUBLISHING)
        if post is None:
            logger.warning("El post %s cambió mientras se publicaba: no se pisa su estado.", post_id)
            self.session.rollback()
            return
        self.repo.update_post(post, fields)
        self.session.commit()

    # ------------------------------------------------------------------ conexión (lectura)
    def _read_connection(self) -> _Connection | None:
        return self._connection_from(self.repo.get_settings_map(keys=LINKEDIN_KEYS))

    def _connection_from(self, stored: dict[str, str]) -> _Connection | None:
        """Conectado = hay permiso guardado y todavía no venció."""
        token, person_id = stored.get("li_token", ""), stored.get("li_sub", "")
        expires_at = self._parse_expiry(stored.get("li_expires_at", ""))
        if not (token and person_id and expires_at) or expires_at <= self._now():
            return None
        return _Connection(token, person_id, stored.get("li_name", ""), expires_at)

    def _parse_expiry(self, value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=self.tz)
