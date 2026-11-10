"""estado publishing en ck_posts_status_valid

Agrega el estado 'publishing' ("la app está llamando a LinkedIn") a la restricción CHECK de posts.status.

ESCRITA A MANO: Alembic autogenerate NO compara las restricciones CHECK, así que `alembic check` dice
"no hay cambios" aunque el modelo y la base difieran. Por eso hay que revisar esto con
    SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_posts_status_valid';

Revision ID: d85ea00df7db
Revises: 092eff170631
Create Date: 2026-11-10 16:07:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d85ea00df7db"
down_revision: Union[str, Sequence[str], None] = "092eff170631"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# op.f(...) le dice a Alembic "este nombre ya es el definitivo, no le apliques la convención de nombres".
# Sin op.f el nombre saldría duplicado: ck_posts_ck_posts_status_valid.
CONSTRAINT = op.f("ck_posts_status_valid")

NEW_VALUES = "'draft', 'approved', 'ready', 'publishing', 'published', 'failed'"
OLD_VALUES = "'draft', 'approved', 'ready', 'published', 'failed'"


def upgrade() -> None:
    # Dos pasos, dentro de la misma transacción (Postgres permite DDL transaccional).
    op.drop_constraint(CONSTRAINT, "posts", type_="check")
    op.create_check_constraint(CONSTRAINT, "posts", f"status IN ({NEW_VALUES})")


def downgrade() -> None:
    # Antes de volver a la restricción vieja hay que sacar las filas con el estado que ya no existe;
    # si no, el CHECK viejo las rechazaría y el downgrade fallaría.
    # Se pasan a 'ready' ("publicar a mano"), que es lo más seguro: el usuario decide, y el mensaje le avisa
    # que antes tiene que verificar en LinkedIn (no sabemos si ese post llegó a salir).
    op.execute(
        """
        UPDATE posts
        SET status = 'ready',
            error = 'Quedó en "publishing" antes de volver a una versión anterior. Verificá en LinkedIn si salió antes de publicarlo a mano.'
        WHERE status = 'publishing'
        """
    )
    op.drop_constraint(CONSTRAINT, "posts", type_="check")
    op.create_check_constraint(CONSTRAINT, "posts", f"status IN ({OLD_VALUES})")
