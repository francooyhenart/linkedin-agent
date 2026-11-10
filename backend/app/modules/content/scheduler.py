"""Tarea periódica (cada 1 minuto) que publica los posts programados cuya hora ya llegó.

El scheduler corre en un hilo aparte (APScheduler) y NO comparte nada con los requests: cada pasada abre su
PROPIA sesión de base de datos y la cierra al terminar. Todas las reglas (reclamar el post, MAX_LATE_HOURS,
no duplicar) están en PublishingService; esto solo lo dispara cada minuto.

Las horas se comparan como datetimes con zona horaria (la de TIMEZONE), así que no depende de la zona en que esté
configurada la compu ni de cambios de horario.
"""
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.integrations.linkedin.client import LinkedInClient
from app.modules.content.publishing import PublishingService

logger = logging.getLogger(__name__)


def run_tick(
    session_factory: Callable[[], Session],
    linkedin: LinkedInClient,
    settings: Settings,
) -> None:
    """Una pasada. Nunca lanza: si algo falla se registra y APScheduler vuelve a intentar en un minuto."""
    try:
        with session_factory() as session:  # sesión propia de esta pasada
            PublishingService(
                session,
                linkedin,
                settings.tzinfo,
                max_late=timedelta(hours=settings.max_late_hours),
            ).publish_due_posts()
    except Exception:  # noqa: BLE001 - por ejemplo, Postgres caído
        logger.exception("Falló la pasada del scheduler; se reintenta en 1 minuto")


def start_scheduler(
    session_factory: Callable[[], Session],
    linkedin: LinkedInClient,
    settings: Settings,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.tzinfo)
    scheduler.add_job(
        run_tick,
        "interval",
        minutes=1,
        args=[session_factory, linkedin, settings],
        next_run_time=datetime.now(settings.tzinfo),  # una pasada apenas arranca la app (publica lo atrasado)
        max_instances=1,  # si una pasada tarda más de un minuto, no se superponen
        coalesce=True,  # si se perdieron pasadas (compu dormida), se hace una sola
        id="publish-due-posts",
    )
    scheduler.start()
    logger.info("Scheduler iniciado (cada 1 minuto, zona %s)", settings.timezone)
    return scheduler
