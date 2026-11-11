"""Texto de la notificación "hay posts para publicar a mano". Funciones PURAS: texto entra, texto sale.

Cómo se muestra el aviso (Windows, macOS, Linux) es otro tema y vive en app/shared/notifications/.
"""
import re

PREVIEW_CHARS = 60  # cuánto del post se muestra
MAX_LISTED = 3  # con varios posts, cuántos se muestran en el cuerpo

SINGLE_TITLE = "Post para publicar a mano"

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """Los primeros caracteres del post en una sola línea (saltos de línea y espacios repetidos se aplastan)."""
    flat = " ".join(_CONTROL_CHARS.sub(" ", text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def build_manual_publish_message(texts: list[str]) -> tuple[str, str]:
    """(título, cuerpo) del aviso. Un solo post: título fijo y el comienzo del texto. Varios del mismo ciclo del
    scheduler: UN solo aviso, "3 posts para publicar a mano", con el comienzo de los primeros."""
    if len(texts) == 1:
        return SINGLE_TITLE, preview(texts[0])
    lines = [preview(text) for text in texts[:MAX_LISTED]]
    if len(texts) > MAX_LISTED:
        lines.append(f"y {len(texts) - MAX_LISTED} más")
    return f"{len(texts)} posts para publicar a mano", "\n".join(lines)
