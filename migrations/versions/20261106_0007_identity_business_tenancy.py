"""Add identity/business foundation and tenant ownership to financial records.

Revision ID: 20261106_0007
Revises: 20261105_0006
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261106_0007"
down_revision: Union[str, None] = "20261105_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEMO_BUSINESS_ID = "00000000-0000-4000-8000-000000000001"
FINANCIAL_TABLES = (
    "invoices", "bank_lines", "matches", "match_events", "expected_vendors",
    "clarification_requests", "clarification_receipts", "chase_events",
)

# Explicit orphan checks make the migration fail with a useful, non-destructive
# error before it creates tables or alters existing financial records.
PREFLIGHT_QUERIES = (
    ("invoices.expected_vendor_id", "SELECT count(*) FROM invoices c LEFT JOIN expected_vendors p ON p.id=c.expected_vendor_id WHERE c.expected_vendor_id IS NOT NULL AND p.id IS NULL"),
    ("matches.invoice_id", "SELECT count(*) FROM matches c LEFT JOIN invoices p ON p.id=c.invoice_id WHERE p.id IS NULL"),
    ("matches.bank_line_id", "SELECT count(*) FROM matches c LEFT JOIN bank_lines p ON p.id=c.bank_line_id WHERE c.bank_line_id IS NOT NULL AND p.id IS NULL"),
    ("match_events.match_id", "SELECT count(*) FROM match_events c LEFT JOIN matches p ON p.id=c.match_id WHERE p.id IS NULL"),
    ("match_events.invoice_id", "SELECT count(*) FROM match_events c LEFT JOIN invoices p ON p.id=c.invoice_id WHERE p.id IS NULL"),
    ("match_events.previous_bank_line_id", "SELECT count(*) FROM match_events c LEFT JOIN bank_lines p ON p.id=c.previous_bank_line_id WHERE c.previous_bank_line_id IS NOT NULL AND p.id IS NULL"),
    ("match_events.new_bank_line_id", "SELECT count(*) FROM match_events c LEFT JOIN bank_lines p ON p.id=c.new_bank_line_id WHERE c.new_bank_line_id IS NOT NULL AND p.id IS NULL"),
    ("clarification_requests.bank_line_id", "SELECT count(*) FROM clarification_requests c LEFT JOIN bank_lines p ON p.id=c.bank_line_id WHERE p.id IS NULL"),
    ("clarification_receipts.clarification_request_id", "SELECT count(*) FROM clarification_receipts c LEFT JOIN clarification_requests p ON p.id=c.clarification_request_id WHERE p.id IS NULL"),
    ("chase_events.invoice_id", "SELECT count(*) FROM chase_events c LEFT JOIN invoices p ON p.id=c.invoice_id WHERE c.invoice_id IS NOT NULL AND p.id IS NULL"),
    ("chase_events.bank_line_id", "SELECT count(*) FROM chase_events c LEFT JOIN bank_lines p ON p.id=c.bank_line_id WHERE c.bank_line_id IS NOT NULL AND p.id IS NULL"),
    ("chase_events.match_id", "SELECT count(*) FROM chase_events c LEFT JOIN matches p ON p.id=c.match_id WHERE c.match_id IS NOT NULL AND p.id IS NULL"),
    ("chase_events.clarification_request_id", "SELECT count(*) FROM chase_events c LEFT JOIN clarification_requests p ON p.id=c.clarification_request_id WHERE c.clarification_request_id IS NOT NULL AND p.id IS NULL"),
)


def preflight(connection) -> None:
    """Reject orphaned legacy references before any schema/data changes."""
    for relation, query in PREFLIGHT_QUERIES:
        count = connection.execute(sa.text(query)).scalar_one()
        if count:
            raise RuntimeError(
                f"Cannot apply 20261106_0007: {count} orphaned reference(s) in {relation}; "
                "no records were reassigned or removed. Repair the references and retry."
            )


def upgrade() -> None:
    bind = op.get_bind()
    preflight(bind)

    op.create_table(
        "businesses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_name", sa.String(255), nullable=False),
        sa.Column("proprietor_name", sa.String(255), nullable=False),
        sa.Column("gstin", sa.String(15)),
        sa.Column("business_type", sa.String(64), nullable=False),
        sa.Column("business_email", sa.String(320), nullable=False),
        sa.Column("business_phone", sa.String(32), nullable=False),
        sa.Column("address_line_1", sa.String(255), nullable=False),
        sa.Column("address_line_2", sa.String(255)),
        sa.Column("city", sa.String(128), nullable=False),
        sa.Column("state", sa.String(128), nullable=False),
        sa.Column("pin_code", sa.String(16), nullable=False),
        sa.Column("country", sa.String(2), server_default=sa.text("'IN'"), nullable=False),
        sa.Column("gst_registration_status", sa.String(32), server_default=sa.text("'unregistered'"), nullable=False),
        sa.Column("is_demo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("gst_registration_status IN ('unregistered', 'pending', 'registered', 'not_applicable')", name="ck_businesses_valid_gst_status"),
        sa.PrimaryKeyConstraint("id", name="pk_businesses"),
    )
    op.create_index("uq_businesses_single_demo", "businesses", ["is_demo"], unique=True, postgresql_where=sa.text("is_demo IS TRUE"))

    # This row explicitly labels legacy evidence as demo data; it does not invent
    # a person or account and cannot be claimed through an HTTP endpoint.
    op.execute(sa.text("""
        INSERT INTO businesses (
            id, business_name, proprietor_name, business_type, business_email,
            business_phone, address_line_1, city, state, pin_code, country,
            gst_registration_status, is_demo
        ) VALUES (
            CAST(:id AS uuid), 'LedgerRoom Demo Business', 'Unassigned demo data',
            'demo', 'demo@ledgerroom.invalid', 'Not provided',
            'Legacy records; business owner not assigned', 'Not provided',
            'Not provided', '000000', 'IN', 'not_applicable', true
        )
    """).bindparams(id=DEMO_BUSINESS_ID))

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("email_normalized", sa.String(320), nullable=False),
        sa.Column("mobile_number", sa.String(32), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True)),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index("uq_users_email_normalized", "users", ["email_normalized"], unique=True)

    op.create_table(
        "business_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("role IN ('owner', 'accountant')", name="ck_business_memberships_valid_role"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_business_memberships_valid_status"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT", name="fk_business_memberships_business"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", name="fk_business_memberships_user"),
        sa.PrimaryKeyConstraint("id", name="pk_business_memberships"),
        sa.UniqueConstraint("business_id", "user_id", name="uq_business_memberships_business_user"),
    )

    op.create_table(
        "accountant_profiles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("firm_name", sa.String(255)),
        sa.Column("office_address", sa.String(1000)),
        sa.Column("professional_qualification", sa.String(255), nullable=False),
        sa.Column("professional_registration_number", sa.String(128)),
        sa.Column("icai_membership_number", sa.String(64)),
        sa.Column("verification_status", sa.String(32), server_default=sa.text("'unverified'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("verification_status IN ('unverified', 'pending', 'verified', 'rejected')", name="ck_accountant_profiles_valid_verification_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", name="fk_accountant_profiles_user"),
        sa.PrimaryKeyConstraint("user_id", name="pk_accountant_profiles"),
    )

    op.create_table(
        "business_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invited_by_membership_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("email_normalized", sa.String(320), nullable=False),
        sa.Column("role", sa.String(24), server_default=sa.text("'accountant'"), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("role = 'accountant'", name="ck_business_invitations_accountant_only_role"),
        sa.CheckConstraint("status IN ('pending', 'accepted', 'revoked', 'expired')", name="ck_business_invitations_valid_status"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT", name="fk_business_invitations_business"),
        sa.ForeignKeyConstraint(["invited_by_membership_id"], ["business_memberships.id"], ondelete="RESTRICT", name="fk_business_invitations_inviter"),
        sa.PrimaryKeyConstraint("id", name="pk_business_invitations"),
        sa.UniqueConstraint("token_hash", name="uq_business_invitations_token_hash"),
    )
    op.create_index("ix_business_invitations_business_email", "business_invitations", ["business_id", "email_normalized"])

    op.create_table(
        "auth_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("purpose IN ('email_verification', 'password_reset', 'refresh')", name="ck_auth_tokens_valid_purpose"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", name="fk_auth_tokens_user"),
        sa.PrimaryKeyConstraint("id", name="pk_auth_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_auth_tokens_token_hash"),
    )
    op.create_index("ix_auth_tokens_user_purpose", "auth_tokens", ["user_id", "purpose"])

    op.create_table(
        "external_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("external_user_id", sa.String(128), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", name="fk_external_identities_user"),
        sa.PrimaryKeyConstraint("id", name="pk_external_identities"),
        sa.UniqueConstraint("provider", "workspace_id", "external_user_id", name="uq_external_identities_provider_workspace_user"),
    )
    op.create_index("ix_external_identities_user_id", "external_identities", ["user_id"])

    # Add nullable ownership columns first. Every existing record is explicitly
    # assigned to the one labeled demo tenant; all primary UUIDs and payloads
    # remain untouched.
    for table in FINANCIAL_TABLES:
        op.add_column(table, sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.execute(sa.text(f"UPDATE {table} SET business_id = CAST(:business_id AS uuid) WHERE business_id IS NULL").bindparams(business_id=DEMO_BUSINESS_ID))
        op.alter_column(table, "business_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
        op.alter_column(
            table, "business_id", existing_type=postgresql.UUID(as_uuid=True),
            server_default=sa.text(f"'{DEMO_BUSINESS_ID}'::uuid"),
        )
        op.create_foreign_key(f"fk_{table}_business", table, "businesses", ["business_id"], ["id"], ondelete="RESTRICT")
        op.create_unique_constraint(f"uq_{table}_id_business", table, ["id", "business_id"])

    # Tenant-scoped vendor names replace the former global uniqueness rule.
    op.drop_constraint("uq_expected_vendors_normalized_name", "expected_vendors", type_="unique")
    op.create_unique_constraint("uq_expected_vendors_business_name", "expected_vendors", ["business_id", "normalized_name"])

    tenant_fks = (
        ("invoices", "fk_invoices_vendor_business", ["expected_vendor_id", "business_id"], "expected_vendors", ["id", "business_id"]),
        ("matches", "fk_matches_invoice_business", ["invoice_id", "business_id"], "invoices", ["id", "business_id"]),
        ("matches", "fk_matches_bank_business", ["bank_line_id", "business_id"], "bank_lines", ["id", "business_id"]),
        ("match_events", "fk_match_events_match_business", ["match_id", "business_id"], "matches", ["id", "business_id"]),
        ("match_events", "fk_match_events_invoice_business", ["invoice_id", "business_id"], "invoices", ["id", "business_id"]),
        ("match_events", "fk_match_events_previous_bank_business", ["previous_bank_line_id", "business_id"], "bank_lines", ["id", "business_id"]),
        ("match_events", "fk_match_events_new_bank_business", ["new_bank_line_id", "business_id"], "bank_lines", ["id", "business_id"]),
        ("clarification_requests", "fk_clarification_bank_business", ["bank_line_id", "business_id"], "bank_lines", ["id", "business_id"]),
        ("clarification_receipts", "fk_receipts_clarification_business", ["clarification_request_id", "business_id"], "clarification_requests", ["id", "business_id"]),
        ("chase_events", "fk_chase_invoice_business", ["invoice_id", "business_id"], "invoices", ["id", "business_id"]),
        ("chase_events", "fk_chase_bank_business", ["bank_line_id", "business_id"], "bank_lines", ["id", "business_id"]),
        ("chase_events", "fk_chase_clarification_business", ["clarification_request_id", "business_id"], "clarification_requests", ["id", "business_id"]),
        ("chase_events", "fk_chase_match_business", ["match_id", "business_id"], "matches", ["id", "business_id"]),
    )
    for source, name, columns, target, target_columns in tenant_fks:
        op.create_foreign_key(name, source, target, columns, target_columns)

    # Validate the newly assigned ownership graph before completing the revision.
    for source, name, columns, target, target_columns in tenant_fks:
        source_id, source_tenant = columns
        target_id, target_tenant = target_columns
        # Optional relationships may have a NULL record ID; these are valid.
        op.execute(sa.text(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {source} s LEFT JOIN {target} t "
            f"ON t.{target_id}=s.{source_id} AND t.{target_tenant}=s.{source_tenant} "
            f"WHERE s.{source_id} IS NOT NULL AND t.{target_id} IS NULL) THEN "
            f"RAISE EXCEPTION 'Tenant ownership preflight failed for {name}; no migration data was discarded'; END IF; END $$;"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    # Tenant assignments are evidence. Refuse downgrade once any rows exist so
    # this revision can never silently remove ownership or user data.
    for table in FINANCIAL_TABLES:
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one():
            raise RuntimeError(f"Cannot downgrade 20261106_0007: {table} contains records with tenant ownership.")
    for table in ("external_identities", "auth_tokens", "business_invitations", "accountant_profiles", "business_memberships", "users"):
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one():
            raise RuntimeError(f"Cannot downgrade 20261106_0007: {table} contains identity or membership records.")

    tenant_fk_names = [item[1] for item in (
        ("invoices", "fk_invoices_vendor_business"), ("matches", "fk_matches_invoice_business"),
        ("matches", "fk_matches_bank_business"), ("match_events", "fk_match_events_match_business"),
        ("match_events", "fk_match_events_invoice_business"), ("match_events", "fk_match_events_previous_bank_business"),
        ("match_events", "fk_match_events_new_bank_business"), ("clarification_requests", "fk_clarification_bank_business"),
        ("clarification_receipts", "fk_receipts_clarification_business"), ("chase_events", "fk_chase_invoice_business"),
        ("chase_events", "fk_chase_bank_business"), ("chase_events", "fk_chase_clarification_business"),
        ("chase_events", "fk_chase_match_business"),
    )]
    source_by_constraint = {name: table for table, name, *_ in (
        ("invoices", "fk_invoices_vendor_business", None, None, None),
        ("matches", "fk_matches_invoice_business", None, None, None),
        ("matches", "fk_matches_bank_business", None, None, None),
        ("match_events", "fk_match_events_match_business", None, None, None),
        ("match_events", "fk_match_events_invoice_business", None, None, None),
        ("match_events", "fk_match_events_previous_bank_business", None, None, None),
        ("match_events", "fk_match_events_new_bank_business", None, None, None),
        ("clarification_requests", "fk_clarification_bank_business", None, None, None),
        ("clarification_receipts", "fk_receipts_clarification_business", None, None, None),
        ("chase_events", "fk_chase_invoice_business", None, None, None),
        ("chase_events", "fk_chase_bank_business", None, None, None),
        ("chase_events", "fk_chase_clarification_business", None, None, None),
        ("chase_events", "fk_chase_match_business", None, None, None),
    )}
    for constraint in reversed(tenant_fk_names):
        op.drop_constraint(constraint, source_by_constraint[constraint], type_="foreignkey")
    op.create_unique_constraint("uq_expected_vendors_normalized_name", "expected_vendors", ["normalized_name"])
    op.drop_constraint("uq_expected_vendors_business_name", "expected_vendors", type_="unique")
    for table in reversed(FINANCIAL_TABLES):
        op.drop_constraint(f"uq_{table}_id_business", table, type_="unique")
        op.drop_constraint(f"fk_{table}_business", table, type_="foreignkey")
        op.drop_column(table, "business_id")
    for table in ("external_identities", "auth_tokens", "business_invitations", "accountant_profiles", "business_memberships", "users"):
        op.drop_table(table)
    op.drop_index("uq_businesses_single_demo", table_name="businesses")
    op.drop_table("businesses")
