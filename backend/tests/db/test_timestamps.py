"""Verifica el comportamiento de created_at / updated_at contra Postgres real."""
import time

from app.modules.content.models import Idea


def test_updated_at_avanza_en_otra_transaccion_y_created_at_no(session_factory):
    # Transacción 1: se crea la idea.
    with session_factory() as s:
        idea = Idea(note="primera versión")
        s.add(idea)
        s.commit()
        idea_id, created_at, first_updated_at = idea.id, idea.created_at, idea.updated_at

    # now() de Postgres es la hora de INICIO de la transacción, así que la segunda tiene que
    # ser una transacción distinta (y pasar un rato) para que el valor cambie.
    time.sleep(0.05)

    # Transacción 2: se modifica.
    with session_factory() as s:
        idea = s.get(Idea, idea_id)
        idea.note = "segunda versión"
        s.commit()
        s.refresh(idea)

        assert idea.updated_at > first_updated_at  # lo garantiza onupdate=func.now() del mixin
        assert idea.created_at == created_at  # la fecha de creación no se toca
