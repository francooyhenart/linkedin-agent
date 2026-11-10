"""Limpieza de texto y chequeo de frases prohibidas. Funciones puras: no necesitan base ni red."""
from app.modules.content.text_rules import BANNED, check_text, clean


# ---------------------------------------------------------------- clean
def test_clean_saca_bloques_think_aunque_ocupen_varias_lineas():
    raw = "<think>pensando...\nmás pensamiento</think>Shipped the fix today."
    assert clean(raw) == "Shipped the fix today."


def test_clean_saca_comillas_de_los_extremos():
    assert clean('"Hola mundo"') == "Hola mundo"
    assert clean("“Hola mundo”") == "Hola mundo"


def test_clean_reemplaza_rayas_largas_por_coma():
    assert clean("I tried it — it worked") == "I tried it, it worked"


def test_clean_saca_emojis():
    assert clean("Done \U0001F680 finally ✅") == "Done  finally"


def test_clean_saca_hashtags_pero_no_el_numeral_pegado_a_otra_palabra():
    assert clean("Migrated the cron #devops #python").strip() == "Migrated the cron"
    assert clean("Issue C#7 fixed") == "Issue C#7 fixed"


def test_clean_colapsa_saltos_de_linea_y_espacios_al_final_de_linea():
    assert clean("uno  \n\n\n\ndos") == "uno\n\ndos"


def test_clean_deja_igual_un_texto_ya_limpio():
    text = "Moved a cron job to Redis.\nTook two days."
    assert clean(text) == text


# ---------------------------------------------------------------- check_text
def test_check_text_sin_problemas_devuelve_lista_vacia():
    assert check_text("Moved a cron job to Redis.", max_chars=300) == []


def test_check_text_avisa_cuanto_se_pasa_del_maximo():
    assert check_text("a" * 105, max_chars=100) == ["Se pasa por 5 caracteres."]


def test_check_text_justo_en_el_maximo_esta_bien():
    assert check_text("a" * 100, max_chars=100) == []


def test_check_text_detecta_frases_prohibidas_sin_importar_mayusculas():
    warnings = check_text("I'm THRILLED to share my journey.", max_chars=300)
    assert warnings == ["Suena a IA: thrilled, journey"]


def test_check_text_detecta_final_con_pregunta():
    assert check_text("Anyone else hit this bug?", max_chars=300) == ["Termina con pregunta al público."]


def test_check_text_acumula_varios_problemas():
    warnings = check_text("Let's delve into this?" + "x" * 50, max_chars=10)
    assert len(warnings) == 2  # largo + frase prohibida (no termina en "?")
    assert warnings[0].startswith("Se pasa por")
    assert "delve" in warnings[1]


def test_toda_frase_prohibida_esta_en_minuscula():
    # check_text compara contra text.lower(): una frase con mayúsculas en BANNED nunca coincidiría.
    assert all(phrase == phrase.lower() for phrase in BANNED)
