"""Privileged LedgerRoom administration commands."""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from .core.config import get_settings
from .db.session import get_session_factory
from .models import Business, BusinessMembership, User
from .services.evidence_storage import inventory_orphans


def bootstrap_demo_owner(email: str) -> None:
    """Grant the one demo business to an already email-verified owner account.

    This command is deliberately local/admin-only. It is not an API route and
    cannot create a user or mark an email as verified. Run only after the user
    registration/verification flow has established the account.
    """
    normalized = email.strip().casefold()
    if not normalized:
        raise ValueError("A verified owner's email is required.")
    confirmation = input(f"Type {normalized} to confirm the demo-business bootstrap: ").strip().casefold()
    if confirmation != normalized:
        raise ValueError("Confirmation did not match; no membership was created.")

    factory = get_session_factory()
    with factory.begin() as session:
        business = session.scalar(
            select(Business).where(Business.is_demo.is_(True)).with_for_update()
        )
        if business is None:
            raise RuntimeError("The demo business is not present. Apply the reviewed identity/business migration first.")
        if session.scalar(
            select(BusinessMembership.id).where(
                BusinessMembership.business_id == business.id,
                BusinessMembership.role == "owner",
            ).limit(1)
        ) is not None:
            raise RuntimeError("The demo business already has an owner membership; bootstrap is one-time.")
        user = session.scalar(
            select(User).where(User.email_normalized == normalized).with_for_update()
        )
        if user is None or not user.is_active or user.email_verified_at is None:
            raise ValueError("No active, email-verified LedgerRoom account matches that address.")
        existing = session.scalar(
            select(BusinessMembership.id).where(
                BusinessMembership.business_id == business.id,
                BusinessMembership.user_id == user.id,
            ).limit(1)
        )
        if existing is not None:
            raise RuntimeError("This user already has a membership in the demo business.")
        session.add(BusinessMembership(business_id=business.id, user_id=user.id, role="owner", status="active"))


def main() -> int:
    parser = argparse.ArgumentParser(prog="ledgerroom-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser("bootstrap-demo-owner")
    bootstrap.add_argument("--email", required=True, help="Email of an existing active, verified user")
    orphans = subparsers.add_parser("evidence-orphans", help="Inspect old evidence objects without database references")
    orphans.add_argument("--provider", choices=("local", "s3"), default=None)
    orphans.add_argument("--retention-days", type=int, default=None)
    orphans.add_argument("--delete", action="store_true", help="Delete only unreferenced objects older than configured retention")
    args = parser.parse_args()
    try:
        if args.command == "bootstrap-demo-owner":
            bootstrap_demo_owner(args.email)
            print("Demo business owner membership created.")
            return 0
        if args.command == "evidence-orphans":
            settings = get_settings()
            provider = args.provider or settings.EVIDENCE_STORAGE_BACKEND
            factory = get_session_factory()
            with factory() as session:
                result = inventory_orphans(
                    session, provider,
                    retention_days=args.retention_days,
                    delete=args.delete,
                )
            action = "deleted" if args.delete else "found"
            print(
                f"Evidence orphan scan {action}: {result['orphan_count']}; "
                f"removed: {result['removed_count']}; retention days: {result['retention_days']}."
            )
            for key in result["orphan_keys"]:
                print(key)
            return 0
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
