"""degraded on validation_cells: answers given without some of the request's logged settings

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('validation_cells', sa.Column('degraded', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('validation_cells') as b:
        b.drop_column('degraded')
