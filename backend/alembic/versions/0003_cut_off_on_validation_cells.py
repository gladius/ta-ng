"""cut_off on validation_cells: answers that stopped at the output cap

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('validation_cells', sa.Column('cut_off', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('validation_cells') as b:
        b.drop_column('cut_off')
