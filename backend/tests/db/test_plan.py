"""Planificar y generar, a nivel service y contra Postgres real.

Foco: no mantener una transacción abierta mientras se espera al modelo, guardar lo que salió bien y
devolver qué ideas fallaron y por qué.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import ExternalServiceError
from app.modules.content.exceptions import NoPublishDaysError, NoUnusedIdeasError
from app.modules.content.models import PostStatus
from app.modules.content.repository import ContentRepository
from app.modules.content.schemas import GenerateIn, SettingsUpdate
from app.modules.content.service import ContentService
from tests.helpers import TZ
from tests.test_support import ScriptedWriter

# Martes 10 de noviembre de 2026, 10:00 (hora de Buenos Aires). Con días "1,3" (mar y jue) a las 09:00,
# los próximos días libres son el jueves 12 y el martes 17.
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)


def make_service(session, writer):
    return ContentService(session, writer, TZ, "llama3.1:8b", now=lambda: NOW)


def add_ideas(session_factory, *notes) -> list[int]:
    with session_factory() as s:
        ideas = [ContentRepository(s).add_idea(note) for note in notes]
        s.commit()
        return [i.id for i in ideas]


def idea_used(session_factory, idea_id) -> bool:
    with session_factory() as s:
        return ContentRepository(s).get_idea(idea_id).used


def post_count(session_factory) -> int:
    with session_factory() as s:
        return len(ContentRepository(s).list_post_rows())


# ---------------------------------------------------------------- sin transacción abierta mientras escribe el modelo
def test_plan_no_tiene_transaccion_abierta_mientras_espera_al_modelo(session_factory):
    add_ideas(session_factory, "uno", "dos")
    with session_factory() as session:
        observed = []

        def script(n, user):
            observed.append(session.in_transaction())  # ¿hay una transacción abierta AHORA, mientras "escribe"?
            return f"Draft {n}."

        make_service(session, ScriptedWriter(script)).plan_next_posts()

    assert observed == [False, False]


def test_generate_tampoco_tiene_transaccion_abierta(session_factory):
    with session_factory() as session:
        observed = []

        def script(n, user):
            observed.append(session.in_transaction())
            return "Un texto."

        result = make_service(session, ScriptedWriter(script)).generate(GenerateIn(note="algo"))

    assert observed == [False]
    assert result.text == "Un texto."


def test_los_reintentos_de_generate_tampoco_la_abren(session_factory):
    with session_factory() as session:
        observed = []

        def script(n, user):
            observed.append(session.in_transaction())
            return "Todavía suena a journey." if n < 3 else "Ahora sí."  # 'journey' está prohibida

        result = make_service(session, ScriptedWriter(script)).generate(GenerateIn(note="algo"))

    assert observed == [False, False, False]
    assert (result.text, result.warnings) == ("Ahora sí.", [])


def test_generate_usa_los_ajustes_de_la_base_sin_leerlos_durante_la_espera(session_factory):
    with session_factory() as session:
        service = make_service(session, writer := ScriptedWriter())
        service.update_settings(SettingsUpdate(max_chars=150, model="mistral", style_examples="Mi estilo"))
        service.generate(GenerateIn(note="algo"))

    call = writer.calls[0]
    assert call["model"] == "mistral"
    assert "Maximum 150 characters" in call["system"] and "Mi estilo" in call["system"]


# ---------------------------------------------------------------- camino feliz
def test_plan_crea_un_borrador_por_idea_en_dias_libres(session_factory):
    ideas = add_ideas(session_factory, "uno", "dos", "tres")
    with session_factory() as session:
        result = make_service(session, ScriptedWriter()).plan_next_posts()

    assert result.failed == []
    assert [format(r.post.scheduled_at.astimezone(TZ), "%Y-%m-%d %H:%M") for r in result.created] == [
        "2026-11-12 09:00",
        "2026-11-17 09:00",
    ]
    assert {r.post.status for r in result.created} == {PostStatus.DRAFT}
    assert [idea_used(session_factory, i) for i in ideas] == [True, True, False]  # solo las 2 que entraban


def test_plan_sin_dias_o_sin_ideas(session_factory):
    with session_factory() as session:
        service = make_service(session, ScriptedWriter())
        with pytest.raises(NoUnusedIdeasError):
            service.plan_next_posts()
        service.update_settings(SettingsUpdate(post_days="x"))
        with pytest.raises(NoPublishDaysError):
            service.plan_next_posts()


# ---------------------------------------------------------------- lo que sale bien se guarda; lo que falla se informa
def test_si_el_modelo_falla_con_una_idea_se_guardan_las_demas_y_se_informa_cual_fallo(session_factory):
    uno, dos = add_ideas(session_factory, "uno", "dos")

    def script(n, user):
        if n == 1:
            raise ExternalServiceError("Ollama tardó más de 300 segundos en responder.")
        return "Draft bueno."

    with session_factory() as session:
        result = make_service(session, ScriptedWriter(script)).plan_next_posts()

    assert [r.post.idea_id for r in result.created] == [dos]
    assert [(f.idea_id, f.reason) for f in result.failed] == [(uno, "Ollama tardó más de 300 segundos en responder.")]
    assert idea_used(session_factory, uno) is False  # la que falló queda disponible para el próximo intento
    assert idea_used(session_factory, dos) is True
    assert post_count(session_factory) == 1


def test_si_el_modelo_falla_con_todas_no_se_guarda_nada_y_se_informa_cada_una(session_factory):
    ideas = add_ideas(session_factory, "uno", "dos")

    def script(n, user):
        raise ExternalServiceError("No pude hablar con Ollama en http://127.0.0.1:11434. ¿Está abierto?")

    with session_factory() as session:
        result = make_service(session, ScriptedWriter(script)).plan_next_posts()

    assert result.created == []
    assert sorted(f.idea_id for f in result.failed) == sorted(ideas)
    assert all("¿Está abierto?" in f.reason for f in result.failed)
    assert post_count(session_factory) == 0
    assert not any(idea_used(session_factory, i) for i in ideas)


# ---------------------------------------------------------------- revalidación al guardar (pasó tiempo entre leer y guardar)
def test_si_la_idea_se_borra_mientras_se_escribe_el_borrador_falla_solo_esa(session_factory):
    uno, dos = add_ideas(session_factory, "uno", "dos")

    def script(n, user):
        if n == 1:  # mientras "escribe" la primera, el usuario borra esa idea desde el panel
            with session_factory() as other:
                ContentRepository(other).delete_idea(uno)
                other.commit()
        return "Draft."

    with session_factory() as session:
        result = make_service(session, ScriptedWriter(script)).plan_next_posts()

    assert [r.post.idea_id for r in result.created] == [dos]
    assert [(f.idea_id, f.reason) for f in result.failed] == [(uno, "La idea se borró mientras se escribía el borrador.")]


def test_si_la_idea_se_usa_mientras_se_escribe_el_borrador_no_se_duplica(session_factory):
    uno, dos = add_ideas(session_factory, "uno", "dos")

    def script(n, user):
        if n == 1:  # otro pedido (o el usuario a mano) se llevó la idea 'uno'
            with session_factory() as other:
                repo = ContentRepository(other)
                repo.mark_idea_used(repo.get_idea(uno))
                other.commit()
        return "Draft."

    with session_factory() as session:
        result = make_service(session, ScriptedWriter(script)).plan_next_posts()

    assert [f.reason for f in result.failed] == ["La idea ya se usó mientras se escribía el borrador."]
    assert [r.post.idea_id for r in result.created] == [dos]


def test_si_el_dia_se_ocupa_mientras_se_escribe_el_borrador_falla_solo_esa_idea(session_factory):
    uno, dos = add_ideas(session_factory, "uno", "dos")

    def script(n, user):
        if n == 1:  # alguien agenda un post a mano el jueves 12 (el día de la primera idea)
            with session_factory() as other:
                ContentRepository(other).add_post(
                    "a mano", datetime(2026, 11, 12, 18, 0, tzinfo=TZ), PostStatus.DRAFT, None
                )
                other.commit()
        return "Draft."

    with session_factory() as session:
        result = make_service(session, ScriptedWriter(script)).plan_next_posts()

    assert [(f.idea_id, "se ocupó" in f.reason) for f in result.failed] == [(uno, True)]
    assert [r.post.idea_id for r in result.created] == [dos]
    assert idea_used(session_factory, uno) is False
    assert post_count(session_factory) == 2  # el de a mano + el de la idea 'dos'


def test_un_borrador_que_no_se_guarda_no_deja_la_idea_marcada_como_usada(session_factory):
    """Cada post es atómico: o se guarda el post Y se marca la idea, o ninguna de las dos cosas."""
    (uno,) = add_ideas(session_factory, "uno")

    def script(n, user):
        with session_factory() as other:
            ContentRepository(other).add_post("a mano", datetime(2026, 11, 12, 9, 0, tzinfo=TZ), PostStatus.DRAFT, None)
            other.commit()
        return "Draft."

    with session_factory() as session:
        result = make_service(session, ScriptedWriter(script)).plan_next_posts()

    assert result.created == [] and len(result.failed) == 1
    assert idea_used(session_factory, uno) is False
    assert post_count(session_factory) == 1


# ---------------------------------------------------------------- el reloj
def test_plan_respeta_dias_ocupados_que_ya_existian(session_factory):
    add_ideas(session_factory, "uno", "dos")
    with session_factory() as s:
        ContentRepository(s).add_post("ya hay uno", datetime(2026, 11, 12, 20, 0, tzinfo=TZ), PostStatus.APPROVED, None)
        s.commit()
    with session_factory() as session:
        result = make_service(session, ScriptedWriter()).plan_next_posts()
    assert [format(r.post.scheduled_at.astimezone(TZ), "%Y-%m-%d") for r in result.created] == ["2026-11-17", "2026-11-19"]
