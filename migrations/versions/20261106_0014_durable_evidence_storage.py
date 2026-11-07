"""Add per-object storage metadata and business evidence quotas.

Revision ID: 20261106_0014
Revises: 20261106_0013
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261106_0014"
down_revision: Union[str, None] = "20261106_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add nullable first so all existing rows remain addressable, then backfill
    # to their current local-disk locations without moving or reading any file.
    for table in ("invoices", "clarification_receipts"):
        op.add_column(table, sa.Column("storage_provider", sa.String(length=16), nullable=True))
        op.add_column(table, sa.Column("storage_key", sa.String(length=1024), nullable=True))

    op.execute("UPDATE invoices SET storage_provider = 'local', storage_key = 'invoices/' || stored_filename")
    op.execute("UPDATE clarification_receipts SET storage_provider = 'local', storage_key = 'receipts/' || stored_filename")

    for table, provider_check, object_unique, tenant_index in (
        ("invoices", "valid_invoice_storage_provider", "uq_invoice_storage_object", "ix_invoice_evidence_business_provider"),
        ("clarification_receipts", "valid_receipt_storage_provider", "uq_receipt_storage_object", "ix_receipt_evidence_business_provider"),
    ):
        op.alter_column(table, "storage_provider", nullable=False)
        op.alter_column(table, "storage_key", nullable=False)
        op.create_check_constraint(provider_check, table, "storage_provider IN ('local', 's3')")
        op.create_unique_constraint(object_unique, table, ["storage_provider", "storage_key"])
        op.create_index(tenant_index, table, ["business_id", "storage_provider"], unique=False)

    op.add_column("businesses", sa.Column("storage_quota_bytes", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "nonnegative_storage_quota", "businesses",
        "storage_quota_bytes IS NULL OR storage_quota_bytes >= 0",
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Dropping these references while any object uses a non-legacy key could
    # orphan cloud objects or make local evidence unreachable. Require an
    # explicit data migration/review before such a downgrade.
    has_nonlegacy_invoice_keys = bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM invoices "
        "WHERE storage_provider <> 'local' OR storage_key <> 'invoices/' || stored_filename)"
    )).scalar_one()
    has_nonlegacy_receipt_keys = bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM clarification_receipts "
        "WHERE storage_provider <> 'local' OR storage_key <> 'receipts/' || stored_filename)"
    )).scalar_one()
    has_quota_overrides = bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM businesses WHERE storage_quota_bytes IS NOT NULL)"
    )).scalar_one()
    if has_nonlegacy_invoice_keys or has_nonlegacy_receipt_keys or has_quota_overrides:
        raise RuntimeError(
            "Cannot downgrade 20261106_0014 while non-legacy evidence keys or quota overrides exist. "
            "Migrate and verify evidence references before an explicitly reviewed downgrade."
        )

    op.drop_constraint("nonnegative_storage_quota", "businesses", type_="check")
    op.drop_column("businesses", "storage_quota_bytes")
    for table, provider_check, object_unique, tenant_index in (
        ("clarification_receipts", "valid_receipt_storage_provider", "uq_receipt_storage_object", "ix_receipt_evidence_business_provider"),
        ("invoices", "valid_invoice_storage_provider", "uq_invoice_storage_object", "ix_invoice_evidence_business_provider"),
    ):
        op.drop_index(tenant_index, table_name=table)
        op.drop_constraint(object_unique, table, type_="unique")
        op.drop_constraint(provider_check, table, type_="check")
        op.drop_column(table, "storage_key")
        op.drop_column(table, "storage_provider")
