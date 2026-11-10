"""Repository de content: el ÚNICO lugar del módulo que habla con la base.

Reglas:
- Recibe una sesión de SQLAlchemy y hace consultas. Nada más.
- NUNCA hace commit ni rollback: eso lo decide el service (es quien sabe dónde termina una operación).
  Hace `flush()` cuando necesita que la base asigne ids o valores por defecto.
- No tiene reglas de negocio: no decide nada, solo guarda y busca.
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from app.modules.content.models import Idea, Post, PostMetric, PostStatus, Setting


@dataclass(frozen=True)
class PostRow:
    """Un post junto con su último snapshot de métricas (None si nunca se cargaron)."""

    post: Post
    latest_metric: PostMetric | None


class ContentRepository:
    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------ ideas
    def add_idea(self, note: str) -> Idea:
        idea = Idea(note=note)
        self.session.add(idea)
        self.session.flush()
        self.session.refresh(idea)  # trae created_at/updated_at que puso la base
        return idea

    def get_idea(self, idea_id: int) -> Idea | None:
        return self.session.get(Idea, idea_id)

    def list_ideas(self) -> list[Idea]:
        """Primero las sin usar, y dentro de cada grupo las más nuevas primero."""
        return list(self.session.scalars(select(Idea).order_by(Idea.used, Idea.id.desc())))

    def list_unused_ideas(self, limit: int) -> list[Idea]:
        """Las ideas sin usar más viejas primero (se usan en orden de llegada)."""
        stmt = select(Idea).where(Idea.used.is_(False)).order_by(Idea.id).limit(limit)
        return list(self.session.scalars(stmt))

    def mark_idea_used(self, idea: Idea) -> None:
        idea.used = True
        self.session.flush()

    def delete_idea(self, idea_id: int) -> bool:
        """Devuelve False si no existía. Los posts de la idea quedan con idea_id NULL (lo hace Postgres)."""
        idea = self.session.get(Idea, idea_id)
        if idea is None:
            return False
        self.session.delete(idea)
        self.session.flush()
        return True

    # ------------------------------------------------------------------ posts
    def _posts_with_latest_metric(self):
        """SELECT de posts + su último snapshot, resuelto en la base con DISTINCT ON.

        DISTINCT ON (post_id) deja UNA fila por post: la primera según el ORDER BY, o sea
        la de captured_at más reciente (id desc desempata). Después se une con posts con LEFT JOIN,
        para que los posts sin métricas también aparezcan.
        """
        latest_per_post = (
            select(PostMetric)
            .ext(distinct_on(PostMetric.post_id))
            .order_by(PostMetric.post_id, PostMetric.captured_at.desc(), PostMetric.id.desc())
            .subquery()
        )
        latest = aliased(PostMetric, latest_per_post)
        return select(Post, latest).outerjoin(latest, latest.post_id == Post.id)

    def get_post_row(self, post_id: int) -> PostRow | None:
        row = self.session.execute(self._posts_with_latest_metric().where(Post.id == post_id)).one_or_none()
        return PostRow(row[0], row[1]) if row else None

    def get_post(self, post_id: int) -> Post | None:
        return self.session.get(Post, post_id)

    def list_post_rows(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        status: PostStatus | None = None,
    ) -> list[PostRow]:
        """Posts ordenados por fecha. `start` es inclusivo y `end` exclusivo."""
        stmt = self._posts_with_latest_metric()
        if start is not None:
            stmt = stmt.where(Post.scheduled_at >= start)
        if end is not None:
            stmt = stmt.where(Post.scheduled_at < end)
        if status is not None:
            stmt = stmt.where(Post.status == status)
        rows = self.session.execute(stmt.order_by(Post.scheduled_at, Post.id)).all()
        return [PostRow(post, metric) for post, metric in rows]

    def list_scheduled_times(self) -> list[datetime]:
        """scheduled_at de todos los posts (para saber qué días ya están ocupados)."""
        return list(self.session.scalars(select(Post.scheduled_at)))

    def add_post(
        self,
        text: str,
        scheduled_at: datetime,
        status: PostStatus,
        idea_id: int | None,
    ) -> Post:
        post = Post(text=text, scheduled_at=scheduled_at, status=status, idea_id=idea_id)
        self.session.add(post)
        self.session.flush()
        self.session.refresh(post)
        return post

    def update_post(self, post: Post, fields: dict) -> Post:
        for name, value in fields.items():
            setattr(post, name, value)
        self.session.flush()
        self.session.refresh(post)
        return post

    def delete_post(self, post_id: int) -> bool:
        """Devuelve False si no existía. Sus snapshots de métricas se borran solos (ON DELETE CASCADE)."""
        post = self.session.get(Post, post_id)
        if post is None:
            return False
        self.session.delete(post)
        self.session.flush()
        return True

    # ------------------------------------------------------------------ métricas
    def latest_metric(self, post_id: int) -> PostMetric | None:
        stmt = (
            select(PostMetric)
            .where(PostMetric.post_id == post_id)
            .order_by(PostMetric.captured_at.desc(), PostMetric.id.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list_metrics(self, post_id: int) -> list[PostMetric]:
        """Todos los snapshots de un post, del más viejo al más nuevo (para ver la evolución)."""
        stmt = (
            select(PostMetric)
            .where(PostMetric.post_id == post_id)
            .order_by(PostMetric.captured_at, PostMetric.id)
        )
        return list(self.session.scalars(stmt))

    def add_metric(self, post_id: int, captured_at: datetime, counts: dict[str, int]) -> PostMetric:
        metric = PostMetric(post_id=post_id, captured_at=captured_at, **counts)
        self.session.add(metric)
        self.session.flush()
        return metric

    def update_metric(self, metric: PostMetric, captured_at: datetime, counts: dict[str, int]) -> PostMetric:
        metric.captured_at = captured_at
        for name, value in counts.items():
            setattr(metric, name, value)
        self.session.flush()
        return metric

    # ------------------------------------------------------------------ settings (clave-valor)
    def get_settings_map(self, keys: list[str] | None = None) -> dict[str, str]:
        """Los pares clave-valor guardados. Con `keys`, trae SOLO esas claves (el resto ni se lee)."""
        stmt = select(Setting.key, Setting.value)
        if keys is not None:
            stmt = stmt.where(Setting.key.in_(keys))
        return {key: value for key, value in self.session.execute(stmt)}

    def upsert_settings(self, values: dict[str, str]) -> None:
        """Inserta o actualiza cada clave (INSERT ... ON CONFLICT DO UPDATE de Postgres)."""
        for key, value in values.items():
            stmt = pg_insert(Setting).values(key=key, value=value)
            stmt = stmt.on_conflict_do_update(
                index_elements=[Setting.key],
                set_={"value": stmt.excluded.value, "updated_at": func.now()},
            )
            self.session.execute(stmt)
