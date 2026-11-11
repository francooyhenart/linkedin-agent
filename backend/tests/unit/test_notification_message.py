"""Texto de la notificación "hay posts para publicar a mano". Funciones puras."""
from app.modules.content.notifications import MAX_LISTED, PREVIEW_CHARS, build_manual_publish_message, preview


def test_preview_deja_el_texto_corto_tal_cual():
    assert preview("Migré un cron a Redis") == "Migré un cron a Redis"


def test_preview_corta_en_60_caracteres_con_puntos_suspensivos():
    result = preview("a" * 200)
    assert len(result) == PREVIEW_CHARS == 60
    assert result == "a" * 59 + "…"


def test_preview_justo_en_el_limite_no_se_corta():
    assert preview("b" * 60) == "b" * 60


def test_preview_aplasta_saltos_de_linea_y_espacios_repetidos():
    assert preview("primera línea\n\n  segunda\tlínea") == "primera línea segunda línea"


def test_preview_saca_los_caracteres_de_control():
    assert preview("hola\x00\x07\x1bmundo") == "hola mundo"


def test_un_post_titulo_fijo_y_comienzo_del_texto():
    assert build_manual_publish_message(["Hoy migré un cron"]) == ("Post para publicar a mano", "Hoy migré un cron")


def test_varios_posts_son_un_solo_aviso_con_la_cantidad_en_el_titulo():
    title, body = build_manual_publish_message(["uno", "dos", "tres"])
    assert title == "3 posts para publicar a mano"
    assert body == "uno\ndos\ntres"


def test_con_mas_de_tres_muestra_los_primeros_y_cuantos_faltan():
    texts = [f"post {n}" for n in range(1, 6)]
    title, body = build_manual_publish_message(texts)
    assert title == "5 posts para publicar a mano"
    assert body.splitlines() == ["post 1", "post 2", "post 3", f"y {5 - MAX_LISTED} más"]
