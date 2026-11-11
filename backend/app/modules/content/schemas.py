"""Schemas Pydantic de content: la forma de lo que entra y sale por la API.

Los schemas NO tocan la base ni tienen reglas de negocio; solo validan formato y definen el JSON.
Las fechas viajan como texto en hora local ("2026-11-10T09:00") para que el panel no necesite cambios;
en la base se guardan como timestamptz.
"""
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.core.config import get_settings
from app.core.timeutil import format_local
from app.modules.content.models import PostStatus

if TYPE_CHECKING:  # solo para anotar tipos: el schema no depende del repository en tiempo de ejecución
    from app.modules.content.repository import PostRow


# ---------------------------------------------------------------- ideas
class IdeaCreate(BaseModel):
    note: str


class IdeaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    note: str
    used: bool
    created_at: datetime


class IdeaSuggestionOut(BaseModel):
    """Una pregunta para recordar, con la idea del banco de la que sale. No se guarda en ningún lado."""

    model_config = ConfigDict(from_attributes=True)

    question: str
    idea_id: int
    idea_note: str


class IdeaSuggestionsOut(BaseModel):
    suggestions: list[IdeaSuggestionOut]


# ---------------------------------------------------------------- posts
class PostCreate(BaseModel):
    text: str = ""
    scheduled_at: str  # "AAAA-MM-DDTHH:MM", hora local. Se interpreta en el service.
    status: PostStatus = PostStatus.DRAFT
    idea_id: int | None = None


class PostUpdate(BaseModel):
    text: str | None = None
    scheduled_at: str | None = None
    status: PostStatus | None = None
    linkedin_url: str | None = None


class PostOut(BaseModel):
    id: int
    idea_id: int | None
    text: str
    scheduled_at: datetime
    status: PostStatus
    linkedin_url: str | None
    published_at: datetime | None
    error: str | None
    # Último snapshot de métricas (0 si nunca se cargaron), aplanado como lo espera el panel.
    impressions: int
    reactions: int
    comments: int
    reposts: int
    metrics_captured_at: datetime | None

    @field_serializer("scheduled_at", "published_at", "metrics_captured_at")
    def _as_local_text(self, value: datetime | None) -> str | None:
        return None if value is None else format_local(value, get_settings().tzinfo)

    @classmethod
    def from_row(cls, row: "PostRow") -> "PostOut":
        post, metric = row.post, row.latest_metric
        return cls(
            id=post.id,
            idea_id=post.idea_id,
            text=post.text,
            scheduled_at=post.scheduled_at,
            status=post.status,
            linkedin_url=post.linkedin_url,
            published_at=post.published_at,
            error=post.error,
            impressions=metric.impressions if metric else 0,
            reactions=metric.reactions if metric else 0,
            comments=metric.comments if metric else 0,
            reposts=metric.reposts if metric else 0,
            metrics_captured_at=metric.captured_at if metric else None,
        )


# ---------------------------------------------------------------- métricas
class MetricsIn(BaseModel):
    impressions: int = Field(0, ge=0)
    reactions: int = Field(0, ge=0)
    comments: int = Field(0, ge=0)
    reposts: int = Field(0, ge=0)


class MetricSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    captured_at: datetime
    impressions: int
    reactions: int
    comments: int
    reposts: int

    @field_serializer("captured_at")
    def _as_local_text(self, value: datetime) -> str:
        return format_local(value, get_settings().tzinfo)


# ---------------------------------------------------------------- generación
class GenerateIn(BaseModel):
    note: str = ""
    text: str = ""
    instruction: str = ""


class GenerateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    text: str
    warnings: list[str]


class PlanFailureOut(BaseModel):
    """Una idea para la que no se pudo armar el borrador, y por qué."""

    model_config = ConfigDict(from_attributes=True)

    idea_id: int
    reason: str


class PlanOut(BaseModel):
    """Resultado de planificar: los borradores que salieron bien y las ideas que fallaron."""

    created: list[PostOut]
    failed: list[PlanFailureOut]


# ---------------------------------------------------------------- estado (Ollama y LinkedIn)
class OllamaStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ok: bool
    models: list[str]


class LinkedInStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    configured: bool
    connected: bool
    name: str
    expires_at: datetime | None  # cuándo vence el permiso (se muestra en hora local con offset)
    api_version: str  # el valor de LINKEDIN_API_VERSION
    api_version_age_months: int
    api_version_outdated: bool  # True si tiene más de 10 meses: hay que actualizarla pronto (vence al año)

    @field_serializer("expires_at")
    def _as_text(self, value: datetime | None) -> str:
        # El panel hace new Date(expires_at): ISO con offset sirve. Sin fecha, texto vacío (como antes).
        return "" if value is None else value.astimezone(get_settings().tzinfo).isoformat(timespec="minutes")


class StatusOut(BaseModel):
    ollama: OllamaStatusOut
    linkedin: LinkedInStatusOut


# ---------------------------------------------------------------- ajustes
class SettingsUpdate(BaseModel):
    """Solo estos campos se pueden editar. Cualquier otro que llegue se ignora (extra='ignore' es el default),
    así que por esta vía es imposible tocar los tokens de LinkedIn."""

    post_days: str | None = None
    post_time: str | None = None
    max_chars: int | None = None
    model: str | None = None
    style_examples: str | None = None
    notify_ready: bool | None = None


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    post_days: str
    post_time: str
    max_chars: int
    model: str
    style_examples: str
    banned: list[str]
    notify_ready: bool
