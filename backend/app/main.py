"""Punto de entrada: crea la app de FastAPI.

En esta fase solo tiene GET /health. A medida que avancemos acá se van a:
registrar los routers de cada módulo, montar el frontend y arrancar el scheduler.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from app.core.config import get_settings
from app.core.database import check_connection
from app.core.logging import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Código que corre al arrancar (antes del yield) y al apagar (después)."""
    setup_logging()
    settings = get_settings()
    logger.info("Arrancando. Zona horaria: %s", settings.timezone)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Postline", lifespan=lifespan)

    @app.get("/health")
    def health(response: Response) -> dict:
        """200 si la app y Postgres responden; 503 si la base no está disponible."""
        database_ok = check_connection()
        if not database_ok:
            response.status_code = 503
        return {"status": "ok" if database_ok else "error", "database": database_ok}

    return app


app = create_app()
