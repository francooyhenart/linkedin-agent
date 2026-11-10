"""Tablas del módulo content: ideas, posts, post_metrics y settings.

Los modelos solo describen la forma de los datos. No tienen lógica de negocio ni hacen consultas:
eso es trabajo del service (reglas) y del repository (consultas).
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class PostStatus(str, Enum):
    """Estados de un post. En la base se guardan como texto simple ('draft', 'approved'...)."""

    DRAFT = "draft"  # borrador
    APPROVED = "approved"  # programado: sale solo a su hora
    READY = "ready"  # hay que publicarlo a mano
    # La app lo reclamó y está llamando a LinkedIn. Si queda así, NO se sabe si salió: se verifica a mano.
    # Nunca se reintenta solo (así no se duplican posts).
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class Idea(TimestampMixin, Base):
    """Nota cruda del usuario: la materia prima de los posts."""

    __tablename__ = "ideas"

    id: Mapped[int] = mapped_column(primary_key=True)
    note: Mapped[str] = mapped_column(Text)
    used: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    # Una idea puede originar varios posts (uno a muchos).
    # passive_deletes=True: al borrar una idea, SQLAlchemy NO toca los posts; lo resuelve Postgres con
    # ON DELETE SET NULL (ver Post.idea_id).
    posts: Mapped[list["Post"]] = relationship(back_populates="idea", passive_deletes=True)


class Post(TimestampMixin, Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Si se borra la idea, el post sigue existiendo y idea_id queda en NULL.
    idea_id: Mapped[int | None] = mapped_column(ForeignKey("ideas.id", ondelete="SET NULL"))
    text: Mapped[str] = mapped_column(Text, server_default="")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Enum de Python en el código, VARCHAR en la base (native_enum=False) + CHECK explícito abajo.
    # values_callable: guarda 'draft' (el valor) y no 'DRAFT' (el nombre del miembro).
    status: Mapped[PostStatus] = mapped_column(
        SAEnum(
            PostStatus,
            native_enum=False,
            length=20,
            create_constraint=False,  # el CHECK lo declaramos nosotros, con nombre, en __table_args__
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        server_default=PostStatus.DRAFT.value,
    )
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    idea: Mapped[Idea | None] = relationship(back_populates="posts")
    # Un post tiene muchos snapshots de métricas. Si se borra el post, se borran sus snapshots:
    # lo hace Postgres (ON DELETE CASCADE) y passive_deletes evita que SQLAlchemy los cargue uno por uno.
    metrics: Mapped[list["PostMetric"]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PostMetric.captured_at",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'approved', 'ready', 'publishing', 'published', 'failed')",
            name="status_valid",
        ),
        # El calendario filtra por rango de fechas y el scheduler por "ya le toca": ambos usan scheduled_at.
        Index("ix_posts_scheduled_at", "scheduled_at"),
    )


class PostMetric(TimestampMixin, Base):
    """Un snapshot de métricas de un post en un momento dado.

    No se pisa el anterior: cada vez que el usuario carga números se agrega una fila nueva.
    Así se puede ver la evolución; el panel muestra la fila más reciente (mayor captured_at).
    """

    __tablename__ = "post_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    # Cuándo se leyeron los números (puede diferir de created_at si el usuario carga uno retroactivo).
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    impressions: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reactions: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    comments: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reposts: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    post: Mapped[Post] = relationship(back_populates="metrics")

    __table_args__ = (
        CheckConstraint(
            "impressions >= 0 AND reactions >= 0 AND comments >= 0 AND reposts >= 0",
            name="counts_non_negative",
        ),
        # Para encontrar rápido "el último snapshot de este post".
        Index("ix_post_metrics_post_id_captured_at", "post_id", "captured_at"),
    )


class Setting(TimestampMixin, Base):
    """Ajustes del usuario y tokens de LinkedIn, como pares clave-valor (todo texto)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, server_default="")
