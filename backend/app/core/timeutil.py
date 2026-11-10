"""Fechas: en la base todo es timestamptz; el panel habla en hora local sin offset.

El panel (HTML) trabaja con textos tipo "2026-11-10T09:00" que significan "hora de mi zona horaria".
Estas funciones convierten entre ese formato y datetimes con zona (aware) para guardar en Postgres.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_FORMAT = "%Y-%m-%dT%H:%M"


def parse_local(value: str, tz: ZoneInfo) -> datetime:
    """'2026-11-10T09:00' (o solo '2026-11-10') -> datetime con zona `tz`. Lanza ValueError si no se entiende."""
    if not isinstance(value, str):
        raise ValueError("se esperaba texto")
    return datetime.fromisoformat(value[:16]).replace(tzinfo=tz)


def format_local(value: datetime, tz: ZoneInfo) -> str:
    """datetime con zona -> '2026-11-10T09:00' en la zona `tz`."""
    return value.astimezone(tz).strftime(LOCAL_FORMAT)
