"""Cálculo de los próximos días libres para publicar. Función pura: la hora actual se le pasa de afuera."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.modules.content.slots import next_slots, parse_post_days

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
NINE = time(9, 0)
# 2026-11-10 es martes (weekday() == 1).
MARTES_10_AM = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
MARTES_8_AM = datetime(2026, 11, 10, 8, 0, tzinfo=TZ)


def dates(slots):
    return [s.date() for s in slots]


# ---------------------------------------------------------------- parse_post_days
def test_parse_post_days():
    assert parse_post_days("1,3") == {1, 3}
    assert parse_post_days(" 0 , 6 ") == {0, 6}


def test_parse_post_days_ignora_basura_y_vacios():
    assert parse_post_days("") == set()
    assert parse_post_days("1,,x,3") == {1, 3}


# ---------------------------------------------------------------- next_slots
def test_si_la_hora_de_hoy_ya_paso_empieza_por_el_proximo_dia_valido():
    slots = next_slots(2, {1, 3}, NINE, set(), MARTES_10_AM)
    assert dates(slots) == [date(2026, 11, 12), date(2026, 11, 17)]  # jueves y martes siguiente


def test_si_la_hora_de_hoy_todavia_no_llego_hoy_cuenta():
    slots = next_slots(1, {1, 3}, NINE, set(), MARTES_8_AM)
    assert dates(slots) == [date(2026, 11, 10)]


def test_saltea_los_dias_que_ya_tienen_un_post():
    taken = {date(2026, 11, 12)}
    slots = next_slots(2, {1, 3}, NINE, taken, MARTES_10_AM)
    assert dates(slots) == [date(2026, 11, 17), date(2026, 11, 19)]


def test_los_resultados_tienen_la_hora_y_la_zona_pedidas():
    (slot,) = next_slots(1, {3}, time(14, 30), set(), MARTES_10_AM)
    assert (slot.hour, slot.minute) == (14, 30)
    assert slot.tzinfo == TZ
    assert slot.isoformat() == "2026-11-12T14:30:00-03:00"


def test_sin_dias_elegidos_no_devuelve_nada():
    assert next_slots(3, set(), NINE, set(), MARTES_10_AM) == []


def test_count_cero_devuelve_lista_vacia():
    assert next_slots(0, {1}, NINE, set(), MARTES_10_AM) == []


def test_respeta_el_horizonte_y_devuelve_menos_si_no_hay_lugar():
    # Con 8 días de horizonte (martes 10 al martes 17 inclusive) solo caben jueves 12 y martes 17.
    slots = next_slots(5, {1, 3}, NINE, set(), MARTES_10_AM, horizon_days=8)
    assert dates(slots) == [date(2026, 11, 12), date(2026, 11, 17)]


def test_todos_los_dias_ocupados_devuelve_vacio():
    all_days = {date(2026, 11, 10) + timedelta(days=i) for i in range(90)}
    assert next_slots(2, {1, 3}, NINE, all_days, MARTES_10_AM) == []
