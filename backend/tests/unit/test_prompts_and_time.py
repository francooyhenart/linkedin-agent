"""Prompts y conversión de fechas locales. Funciones puras."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.timeutil import format_local, parse_local
from app.modules.content.prompts import add_feedback, build_system_prompt, build_user_prompt
from app.modules.content.text_rules import BANNED

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def test_system_prompt_incluye_el_maximo_y_todas_las_frases_prohibidas():
    prompt = build_system_prompt(max_chars=250)
    assert "Maximum 250 characters" in prompt
    assert all(phrase in prompt for phrase in BANNED)
    assert "Posts the author wrote before" not in prompt


def test_system_prompt_agrega_ejemplos_de_estilo_si_hay():
    prompt = build_system_prompt(300, style_examples="  Mi post anterior  ")
    assert prompt.endswith("---\nMi post anterior")


def test_user_prompt_solo_con_nota():
    assert build_user_prompt("migré un cron") == "Notes:\nmigré un cron"


def test_user_prompt_sin_nota_usa_el_borrador():
    assert "(no notes, use the current draft)" in build_user_prompt("", text="borrador")


def test_user_prompt_con_borrador_e_instruccion():
    prompt = build_user_prompt("nota", text="borrador", instruction="más corto")
    assert "Current draft:\nborrador\n\nRewrite the draft." in prompt
    assert prompt.endswith("Extra instruction: más corto")


def test_add_feedback_muestra_los_problemas_y_el_ultimo_texto():
    prompt = add_feedback("Notes:\nx", ["Se pasa por 5 caracteres.", "Suena a IA: journey"], "mi texto")
    assert "Your last version had problems: Se pasa por 5 caracteres. Suena a IA: journey Fix them:\nmi texto" in prompt


def test_parse_local_interpreta_en_la_zona_dada():
    parsed = parse_local("2026-11-10T09:00", TZ)
    assert parsed.utcoffset().total_seconds() == -3 * 3600
    assert parsed.astimezone(timezone.utc) == datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def test_parse_local_acepta_solo_fecha_y_toma_medianoche():
    assert parse_local("2026-11-10", TZ) == datetime(2026, 11, 10, 0, 0, tzinfo=TZ)


@pytest.mark.parametrize("bad", ["", "mañana", "2026-13-45T00:00", None])
def test_parse_local_rechaza_fechas_invalidas(bad):
    with pytest.raises((ValueError, TypeError)):
        parse_local(bad, TZ)


def test_format_local_ida_y_vuelta():
    utc = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    assert format_local(utc, TZ) == "2026-11-10T09:00"
