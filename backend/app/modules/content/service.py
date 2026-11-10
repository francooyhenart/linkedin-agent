"""Reglas de negocio de content: ideas, posts, métricas, ajustes, generación y planificación.

Reglas de esta capa:
- No conoce FastAPI: si algo falla lanza excepciones de dominio (exceptions.py), nunca HTTPException.
- No escribe consultas: pide todo al repository.
- Maneja la TRANSACCIÓN: cada operación que escribe termina con `commit()`. Si algo falla antes,
  no hay commit y al cerrarse la sesión todo se deshace (todo o nada).
  Por eso recibe la sesión, pero solo para eso: abrir/cerrar la transacción. Nunca consulta con ella.
- Lo externo (el modelo de lenguaje) entra por el Protocol `PostWriter`, así se puede reemplazar por un fake.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.timeutil import parse_local
from app.modules.content import prompts, slots, text_rules
from app.modules.content.exceptions import (
    EmptyIdeaError,
    IdeaNotFoundError,
    InvalidDateError,
    InvalidSettingError,
    NoPublishDaysError,
    NoUnusedIdeasError,
    NothingToGenerateFromError,
    PostNotFoundError,
)
from app.modules.content.models import Idea, PostMetric, PostStatus
from app.modules.content.repository import ContentRepository, PostRow
from app.modules.content.schemas import (
    GenerateIn,
    IdeaCreate,
    MetricsIn,
    PostCreate,
    PostUpdate,
    SettingsUpdate,
)

MAX_ATTEMPTS = 3  # intentos de generación si el texto no pasa los chequeos

# Ajustes que el usuario puede ver y editar. Los tokens de LinkedIn (li_*) viven en la misma tabla
# pero NO están en esta lista, así que nunca salen por la API de ajustes.
EDITABLE_SETTINGS = ["post_days", "post_time", "max_chars", "model", "style_examples"]
DEFAULT_SETTINGS = {"post_days": "1,3", "post_time": "09:00", "max_chars": "300", "style_examples": ""}


class PostWriter(Protocol):
    """Lo que el service necesita de un "escritor": recibe los prompts y devuelve el texto crudo.

    La implementación real (Ollama) llega en la fase 4. Es un Protocol: cualquier clase con un método
    `write` con esta forma sirve, sin heredar de nada.
    """

    def write(self, system: str, user: str, model: str) -> str: ...


@dataclass
class UserSettings:
    post_days: str
    post_time: str
    max_chars: int
    model: str
    style_examples: str
    banned: list[str]


@dataclass
class GenerationResult:
    text: str
    warnings: list[str]


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
        if "status" in fields:
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
    def generate(self, data: GenerateIn) -> GenerationResult:
        if not data.note.strip() and not data.text.strip():
            raise NothingToGenerateFromError()
        return self._generate(data.note, data.text, data.instruction)

    def _generate(self, note: str, text: str = "", instruction: str = "") -> GenerationResult:
        """Pide el texto al modelo; si no pasa los chequeos, reintenta hasta MAX_ATTEMPTS veces
        mostrándole sus errores. Devuelve el último intento (con sus avisos si no llegó a pasar)."""
        settings = self.get_user_settings()
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
    def plan_next_posts(self) -> list[PostRow]:
        """Toma las ideas sin usar (tantas como días de publicación por semana) y arma un borrador
        por cada una en los próximos días libres.

        Primero se genera todo el texto y recién después se guarda: si el modelo falla a mitad de camino
        no queda nada a medias (un solo commit al final)."""
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
        pairs = list(zip(ideas, free))  # si faltan días libres, solo se usan las ideas que alcanzan
        drafts = [(idea, when, self._generate(idea.note).text) for idea, when in pairs]

        created = []
        for idea, when, text in drafts:
            post = self.repo.add_post(text.strip(), when, PostStatus.DRAFT, idea.id)
            self.repo.mark_idea_used(idea)
            created.append(post.id)
        self.session.commit()
        return [self.get_post(post_id) for post_id in created]

    # ------------------------------------------------------------------ internos
    def _add_post(self, data: PostCreate):
        """Crea el post y marca la idea como usada. No hace commit: lo hace quien lo llama."""
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
