"""token columns on validation_cells

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

COLUMNS = ("input_tokens", "output_tokens", "reasoning_tokens", "output_chars")


def upgrade() -> None:
    for c in COLUMNS:
        op.add_column('validation_cells', sa.Column(c, sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('validation_cells') as b:
        for c in COLUMNS:
            b.drop_column(c)
