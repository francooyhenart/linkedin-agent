"""Cálculo de los próximos días libres para publicar. Funciones PURAS (no leen la base ni la hora)."""
from datetime import date, datetime, time, timedelta


def parse_post_days(value: str) -> set[int]:
    """'1,3' -> {1, 3}. Convención de Python: 0 = lunes ... 6 = domingo."""
    return {int(part) for part in value.split(",") if part.strip().isdigit()}


def next_slots(
    count: int,
    days: set[int],
    at: time,
    taken_dates: set[date],
    now: datetime,
    horizon_days: int = 60,
) -> list[datetime]:
    """Los próximos `count` momentos de publicación.

    Un día sirve si: es uno de los `days`, la hora `at` todavía no pasó y no hay ya un post ese día.
    `now` tiene que venir con la zona horaria del usuario: de ahí sale qué día es "hoy" y con qué zona
    se arman los resultados. Mira hasta `horizon_days` días hacia adelante.
    """
    slots: list[datetime] = []
    day = now.date()
    while len(slots) < count and (day - now.date()).days < horizon_days:
        when = datetime.combine(day, at, tzinfo=now.tzinfo)
        if day.weekday() in days and when > now and day not in taken_dates:
            slots.append(when)
        day += timedelta(days=1)
    return slots
