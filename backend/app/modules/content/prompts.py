"""Armado de los prompts para el modelo. Funciones PURAS: texto entra, texto sale."""
import re

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


# ---------------------------------------------------------------- sugerir ideas (preguntas para recordar)
SUGGESTION_COUNT = 5


def build_suggestions_system_prompt(count: int = SUGGESTION_COUNT) -> str:
    """El modelo NO propone temas: hace preguntas sobre cosas que la persona ya anotó, para ayudarla a
    recordar qué pasó después. Cada pregunta tiene que apuntar a una nota concreta (así se puede comprobar)."""
    return f"""You help a person remember real experiences they could write about later.
You will see their idea bank: numbered rough notes about things that really happened to them (in Spanish).
Write exactly {count} short questions in Spanish (rioplatense, use "vos"), each one about ONE specific note.
Good questions look forward from what the note says: what happened next, how it looks now, whether they kept
doing it, what they would change.
Rules:
- Do not assume anything the note does not say. No people (boss, team, colleagues), no feelings, no problems,
  no reasons, no results, no numbers, unless the note itself mentions them.
- Never ask the person to explain or redo something the note says they could not do.
- Every question must name a concrete detail from its note (a tool, a project, an event).
- Write to the person in second person singular, never about "he/she/the team". Use voseo
  ("te fue", "pudiste", "sentís"), never "tú" forms such as "sientes" or "tienes".
- Each question is ONE sentence with exactly one "¿" and one "?". Never join two questions.
- NEVER suggest new topics or new ideas. NEVER give advice. NEVER invent facts.
- NEVER write generic questions that would fit any note ("¿Qué aprendiste esta semana?").
- Use different notes when there are enough of them. Do not repeat a question.
- Output format, one per line, nothing else: <note number>|<question>

GOOD questions (they only use what the note literally says):
Note: Cambié el linter del proyecto de flake8 a ruff
Good: ¿Cómo te fue con ruff después de unas semanas?
Note: Armé un script para renombrar fotos por fecha
Good: ¿Seguiste usando el script para renombrar fotos después de armarlo?
Note: Presenté por primera vez en la daily del equipo
Good: ¿Qué cambiarías de cómo presentaste en la daily esa primera vez?

BAD questions (never write these):
Note: Armé un script para renombrar fotos por fecha
Bad: ¿Qué te pareció la experiencia de programar? (too vague: it would fit any note)
Note: Cambié el linter del proyecto de flake8 a ruff
Bad: ¿Cómo reaccionó tu jefe al ver el cambio? (assumes a boss and a reaction; the note says nothing about either)
Note: Presenté por primera vez en la daily del equipo
Bad: ¿Qué problemas tuviste al presentar? (assumes there were problems; the note does not say so)

The examples are only an illustration of the style: never copy them."""


def build_suggestions_user_prompt(notes: list[str]) -> str:
    numbered = "\n".join(f"{n}. {' '.join(note.split())}" for n, note in enumerate(notes, start=1))
    return f"Idea bank:\n{numbered}"


_SUGGESTION_LINE = re.compile(r"^[\s*\-]*(\d{1,3})\s*\|\s*(.+?)\s*$")


def parse_suggestions(raw: str, note_count: int, limit: int = SUGGESTION_COUNT) -> list[tuple[int, str]]:
    """Lee la respuesta del modelo y devuelve [(número de nota, 1..note_count; pregunta)].

    Se queda SOLO con las líneas que cumplen el formato, apuntan a una nota que existe y son una pregunta
    de largo razonable; descarta repetidas. Lo que el modelo escriba fuera de ese formato se ignora.
    """
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        match = _SUGGESTION_LINE.match(line)
        if not match:
            continue
        number, question = int(match.group(1)), match.group(2).strip().strip('*"“”').strip()
        question = re.sub(r"^\d{1,3}[.)]\s+", "", question)  # el modelo a veces repite "1. " delante de la pregunta
        if not 1 <= number <= note_count or not question.endswith("?") or not 15 <= len(question) <= 200:
            continue
        if question.count("?") != 1 or question.count("¿") > 1:  # dos preguntas pegadas en una: se descarta
            continue
        key = question.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append((number, question))
        if len(found) == limit:
            break
    return found


def add_feedback(user_prompt: str, warnings: list[str], last_output: str) -> str:
    """Para el reintento: le muestra al modelo qué hizo mal para que lo corrija."""
    return f"{user_prompt}\n\nYour last version had problems: {' '.join(warnings)} Fix them:\n{last_output}"
