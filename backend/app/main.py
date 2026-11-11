"""Punto de entrada: crea la app de FastAPI.

Registra los routers de cada módulo, arranca el scheduler y traduce las excepciones de dominio a respuestas HTTP.
(Más adelante: montar el frontend.)
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app.core.config import PROJECT_ROOT, get_settings
from app.core.database import SessionLocal, check_connection
from app.core.exceptions import AppError, ExternalServiceError, InvalidInputError, NotFoundError
from app.core.logging import setup_logging
from app.integrations.linkedin.client import get_linkedin_client
from app.modules.content.router import auth_router as linkedin_auth_router
from app.modules.content.router import router as content_router
from app.modules.content.scheduler import start_scheduler
from app.shared.notifications.system import SystemNotifier

logger = logging.getLogger(__name__)

FRONTEND_DIR = PROJECT_ROOT / "frontend"

# Único lugar donde se decide qué código HTTP corresponde a cada tipo de error de dominio.
# Se prueba en orden con isinstance, así las excepciones de cada módulo (que heredan de estas) funcionan solas.
HTTP_STATUS_BY_ERROR = [
    (NotFoundError, 404),
    (InvalidInputError, 400),
    (ExternalServiceError, 502),
]


async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    status = next((code for cls, code in HTTP_STATUS_BY_ERROR if isinstance(exc, cls)), 500)
    # {"detail": ...} es el mismo formato que usaba la app original: el panel ya sabe leerlo.
    return JSONResponse(status_code=status, content={"detail": exc.message})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Código que corre al arrancar (antes del yield) y al apagar (después)."""
    setup_logging()
    settings = get_settings()
    logger.info("Arrancando. Zona horaria: %s", settings.timezone)
    # SCHEDULER_ENABLED=false (o los tests) -> no se publica nada solo.
    scheduler = (
        start_scheduler(SessionLocal, get_linkedin_client(), settings, SystemNotifier())
        if settings.scheduler_enabled
        else None
    )
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title="Postline", lifespan=lifespan)
    app.add_exception_handler(AppError, handle_app_error)
    app.include_router(content_router)
    app.include_router(linkedin_auth_router)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """El panel: un solo HTML que habla con la API de /api/content/..."""
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/health")
    def health(response: Response) -> dict:
        """200 si la app y Postgres responden; 503 si la base no está disponible."""
        database_ok = check_connection()
        if not database_ok:
            response.status_code = 503
        return {"status": "ok" if database_ok else "error", "database": database_ok}

    return app


app = create_app()
