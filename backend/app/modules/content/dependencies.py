"""Cableado del módulo: dice qué piezas necesita cada request (inyección de dependencias de FastAPI).

Acá se decide QUÉ implementación concreta se usa. Los tests reemplazan estas funciones con
`app.dependency_overrides` para meter clientes con transporte simulado (sin red).
"""
from datetime import timedelta
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.integrations.linkedin.client import LinkedInClient, get_linkedin_client
from app.modules.content.publishing import PublishingService
from app.modules.content.service import ContentService, PostWriter
from app.modules.content.writer import OllamaPostWriter
from app.shared.llm.ollama import get_ollama_client


def get_post_writer() -> PostWriter:
    return OllamaPostWriter(get_ollama_client())


def get_linkedin() -> LinkedInClient:
    return get_linkedin_client()


def get_content_service(
    session: Annotated[Session, Depends(get_session)],
    writer: Annotated[PostWriter, Depends(get_post_writer)],
) -> ContentService:
    """Un service nuevo por request, con la sesión de ese request."""
    settings = get_settings()
    return ContentService(session, writer, settings.tzinfo, settings.ollama_model)


def get_publishing_service(
    session: Annotated[Session, Depends(get_session)],
    linkedin: Annotated[LinkedInClient, Depends(get_linkedin)],
) -> PublishingService:
    settings = get_settings()
    return PublishingService(
        session, linkedin, settings.tzinfo, max_late=timedelta(hours=settings.max_late_hours)
    )
