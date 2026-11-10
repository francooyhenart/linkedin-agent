"""Sugerir ideas, a nivel service y contra Postgres real, con un writer de mentira.

Foco: nada se guarda, cada sugerencia queda atada a una idea REAL, no se espera al modelo con una transacción
abierta y los errores salen como errores de dominio.
"""
from datetime import datetime

import pytest

from app.core.exceptions import ExternalServiceError
from app.modules.content.exceptions import NoIdeasToSuggestFromError, NoUsefulSuggestionsError
from app.modules.content.repository import ContentRepository
from app.modules.content.service import SUGGESTION_SOURCE_LIMIT, ContentService
from tests.helpers import TZ
from tests.test_support import NoWriter, ScriptedWriter

NOW = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
Q = "¿Cómo te fue con {} después de unas semanas?"


def make_service(session, writer):
    return ContentService(session, writer, TZ, "llama3.1:8b", now=lambda: NOW)


def add_ideas(session_factory, *notes) -> list[int]:
    with session_factory() as s:
        ideas = [ContentRepository(s).add_idea(note) for note in notes]
        s.commit()
        return [i.id for i in ideas]


def counts(session_factory) -> tuple[int, int]:
    with session_factory() as s:
        repo = ContentRepository(s)
        return len(repo.list_ideas()), len(repo.list_post_rows())


def test_cada_sugerencia_apunta_a_una_idea_real(session_factory):
    # list_ideas devuelve las sin usar primero y las más nuevas primero: nota 1 = "tercera", 2 = "segunda", 3 = "primera"
    ids = add_ideas(session_factory, "primera", "segunda", "tercera")
    writer = ScriptedWriter(lambda n, user: f"1|{Q.format('Redis')}\n3|{Q.format('pytest')}")
    with session_factory() as session:
        result = make_service(session, writer).suggest_ideas()

    assert [(s.question, s.idea_id, s.idea_note) for s in result] == [
        (Q.format("Redis"), ids[2], "tercera"),
        (Q.format("pytest"), ids[0], "primera"),
    ]


def test_el_modelo_recibe_las_ideas_numeradas_y_el_prompt_de_sugerencias(session_factory):
    add_ideas(session_factory, "migré un cron")
    writer = ScriptedWriter(lambda n, user: f"1|{Q.format('el cron')}")
    with session_factory() as session:
        make_service(session, writer).suggest_ideas()

    call = writer.calls[0]
    assert call["user"] == "Idea bank:\n1. migré un cron"
    assert "NEVER suggest new topics" in call["system"]
    assert call["model"] == "llama3.1:8b"


def test_no_guarda_nada(session_factory):
    add_ideas(session_factory, "una", "otra")
    writer = ScriptedWriter(lambda n, user: f"1|{Q.format('una')}\n2|{Q.format('otra')}")
    with session_factory() as session:
        make_service(session, writer).suggest_ideas()

    assert counts(session_factory) == (2, 0)  # ni ideas nuevas ni posts; las ideas siguen sin usar
    with session_factory() as s:
        assert not any(i.used for i in ContentRepository(s).list_ideas())


def test_sin_ideas_avisa_y_ni_llama_al_modelo(session_factory):
    with session_factory() as session:
        with pytest.raises(NoIdeasToSuggestFromError):
            make_service(session, NoWriter()).suggest_ideas()


def test_no_hay_transaccion_abierta_mientras_espera_al_modelo(session_factory):
    add_ideas(session_factory, "algo")
    with session_factory() as session:
        observed = []

        def script(n, user):
            observed.append(session.in_transaction())
            return f"1|{Q.format('algo')}"

        make_service(session, ScriptedWriter(script)).suggest_ideas()

    assert observed == [False]


def test_si_la_primera_respuesta_no_sirve_reintenta(session_factory):
    add_ideas(session_factory, "algo")
    answers = ["Claro, acá tenés ideas: escribí sobre liderazgo.", f"1|{Q.format('algo')}"]
    writer = ScriptedWriter(lambda n, user: answers[n - 1])
    with session_factory() as session:
        result = make_service(session, writer).suggest_ideas()

    assert len(writer.calls) == 2
    assert [s.question for s in result] == [Q.format("algo")]


def test_si_ninguna_respuesta_sirve_falla_con_un_error_claro(session_factory):
    add_ideas(session_factory, "algo")
    writer = ScriptedWriter(lambda n, user: "99|Deberías escribir sobre liderazgo.")  # nota inexistente y no es pregunta
    with session_factory() as session:
        with pytest.raises(NoUsefulSuggestionsError):
            make_service(session, writer).suggest_ideas()

    assert len(writer.calls) == 3  # MAX_ATTEMPTS
    assert counts(session_factory) == (1, 0)


def test_un_error_del_modelo_se_propaga_como_error_de_dominio(session_factory):
    add_ideas(session_factory, "algo")

    def script(n, user):
        raise ExternalServiceError("Ollama no responde.")

    with session_factory() as session:
        with pytest.raises(ExternalServiceError, match="Ollama no responde"):
            make_service(session, ScriptedWriter(script)).suggest_ideas()


def test_al_modelo_se_le_muestran_a_lo_sumo_las_ideas_del_tope(session_factory):
    add_ideas(session_factory, *[f"idea {n}" for n in range(SUGGESTION_SOURCE_LIMIT + 5)])
    writer = ScriptedWriter(lambda n, user: f"1|{Q.format('algo')}")
    with session_factory() as session:
        make_service(session, writer).suggest_ideas()

    assert writer.calls[0]["user"].count("\n") == SUGGESTION_SOURCE_LIMIT  # encabezado + N líneas
