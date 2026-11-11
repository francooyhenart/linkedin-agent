"""Conexión a PostgreSQL con SQLAlchemy 2.x en modo sincrónico.

Piezas:
- engine:         el "pool" de conexiones a la base (se crea una sola vez).
- SessionLocal:   fábrica de sesiones. Una sesión = una conversación con la base (una unidad de trabajo).
- Base:           clase madre de todos los modelos (tablas).
- get_session():  dependencia de FastAPI: abre una sesión por request y la cierra al terminar.

Regla de arquitectura: solo los repository usan la sesión. Los routers y services nunca.
"""
from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import DateTime, MetaData, create_engine, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core.config import get_settings

# Convención de nombres: SQLAlchemy/Alembic le ponen nombre a cada constraint e índice siguiendo
# estos moldes. Sin esto, Postgres inventa nombres y Alembic no puede borrarlos en un downgrade.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Todos los modelos de todos los módulos heredan de esta clase."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Agrega created_at y updated_at (timestamptz) a cualquier tabla que lo herede.

    Los valores los pone la base (now()), no Python: así son consistentes aunque inserte otro programa.
    """

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# pool_pre_ping: antes de usar una conexión del pool prueba que siga viva (útil si reiniciás Postgres).
# connect_timeout: si la base no contesta en 3 segundos, falla en vez de quedarse colgado
# (así /health puede responder "error" en lugar de no responder nunca).
engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    # Si una consulta falla, SQLAlchemy mete los parámetros en el mensaje de error. Con esto no: así un token
    # de LinkedIn (que se guarda con un INSERT en settings) nunca puede terminar escrito en un log.
    hide_parameters=True,
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
