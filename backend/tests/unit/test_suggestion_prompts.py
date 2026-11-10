"""Prompts y lectura de la respuesta de "Sugerir ideas". Funciones puras: texto entra, texto sale."""
from app.modules.content.prompts import (
    build_suggestions_system_prompt,
    build_suggestions_user_prompt,
    parse_suggestions,
)

Q1 = "¿Cómo te fue con la cola de Redis después de unas semanas?"
Q2 = "¿Pudiste resolver el timeout de los tests o sigue pasando?"


# ---------------------------------------------------------------- prompt
def test_el_prompt_pide_preguntas_y_prohibe_inventar_o_proponer_temas():
    prompt = build_suggestions_system_prompt(5)
    assert "exactly 5 short questions" in prompt
    assert "NEVER suggest new topics" in prompt
    assert "NEVER invent facts" in prompt
    assert "NEVER write generic questions" in prompt
    assert "<note number>|<question>" in prompt


def test_el_prompt_prohibe_suponer_lo_que_la_nota_no_dice():
    assert "Do not assume anything the note does not say" in build_suggestions_system_prompt()


def test_el_prompt_trae_ejemplos_buenos_y_malos_con_el_motivo_de_los_malos():
    prompt = build_suggestions_system_prompt()
    good = prompt.split("GOOD questions")[1].split("BAD questions")[0]
    bad = prompt.split("BAD questions")[1]
    assert good.count("Good:") == 3
    bad_lines = [line for line in bad.splitlines() if line.startswith("Bad:")]
    assert len(bad_lines) == 3
    assert all("(" in line and line.rstrip().endswith(")") for line in bad_lines)  # cada mala dice por qué es mala
    assert "never copy them" in prompt


def test_los_ejemplos_del_prompt_son_de_otros_temas_que_las_notas_de_prueba():
    # Si un ejemplo coincidiera con una nota real, el modelo podría copiarlo en vez de pensar.
    prompt = build_suggestions_system_prompt()
    for topic in ("Redis", "pytest", "deploy", "Postgres"):
        assert topic not in prompt


def test_el_prompt_de_usuario_numera_las_notas_desde_1_y_aplasta_los_saltos_de_linea():
    prompt = build_suggestions_user_prompt(["migré un cron\na Redis", "probé pytest-timeout"])
    assert prompt == "Idea bank:\n1. migré un cron a Redis\n2. probé pytest-timeout"


# ---------------------------------------------------------------- lectura de la respuesta
def test_parse_lee_el_formato_pedido():
    assert parse_suggestions(f"1|{Q1}\n2|{Q2}", note_count=2) == [(1, Q1), (2, Q2)]


def test_parse_tolera_viñetas_espacios_y_negritas_alrededor():
    raw = f"- 1 | {Q1}\n* 2|**{Q2}**\n"
    assert parse_suggestions(raw, note_count=2) == [(1, Q1), (2, Q2)]


def test_parse_saca_la_numeracion_que_el_modelo_repite_dentro_de_la_pregunta():
    # Salida real de llama3.1:8b: "1|1. ¿Qué pasó…?"
    assert parse_suggestions(f"1|1. {Q1}\n2|2) {Q2}", note_count=2) == [(1, Q1), (2, Q2)]


def test_parse_ignora_lo_que_no_tiene_el_formato():
    raw = f"Claro, acá van tus preguntas:\n\n1|{Q1}\nEspero que sirvan.\n1. {Q2}"  # la última usa punto, no barra
    assert parse_suggestions(raw, note_count=3) == [(1, Q1)]


def test_parse_descarta_notas_que_no_existen():
    raw = f"0|{Q1}\n3|{Q2}\n99|{Q1}"
    assert parse_suggestions(raw, note_count=2) == []


def test_parse_descarta_lo_que_no_es_una_pregunta():
    raw = "1|Deberías escribir sobre tu migración de Redis.\n2|¿Qué onda?"  # no es pregunta / es muy corta
    assert parse_suggestions(raw, note_count=2) == []


def test_parse_descarta_dos_preguntas_pegadas_en_una():
    raw = "1|¿Qué fue lo más difícil de migrar el cron a Redis, ¿era el script o la falta de documentación?"
    assert parse_suggestions(raw, note_count=1) == []


def test_parse_descarta_repetidas_sin_importar_mayusculas():
    raw = f"1|{Q1}\n2|{Q1.upper()}"
    assert parse_suggestions(raw, note_count=2) == [(1, Q1)]


def test_parse_devuelve_como_mucho_el_limite():
    raw = "\n".join(f"1|¿Qué pasó después con el cron número {n}?" for n in range(10))
    assert len(parse_suggestions(raw, note_count=1)) == 5
    assert len(parse_suggestions(raw, note_count=1, limit=3)) == 3


def test_parse_de_texto_vacio_o_basura():
    assert parse_suggestions("", note_count=3) == []
    assert parse_suggestions("no sé", note_count=3) == []
