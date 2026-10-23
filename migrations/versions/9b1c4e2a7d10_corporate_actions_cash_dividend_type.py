"""corporate_actions: allow action_type cash_dividend (explains ex-dividend drops; never adjusts prices)

Revision ID: 9b1c4e2a7d10
Revises: 32dfda976ce6
Create Date: 2026-10-21

"""
from alembic import op

revision = "9b1c4e2a7d10"
down_revision = "32dfda976ce6"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(op.f("ck_corporate_actions_action_type_valid"), "corporate_actions", type_="check")
    op.create_check_constraint("action_type_valid", "corporate_actions",
                               "action_type IN ('bonus', 'split', 'consolidation', 'cash_dividend')")


def downgrade():
    op.execute("DELETE FROM corporate_actions WHERE action_type = 'cash_dividend'")
    op.drop_constraint(op.f("ck_corporate_actions_action_type_valid"), "corporate_actions", type_="check")
    op.create_check_constraint("action_type_valid", "corporate_actions",
                               "action_type IN ('bonus', 'split', 'consolidation')")
