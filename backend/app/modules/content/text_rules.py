"""Reglas de texto: frases prohibidas, limpieza y chequeos. Funciones PURAS (sin base, sin red)."""
import re

# Frases que delatan texto generado. Se evitan en el prompt y se marcan en el editor.
BANNED = [
    "thrilled", "excited to announce", "excited to share", "delve", "game-changer",
    "game changer", "journey", "leverage", "unlock", "landscape", "fast-paced", "humbled",
    "synergy", "elevate", "harness", "seamless", "robust", "tapestry", "testament",
    "pivotal", "foster", "empower", "dive into", "deep dive", "let's connect", "agree?",
    "thoughts?", "here's the thing", "the best part?", "unpopular opinion", "i'm proud to",
    "in today's world", "navigating", "embark", "realm", "cutting-edge",
]

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def clean(text: str) -> str:
    """Limpia lo que devuelve el modelo: bloques <think>, comillas, rayas largas, emojis y hashtags."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # modelos con razonamiento
    text = text.strip().strip('"').strip("“”").strip()
    text = re.sub(r"\s*—\s*", ", ", text)  # raya larga (em dash)
    text = EMOJI_RE.sub("", text)
    text = re.sub(r"(^|\s)#\w+", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def check_text(text: str, max_chars: int) -> list[str]:
    """Devuelve la lista de problemas del texto (vacía si está bien)."""
    warnings = []
    if len(text) > max_chars:
        warnings.append(f"Se pasa por {len(text) - max_chars} caracteres.")
    hits = [phrase for phrase in BANNED if phrase in text.lower()]
    if hits:
        warnings.append("Suena a IA: " + ", ".join(hits))
    if text.rstrip().endswith("?"):
        warnings.append("Termina con pregunta al público.")
    return warnings
