"""Cableado del módulo: dice qué piezas necesita cada request (inyección de dependencias de FastAPI).

Acá se decide QUÉ implementación concreta se usa. Para cambiar el escritor falso por el real
(fase 4) alcanza con cambiar `get_post_writer`; ni el router ni el service se enteran.
"""
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.modules.content.fakes import FakePostWriter
from app.modules.content.service import ContentService, PostWriter


def get_post_writer() -> PostWriter:
    return FakePostWriter()


def get_content_service(
    session: Annotated[Session, Depends(get_session)],
    writer: Annotated[PostWriter, Depends(get_post_writer)],
) -> ContentService:
    """Un service nuevo por request, con la sesión de ese request."""
    settings = get_settings()
    return ContentService(session, writer, settings.tzinfo, settings.ollama_model)
