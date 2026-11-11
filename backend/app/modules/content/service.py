"""Reglas de negocio de content: ideas, posts, métricas, ajustes, generación y planificación.

Reglas de esta capa:
- No conoce FastAPI: si algo falla lanza excepciones de dominio (exceptions.py), nunca HTTPException.
- No escribe consultas: pide todo al repository.
- Maneja la TRANSACCIÓN: cada operación que escribe termina con `commit()`. Si algo falla antes,
  no hay commit y al cerrarse la sesión todo se deshace (todo o nada).
  Por eso recibe la sesión, pero solo para eso: abrir/cerrar la transacción. Nunca consulta con ella.
- Lo externo (el modelo de lenguaje) entra por el Protocol `PostWriter`, así se puede reemplazar por un fake.
- NUNCA se queda con una transacción abierta mientras espera algo lento (el modelo): primero lee y hace commit
  (cierra la transacción), después espera, y recién después abre otra para guardar.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.exceptions import ExternalServiceError
from app.core.timeutil import format_local, parse_local
from app.modules.content import prompts, slots, text_rules
from app.modules.content.exceptions import (
    EmptyIdeaError,
    IdeaNotFoundError,
    InvalidDateError,
    InvalidSettingError,
    NoIdeasToSuggestFromError,
    NoPublishDaysError,
    NoUnusedIdeasError,
    NothingToGenerateFromError,
    NoUsefulSuggestionsError,
    PostNotFoundError,
    ReservedStatusError,
)
from app.modules.content.models import Idea, PostMetric, PostStatus
from app.modules.content.publishing import NOTIFY_READY_KEY
from app.modules.content.repository import ContentRepository, PostRow
from app.modules.content.schemas import (
    GenerateIn,
    IdeaCreate,
    MetricsIn,
    PostCreate,
    PostUpdate,
    SettingsUpdate,
)
from app.shared.llm.ollama import LLMStatus

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # intentos de generación si el texto no pasa los chequeos
SUGGESTION_SOURCE_LIMIT = 20  # cuántas ideas del banco se le muestran al modelo para sugerir preguntas

# Ajustes que el usuario puede ver y editar. Los tokens de LinkedIn (li_*) viven en la misma tabla
# pero NO están en esta lista, así que nunca salen por la API de ajustes.
EDITABLE_SETTINGS = ["post_days", "post_time", "max_chars", "model", "style_examples", NOTIFY_READY_KEY]
DEFAULT_SETTINGS = {
    "post_days": "1,3",
    "post_time": "09:00",
    "max_chars": "300",
    "style_examples": "",
    NOTIFY_READY_KEY: "true",  # avisar con una notificación del sistema cuando un post queda para publicar a mano
}


class PostWriter(Protocol):
    """Lo que el service necesita de un "escritor": recibe los prompts y devuelve el texto crudo.

    La implementación real es `OllamaPostWriter` (writer.py); los tests usan otras. Es un Protocol: cualquier
    clase con estos métodos sirve, sin heredar de nada.
    """

    def write(self, system: str, user: str, model: str) -> str: ...

    def status(self) -> LLMStatus: ...


@dataclass
class UserSettings:
    post_days: str
    post_time: str
    max_chars: int
    model: str
    style_examples: str
    banned: list[str]
    notify_ready: bool


@dataclass
class GenerationResult:
    text: str
    warnings: list[str]


@dataclass
class IdeaSuggestion:
    """Una pregunta sugerida y la idea del banco sobre la que se hizo."""

    question: str
    idea_id: int
    idea_note: str


@dataclass
class FailedIdea:
    """Una idea para la que no se pudo armar el borrador, con el motivo."""

    idea_id: int
    reason: str


@dataclass
class PlanResult:
    created: list[PostRow]
    failed: list[FailedIdea]


class _DraftNotSaved(Exception):
    """Interna: un borrador no se pudo guardar. El mensaje es el motivo que ve el usuario."""


class ContentService:
    def __init__(
        self,
        session: Session,
        writer: PostWriter,
        tz: ZoneInfo,
        default_model: str,
        now: Callable[[], datetime] | None = None,
    ):
        self.session = session  # solo para commit(); las consultas las hace el repository
        self.repo = ContentRepository(session)
        self.writer = writer
        self.tz = tz
        self.default_model = default_model
        self._now = now or (lambda: datetime.now(tz))  # inyectable: los tests fijan la hora

    # ------------------------------------------------------------------ ajustes
    def get_user_settings(self) -> UserSettings:
        """Ajustes editables con sus valores por defecto. Trae solo las claves editables de la base."""
        stored = self.repo.get_settings_map(keys=EDITABLE_SETTINGS)
        merged = {**DEFAULT_SETTINGS, "model": self.default_model, **stored}
        return UserSettings(
            post_days=merged["post_days"],
            post_time=merged["post_time"],
            max_chars=int(merged["max_chars"]),
            model=merged["model"].strip() or self.default_model,
            style_examples=merged["style_examples"],
            banned=text_rules.BANNED,
            notify_ready=merged[NOTIFY_READY_KEY] != "false",
        )

    def update_settings(self, data: SettingsUpdate) -> UserSettings:
        values = data.model_dump(exclude_none=True)
        if "post_time" in values:
            try:
                time.fromisoformat(values["post_time"])
                valid = len(values["post_time"]) == 5
            except ValueError:
                valid = False
            if not valid:
                raise InvalidSettingError("La hora tiene que ser HH:MM.")
        if "max_chars" in values:
            values["max_chars"] = max(80, min(3000, values["max_chars"]))
        if "model" in values:
            values["model"] = values["model"].strip()
        if NOTIFY_READY_KEY in values:
            values[NOTIFY_READY_KEY] = "true" if values[NOTIFY_READY_KEY] else "false"
        self.repo.upsert_settings({key: str(value) for key, value in values.items()})
        self.session.commit()
        return self.get_user_settings()

    # ------------------------------------------------------------------ ideas
    def list_ideas(self) -> list[Idea]:
        return self.repo.list_ideas()

    def create_idea(self, data: IdeaCreate) -> Idea:
        note = data.note.strip()
        if not note:
            raise EmptyIdeaError()
        idea = self.repo.add_idea(note)
        self.session.commit()
        return idea

    def delete_idea(self, idea_id: int) -> None:
        self.repo.delete_idea(idea_id)  # si no existía, no pasa nada (igual que antes)
        self.session.commit()

    def suggest_ideas(self) -> list[IdeaSuggestion]:
        """Propone preguntas para recordar experiencias, a partir del banco de ideas. NO guarda nada.

        Lee las ideas, cierra la transacción (el modelo puede tardar) y recién después le pregunta al modelo.
        Cada sugerencia queda atada a la idea de la que sale: lo que el modelo diga sin apuntar a una idea
        real se descarta (ver prompts.parse_suggestions), así no llegan temas genéricos ni inventados."""
        settings = self.get_user_settings()
        # Las sin usar primero (list_ideas ya las ordena así) y un tope, para que el prompt no crezca sin límite.
        source = [(idea.id, idea.note) for idea in self.repo.list_ideas()[:SUGGESTION_SOURCE_LIMIT]]
        if not source:
            raise NoIdeasToSuggestFromError()
        self.session.commit()  # cierra la transacción de lectura antes de esperar al modelo

        system = prompts.build_suggestions_system_prompt()
        user = prompts.build_suggestions_user_prompt([note for _, note in source])
        for _ in range(MAX_ATTEMPTS):
            raw = self.writer.write(system, user, settings.model)
            parsed = prompts.parse_suggestions(raw, note_count=len(source))
            if parsed:  # con alguna válida alcanza: mejor 3 buenas que insistir hasta tener 5
                return [IdeaSuggestion(question, *source[number - 1]) for number, question in parsed]
        raise NoUsefulSuggestionsError()

    # ------------------------------------------------------------------ posts
    def list_posts(
        self, start: str | None = None, end: str | None = None, status: PostStatus | None = None
    ) -> list[PostRow]:
        return self.repo.list_post_rows(
            start=self._parse_when(start) if start else None,
            end=self._parse_when(end) if end else None,
            status=status,
        )

    def get_post(self, post_id: int) -> PostRow:
        row = self.repo.get_post_row(post_id)
        if row is None:
            raise PostNotFoundError()
        return row

    def create_post(self, data: PostCreate) -> PostRow:
        post = self._add_post(data)
        self.session.commit()
        return self.get_post(post.id)

    def update_post(self, post_id: int, data: PostUpdate) -> PostRow:
        post = self.repo.get_post(post_id)
        if post is None:
            raise PostNotFoundError()
        fields = data.model_dump(exclude_none=True)
        if fields.get("status") == post.status:
            # Mandar el estado que el post YA tiene no cambia nada (idempotente): ni el aviso de error ni la fecha.
            del fields["status"]
        if "status" in fields:
            if fields["status"] == PostStatus.PUBLISHING:
                raise ReservedStatusError()  # pasar A 'publishing' solo lo hace la app al reclamar un post
            if fields["status"] == PostStatus.PUBLISHED:
                fields["published_at"] = self._now()
            fields["error"] = None  # cambiar de estado a mano borra el error anterior
        if "scheduled_at" in fields:
            fields["scheduled_at"] = self._parse_when(fields["scheduled_at"])
        if "text" in fields:
            fields["text"] = fields["text"].strip()
        if fields:
            self.repo.update_post(post, fields)
            self.session.commit()
        return self.get_post(post_id)

    def delete_post(self, post_id: int) -> None:
        self.repo.delete_post(post_id)
        self.session.commit()

    # ------------------------------------------------------------------ métricas
    def record_metrics(self, post_id: int, data: MetricsIn) -> PostRow:
        """Guarda los números como snapshot. Máximo uno por día: si ya hay uno de hoy se corrige,
        así editar un número varias veces seguidas no llena la tabla de filas casi iguales."""
        if self.repo.get_post(post_id) is None:
            raise PostNotFoundError()
        now = self._now()
        counts = data.model_dump()
        latest = self.repo.latest_metric(post_id)
        if latest is not None and latest.captured_at.astimezone(self.tz).date() == now.date():
            self.repo.update_metric(latest, now, counts)
        else:
            self.repo.add_metric(post_id, now, counts)
        self.session.commit()
        return self.get_post(post_id)

    def list_metric_history(self, post_id: int) -> list[PostMetric]:
        if self.repo.get_post(post_id) is None:
            raise PostNotFoundError()
        return self.repo.list_metrics(post_id)

    # ------------------------------------------------------------------ generación
    def get_model_status(self) -> LLMStatus:
        return self.writer.status()  # es una llamada HTTP corta (3 s) y no toca la base

    def generate(self, data: GenerateIn) -> GenerationResult:
        if not data.note.strip() and not data.text.strip():
            raise NothingToGenerateFromError()
        settings = self.get_user_settings()
        self.session.commit()  # cierra la transacción de lectura: el modelo puede tardar minutos
        return self._generate(data.note, data.text, data.instruction, settings=settings)

    def _generate(
        self, note: str, text: str = "", instruction: str = "", *, settings: UserSettings
    ) -> GenerationResult:
        """Pide el texto al modelo; si no pasa los chequeos, reintenta hasta MAX_ATTEMPTS veces
        mostrándole sus errores. Devuelve el último intento (con sus avisos si no llegó a pasar).

        Recibe los ajustes ya leídos: este método NO toca la base, así que se puede llamar sin transacción abierta."""
        system = prompts.build_system_prompt(settings.max_chars, settings.style_examples)
        user = prompts.build_user_prompt(note, text, instruction)

        out, warnings = "", []
        for _ in range(MAX_ATTEMPTS):
            out = text_rules.clean(self.writer.write(system, user, settings.model))
            warnings = text_rules.check_text(out, settings.max_chars)
            if not warnings:
                break
            user = prompts.add_feedback(user, warnings, out)
        return GenerationResult(text=out, warnings=warnings)

    # ------------------------------------------------------------------ planificación
    def plan_next_posts(self) -> PlanResult:
        """Toma las ideas sin usar (tantas como días de publicación por semana) y arma un borrador
        por cada una en los próximos días libres. En tres fases, para no dejar una transacción abierta
        mientras se espera al modelo:

        A) LEER: ajustes, ideas y días ocupados. Se hace commit y la transacción queda cerrada.
        B) GENERAR: el modelo escribe cada borrador (lento). No hay transacción abierta ni se toca la base.
        C) GUARDAR: cada borrador en SU PROPIA transacción, revalidando antes (pasó tiempo: la idea pudo
           borrarse o usarse, el día pudo ocuparse). Cada post se guarda completo o no se guarda.

        Lo que salió bien se guarda; lo que falló vuelve en `failed` con el motivo, y esa idea queda sin usar.
        """
        # --- A) leer
        settings = self.get_user_settings()
        days = slots.parse_post_days(settings.post_days)
        if not days:
            raise NoPublishDaysError()
        ideas = self.repo.list_unused_ideas(limit=len(days))
        if not ideas:
            raise NoUnusedIdeasError()
        taken = {when.astimezone(self.tz).date() for when in self.repo.list_scheduled_times()}
        free = slots.next_slots(
            count=len(ideas),
            days=days,
            at=time.fromisoformat(settings.post_time),
            taken_dates=taken,
            now=self._now(),
        )
        # Valores simples (no objetos de la base): después del commit ya no los vamos a consultar.
        work = [(idea.id, idea.note, when) for idea, when in zip(ideas, free)]  # si faltan días, alcanzan menos ideas
        self.session.commit()

        # --- B) generar (sin transacción abierta)
        drafts: list[tuple[int, datetime, str]] = []
        failed: list[FailedIdea] = []
        for idea_id, note, when in work:
            try:
                drafts.append((idea_id, when, self._generate(note, settings=settings).text))
            except ExternalServiceError as exc:
                logger.warning("No se pudo escribir el borrador de la idea %s: %s", idea_id, exc.message)
                failed.append(FailedIdea(idea_id, exc.message))

        # --- C) guardar, uno por uno
        created_ids: list[int] = []
        for idea_id, when, text in drafts:
            try:
                created_ids.append(self._save_draft(idea_id, when, text))
            except _DraftNotSaved as exc:
                failed.append(FailedIdea(idea_id, str(exc)))
        return PlanResult(created=[self.get_post(post_id) for post_id in created_ids], failed=failed)

    def _save_draft(self, idea_id: int, when: datetime, text: str) -> int:
        """Guarda UN borrador en su propia transacción, revalidando todo. Devuelve el id del post."""
        try:
            idea = self.repo.lock_idea(idea_id)  # la bloquea: nadie más la puede usar hasta el commit
            if idea is None:
                raise _DraftNotSaved("La idea se borró mientras se escribía el borrador.")
            if idea.used:
                raise _DraftNotSaved("La idea ya se usó mientras se escribía el borrador.")
            day_start = datetime.combine(when.date(), time.min, tzinfo=self.tz)
            if self.repo.has_post_between(day_start, day_start + timedelta(days=1)):
                raise _DraftNotSaved(f"El {format_local(when, self.tz)[:10]} se ocupó mientras se escribía el borrador.")
            post = self.repo.add_post(text.strip(), when, PostStatus.DRAFT, idea_id)
            self.repo.mark_idea_used(idea)
            self.session.commit()
            return post.id
        except _DraftNotSaved:
            self.session.rollback()
            raise
        except SQLAlchemyError:
            self.session.rollback()
            logger.exception("Falló el guardado del borrador de la idea %s", idea_id)
            raise _DraftNotSaved("No se pudo guardar el borrador (error de base de datos).") from None

    # ------------------------------------------------------------------ internos
    def _add_post(self, data: PostCreate):
        """Crea el post y marca la idea como usada. No hace commit: lo hace quien lo llama."""
        if data.status == PostStatus.PUBLISHING:
            raise ReservedStatusError()
        if data.idea_id is not None:
            idea = self.repo.get_idea(data.idea_id)
            if idea is None:
                raise IdeaNotFoundError()
            self.repo.mark_idea_used(idea)
        return self.repo.add_post(
            text=data.text.strip(),
            scheduled_at=self._parse_when(data.scheduled_at),
            status=data.status,
            idea_id=data.idea_id,
        )

    def _parse_when(self, value: str) -> datetime:
        try:
            return parse_local(value, self.tz)
        except (ValueError, TypeError):
            raise InvalidDateError() from None
