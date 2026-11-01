from alembic import op
import sqlalchemy as sa

revision = '002'
down_revision = '001'


def upgrade():
    op.add_column('parser_runs', sa.Column('requests', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    op.drop_column('parser_runs', 'requests')
