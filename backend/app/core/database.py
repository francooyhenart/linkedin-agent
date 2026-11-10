"""Conexión a PostgreSQL con SQLAlchemy 2.0 en modo sincrónico.

Piezas:
- engine:         el "pool" de conexiones a la base (se crea una sola vez).
- SessionLocal:   fábrica de sesiones. Una sesión = una conversación con la base (una unidad de trabajo).
- Base:           clase madre de todos los modelos (tablas).
- get_session():  dependencia de FastAPI: abre una sesión por request y la cierra al terminar.

Regla de arquitectura: solo los repository usan la sesión. Los routers y services nunca.
"""
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Todos los modelos de todos los módulos heredan de esta clase."""


# pool_pre_ping: antes de usar una conexión del pool prueba que siga viva (útil si reiniciás Postgres).
# connect_timeout: si la base no contesta en 3 segundos, falla en vez de quedarse colgado
# (así /health puede responder "error" en lugar de no responder nunca).
engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 3},
)

# expire_on_commit=False: después de un commit los objetos siguen siendo legibles
# (sin esto, leer un atributo tras el commit dispara una consulta nueva).
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """Una sesión por request. El `finally` garantiza que se cierre aunque haya un error."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def check_connection() -> bool:
    """Devuelve True si Postgres responde. Lo usa GET /health.

    Es infraestructura (no toca datos de negocio), por eso vive en core y abre
    su propia conexión en vez de usar una sesión de repository.
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - cualquier fallo significa "no hay base"
        return False
