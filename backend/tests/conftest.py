"""Configuración compartida de los tests.

Los tests NUNCA tocan tu base de desarrollo: usan otra base en el mismo contenedor de Postgres,
con el mismo nombre + "_test" (linkedin_agent -> linkedin_agent_test).

El truco está acá arriba, antes de importar nada de la app: cambiamos DATABASE_URL para que
`app.core.database` (que crea el engine al importarse) ya apunte a la base de test.
"""
import os

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from app.core.config import get_settings

_dev_url = make_url(get_settings().database_url)
TEST_DB_NAME = _dev_url.database if _dev_url.database.endswith("_test") else f"{_dev_url.database}_test"
_test_url = _dev_url.set(database=TEST_DB_NAME)

# lock_timeout: si una consulta espera un bloqueo de fila más de 5 segundos, Postgres la cancela con un error.
# Sin esto, un bloqueo que nunca se libera deja el test colgado para siempre. (Además hay un timeout por test.)
_test_url = _test_url.update_query_dict({"options": "-c lock_timeout=5000"})
os.environ["DATABASE_URL"] = _test_url.render_as_string(hide_password=False)
# Los tests nunca arrancan el scheduler real (publicaría posts de verdad): lo prueban llamando al service directo.
os.environ["SCHEDULER_ENABLED"] = "false"
get_settings.cache_clear()  # la próxima lectura ya ve estos valores


def _admin_dsn() -> str:
    """Conexión a la base 'postgres' (la de mantenimiento), desde donde se puede CREATE DATABASE."""
    return _dev_url.set(database="postgres", drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def test_database():
    """Crea la base de test si no existe y le aplica las migraciones (así también se prueban)."""
    try:
        with psycopg.connect(_admin_dsn(), autocommit=True, connect_timeout=3) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,)).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME)))
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres no responde (¿docker compose up -d?): {exc}")

    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    backend_dir = Path(__file__).resolve().parents[1]
    command.upgrade(Config(str(backend_dir / "alembic.ini")), "head")
    return TEST_DB_NAME


@pytest.fixture
def session_factory(test_database):
    """Fábrica de sesiones contra la base de test. Al terminar cada test, vacía las tablas."""
    from sqlalchemy import text

    from app.core.database import SessionLocal, engine

    assert engine.url.database.endswith("_test"), "¡el engine no apunta a la base de test!"

    def wipe():
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE ideas, posts, post_metrics, settings RESTART IDENTITY CASCADE"))

    wipe()
    yield SessionLocal
    wipe()


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s
