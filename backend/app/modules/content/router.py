"""Capa HTTP de content. Cada endpoint: recibe datos validados, llama al service, devuelve un schema.

Sin lógica y sin base de datos. Los errores no se atrapan acá: los services lanzan excepciones de dominio
y el handler de main.py las convierte en respuestas HTTP.
"""
from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.content.dependencies import get_content_service
from app.modules.content.models import PostStatus
from app.modules.content.schemas import (
    GenerateIn,
    GenerateOut,
    IdeaCreate,
    IdeaOut,
    MetricsIn,
    MetricSnapshotOut,
    PostCreate,
    PostOut,
    PostUpdate,
    SettingsOut,
    SettingsUpdate,
)
from app.modules.content.service import ContentService

router = APIRouter(prefix="/api/content", tags=["content"])

Service = Annotated[ContentService, Depends(get_content_service)]


# ---------------------------------------------------------------- ajustes
@router.get("/settings", response_model=SettingsOut)
def read_settings(service: Service):
    return service.get_user_settings()


@router.put("/settings", response_model=SettingsOut)
def write_settings(body: SettingsUpdate, service: Service):
    return service.update_settings(body)


# ---------------------------------------------------------------- ideas
@router.get("/ideas", response_model=list[IdeaOut])
def list_ideas(service: Service):
    return service.list_ideas()


@router.post("/ideas", response_model=IdeaOut)
def create_idea(body: IdeaCreate, service: Service):
    return service.create_idea(body)


@router.delete("/ideas/{idea_id}")
def delete_idea(idea_id: int, service: Service):
    service.delete_idea(idea_id)
    return {"ok": True}


# ---------------------------------------------------------------- posts
@router.get("/posts", response_model=list[PostOut])
def list_posts(
    service: Service,
    start: str | None = None,
    end: str | None = None,
    status: PostStatus | None = None,
):
    return [PostOut.from_row(row) for row in service.list_posts(start, end, status)]


@router.post("/posts", response_model=PostOut)
def create_post(body: PostCreate, service: Service):
    return PostOut.from_row(service.create_post(body))


@router.patch("/posts/{post_id}", response_model=PostOut)
def update_post(post_id: int, body: PostUpdate, service: Service):
    return PostOut.from_row(service.update_post(post_id, body))


@router.delete("/posts/{post_id}")
def delete_post(post_id: int, service: Service):
    service.delete_post(post_id)
    return {"ok": True}


# ---------------------------------------------------------------- métricas
@router.put("/posts/{post_id}/metrics", response_model=PostOut)
def put_metrics(post_id: int, body: MetricsIn, service: Service):
    return PostOut.from_row(service.record_metrics(post_id, body))


@router.get("/posts/{post_id}/metrics", response_model=list[MetricSnapshotOut])
def metric_history(post_id: int, service: Service):
    return service.list_metric_history(post_id)


# ---------------------------------------------------------------- generación y planificación
@router.post("/generate", response_model=GenerateOut)
def generate(body: GenerateIn, service: Service):
    return service.generate(body)


@router.post("/plan", response_model=list[PostOut])
def plan(service: Service):
    return [PostOut.from_row(row) for row in service.plan_next_posts()]
