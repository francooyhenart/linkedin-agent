"""El panel (frontend/index.html) y la API tienen que seguir de acuerdo.

No hay tests de JavaScript, pero este chequeo estático atrapa el error más común al mover rutas: que el HTML llame
a una URL que ya no existe.
"""
import re

from fastapi.testclient import TestClient

from app.core.config import PROJECT_ROOT
from app.main import app

HTML = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def normalize(path: str) -> str:
    """'/posts/${cur.id}/publish-now' y '/posts/{post_id}/publish-now' -> '/posts/{}/publish-now'."""
    return re.sub(r"\$\{[^}]*\}|\{[^}]*\}", "{}", path)


def frontend_api_paths() -> set[str]:
    # Todas las llamadas son `${API}/algo...` (template literals). Se corta en el '?' de la query o en la comilla.
    raw = re.findall(r"\$\{API\}(/[^`\"'?]*)", HTML)
    return {normalize("/api/content" + path) for path in raw}


def test_el_panel_usa_las_rutas_nuevas_y_ninguna_vieja():
    assert 'const API = "/api/content"' in HTML
    # Cualquier texto que arranque con "/api/..." salvo la definición de la constante (que es '"/api/content').
    literal_api_paths = re.findall(r"[\"'`]/api/[^\"'`]*", HTML)
    old_style = [m for m in literal_api_paths if m != '"/api/content']
    assert old_style == [], f"quedaron llamadas con la ruta vieja: {old_style}"


def test_cada_ruta_que_usa_el_panel_existe_en_la_api():
    api_paths = {normalize(path) for path in app.openapi()["paths"]}
    used = frontend_api_paths()
    assert len(used) >= 10  # el regex realmente encontró las llamadas
    missing = sorted(used - api_paths)
    assert missing == [], f"el panel llama a rutas que no existen: {missing}"


def test_las_rutas_de_oauth_del_panel_siguen_igual():
    assert 'href="/auth/linkedin"' in HTML
    paths = {r.path for r in app.router.routes if hasattr(r, "path")}
    assert "/health" in paths  # (las de /auth/linkedin viven en un router incluido; se prueban en test_linkedin_api)


def test_el_panel_conoce_el_estado_publishing():
    assert "publishing:" in HTML and "s-publishing" in HTML
    assert 'id="edSalio"' in HTML and 'id="edNoSalio"' in HTML  # "Sí, salió" / "No salió"


def test_el_menu_lateral_agrupa_por_modulo_y_deja_ajustes_aparte():
    assert '<header class="top">' not in HTML  # ya no hay barra superior
    group = re.search(r'<div class="grp" data-module="content">(.*?)</div>', HTML, re.S).group(1)
    assert "Contenido" in group
    assert re.findall(r'href="(#\w+)"', group) == ["#calendario", "#ideas", "#metricas"]
    assert 'class="settings" href="#ajustes"' in HTML
    assert 'id="health"' in HTML.split('<aside class="side"')[1].split("</aside>")[0]  # los indicadores, al pie del menú
    assert 'id="menuBtn"' in HTML and 'aria-controls="side"' in HTML  # el botón de pantallas chicas


def test_borrar_una_idea_pide_confirmacion():
    handler = HTML.split('dataset.a === "del"')[1].split("openEditor")[0]
    assert handler.index("confirm(") < handler.index("DELETE")


def test_sugerir_ideas_no_guarda_nada_por_su_cuenta():
    # La única llamada que crea ideas en ese bloque es la de "Guardar idea" de una respuesta escrita por la persona.
    block = HTML.split('$("#suggestBtn").onclick')[1].split("async function planNext")[0]
    assert block.count('${API}/ideas`, { method: "POST"') == 1


def test_la_respuesta_a_una_sugerencia_se_guarda_con_su_pregunta():
    block = HTML.split('$("#suggestList").addEventListener')[1].split("async function planNext")[0]
    assert "note: `${question}\\n${note}`" in block


def test_el_punto_rojo_del_menu_depende_del_modelo_y_del_vencimiento_de_linkedin():
    load = HTML.split("async function loadStatus()")[1].split("async function loadSettings")[0]
    assert 'classList.toggle("alert", !o.ok || liSoon)' in load
    assert "#menuBtn.alert::after" in HTML


def test_el_indicador_de_posts_para_publicar_a_mano_esta_en_el_menu_y_en_el_titulo():
    assert 'id="readyBadge"' in HTML.split('data-module="content"')[1].split("</div>")[0]  # al lado de Calendario
    count = HTML.split("async function updateReadyCount()")[1].split("const refresh")[0]
    assert "status=ready" in count and 'document.title = n ? `(${n}) LinkedIn Agent` : "LinkedIn Agent"' in count
    assert "updateReadyCount()" in HTML.split("const refresh = act(")[1].split("\n")[0]  # se actualiza al refrescar


def test_el_interruptor_de_notificaciones_se_lee_y_se_guarda_en_ajustes():
    assert 'id="sNotify"' in HTML
    assert '$("#sNotify").checked = s.notify_ready' in HTML
    assert 'notify_ready: $("#sNotify").checked' in HTML


def test_get_raiz_sirve_el_panel():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>LinkedIn Agent</title>" in response.text
    # Se comparan BYTES: read_text() convierte los finales de línea CRLF de Windows a LF y la app sirve el archivo tal cual.
    assert response.content == (PROJECT_ROOT / "frontend" / "index.html").read_bytes()
