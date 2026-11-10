"""Registro de models: importa las tablas de TODOS los módulos en un solo lugar.

Alembic compara `Base.metadata` con la base real. Pero `Base.metadata` solo conoce las tablas
cuyos models ya fueron importados. Este archivo existe para garantizar que lo estén.
`migrations/env.py` importa solo este archivo: cuando nazca un módulo con tablas, se suma acá.
"""
from app.modules.content import models as content_models  # noqa: F401
from app.modules.jobs import models as jobs_models  # noqa: F401  (todavía sin tablas)
