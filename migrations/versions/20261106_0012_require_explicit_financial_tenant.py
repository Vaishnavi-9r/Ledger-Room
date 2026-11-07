"""Require callers to assign financial rows to an explicit business.

Revision ID: 20261106_0012
Revises: 20261106_0011
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261106_0012"
down_revision: Union[str, None] = "20261106_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEMO_BUSINESS_ID = "00000000-0000-4000-8000-000000000001"
FINANCIAL_TABLES = (
    "invoices", "bank_lines", "matches", "match_events", "expected_vendors",
    "clarification_requests", "clarification_receipts", "chase_events",
)


def upgrade() -> None:
    bind = op.get_bind()
    inconsistent_candidate = bind.execute(sa.text("""
        SELECT 1
        FROM matches m
        CROSS JOIN LATERAL jsonb_array_elements_text(
            COALESCE(m.candidate_bank_line_ids, '[]'::jsonb)
        ) AS candidate(bank_line_id)
        LEFT JOIN bank_lines b
          ON b.id = CASE
              WHEN candidate.bank_line_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
              THEN candidate.bank_line_id::uuid
              ELSE NULL
          END
         AND b.business_id = m.business_id
        WHERE b.id IS NULL
        LIMIT 1
    """)).first()
    if inconsistent_candidate is not None:
        raise RuntimeError(
            "Cannot apply 20261106_0012: a reconciliation candidate reference is invalid or belongs to another business; "
            "no records were changed. Repair the reference and retry."
        )

    # Existing business_id values are untouched. This removes only the unsafe
    # fallback that could silently assign a future insert to the demo tenant.
    for table in FINANCIAL_TABLES:
        op.alter_column(
            table, "business_id", existing_type=postgresql.UUID(as_uuid=True),
            existing_nullable=False, server_default=None,
        )


def downgrade() -> None:
    # Reversible compatibility fallback for older application versions.
    for table in FINANCIAL_TABLES:
        op.alter_column(
            table, "business_id", existing_type=postgresql.UUID(as_uuid=True),
            existing_nullable=False,
            server_default=sa.text(f"'{DEMO_BUSINESS_ID}'::uuid"),
        )
