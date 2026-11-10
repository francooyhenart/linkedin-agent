"""El último snapshot de cada post se elige en la base (DISTINCT ON), no en Python."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects import postgresql

from app.modules.content.models import PostStatus
from app.modules.content.repository import ContentRepository

BASE = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def test_la_consulta_usa_distinct_on(session):
    sql = str(ContentRepository(session)._posts_with_latest_metric().compile(dialect=postgresql.dialect()))
    assert "DISTINCT ON (post_metrics.post_id)" in sql


def test_devuelve_el_snapshot_mas_reciente_de_cada_post(session):
    repo = ContentRepository(session)
    a = repo.add_post("A", BASE, PostStatus.PUBLISHED, None)
    b = repo.add_post("B", BASE + timedelta(days=1), PostStatus.PUBLISHED, None)
    sin_metricas = repo.add_post("C", BASE + timedelta(days=2), PostStatus.DRAFT, None)

    # Se insertan fuera de orden a propósito: el "último" es por captured_at, no por orden de inserción.
    repo.add_metric(a.id, BASE + timedelta(days=5), {"impressions": 500})
    repo.add_metric(a.id, BASE + timedelta(days=1), {"impressions": 100})
    repo.add_metric(a.id, BASE + timedelta(days=3), {"impressions": 300})
    repo.add_metric(b.id, BASE + timedelta(days=2), {"impressions": 40})

    rows = {row.post.id: row for row in repo.list_post_rows()}

    assert len(rows) == 3  # los posts sin métricas también aparecen (LEFT JOIN)
    assert rows[a.id].latest_metric.impressions == 500
    assert rows[b.id].latest_metric.impressions == 40
    assert rows[sin_metricas.id].latest_metric is None


def test_get_post_row_tambien_trae_el_ultimo_snapshot(session):
    repo = ContentRepository(session)
    post = repo.add_post("A", BASE, PostStatus.PUBLISHED, None)
    repo.add_metric(post.id, BASE + timedelta(days=1), {"impressions": 10})
    repo.add_metric(post.id, BASE + timedelta(days=2), {"impressions": 20})

    assert repo.get_post_row(post.id).latest_metric.impressions == 20
    assert repo.get_post_row(9999) is None


def test_el_historial_completo_queda_ordenado_de_viejo_a_nuevo(session):
    repo = ContentRepository(session)
    post = repo.add_post("A", BASE, PostStatus.PUBLISHED, None)
    repo.add_metric(post.id, BASE + timedelta(days=3), {"impressions": 3})
    repo.add_metric(post.id, BASE + timedelta(days=1), {"impressions": 1})

    assert [m.impressions for m in repo.list_metrics(post.id)] == [1, 3]


def test_el_repository_nunca_hace_commit(session_factory):
    with session_factory() as s:
        ContentRepository(s).add_idea("no debería quedar guardada")
        # Sin commit del service: al cerrar la sesión se deshace todo.
    with session_factory() as s:
        assert ContentRepository(s).list_ideas() == []
