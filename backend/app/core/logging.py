"""Configuración de logs: un formato único para toda la app."""
import logging


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx loguea cada request a INFO; con tokens en la URL sería ruido (y riesgo). Lo bajamos.
    logging.getLogger("httpx").setLevel(logging.WARNING)
