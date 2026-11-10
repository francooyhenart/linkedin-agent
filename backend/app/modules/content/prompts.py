"""Armado de los prompts para el modelo. Funciones PURAS: texto entra, texto sale."""
from app.modules.content.text_rules import BANNED


def build_system_prompt(max_chars: int, style_examples: str = "") -> str:
    rules = f"""You write short LinkedIn posts in English for a real person, based on their rough notes (the notes may be in Spanish).
Rules:
- Maximum {max_chars} characters. Usually 2 to 4 short lines.
- Sound like a person typing quickly, not a marketer. Plain words, first person.
- Keep one concrete detail from the notes (a tool, a number, a moment).
- Never invent facts, numbers, names or results that are not in the notes.
- No emojis. No hashtags. No em dashes. No bullet lists. No title, no hook formula.
- Do not end with a question to the audience or a call to action.
- Never use these words or phrases: {', '.join(BANNED)}.
- Output only the post text. No quotes, no preamble."""
    if style_examples.strip():
        rules += (
            "\n\nPosts the author wrote before. Match their tone and rhythm, not their topics:\n---\n"
            + style_examples.strip()
        )
    return rules


def build_user_prompt(note: str, text: str = "", instruction: str = "") -> str:
    user = f"Notes:\n{note.strip() or '(no notes, use the current draft)'}"
    if text.strip():
        user += f"\n\nCurrent draft:\n{text.strip()}\n\nRewrite the draft."
    if instruction.strip():
        user += f"\n\nExtra instruction: {instruction.strip()}"
    return user


def add_feedback(user_prompt: str, warnings: list[str], last_output: str) -> str:
    """Para el reintento: le muestra al modelo qué hizo mal para que lo corrija."""
    return f"{user_prompt}\n\nYour last version had problems: {' '.join(warnings)} Fix them:\n{last_output}"
