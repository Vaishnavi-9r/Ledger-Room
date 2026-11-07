"""CSV validation and transactional persistence for bank statements."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models import BankLine
from ..schemas.bank_statement import (
    BankStatementImportError,
    BankStatementImportResponse,
)

REQUIRED_HEADERS = ("transaction_date", "description", "debit", "credit", "reference")
_DATE_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RUPEE_AMOUNT = re.compile(r"^\d+(?:\.\d+)?$")
_MAX_BIGINT = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class BankStatementTransaction:
    """A validated transaction; debits are positive paise, credits negative."""

    transaction_date: date
    description: str
    amount_paise: int
    external_reference: str | None
    currency: str = "INR"

    @property
    def fingerprint(self) -> tuple[date, str, int, str]:
        """Canonical identity based on date, description, signed amount, reference."""
        normalized_description = " ".join(self.description.split()).casefold()
        normalized_reference = (self.external_reference or "").strip().casefold()
        return (
            self.transaction_date,
            normalized_description,
            self.amount_paise,
            normalized_reference,
        )


@dataclass(frozen=True, slots=True)
class ParsedBankStatement:
    total_rows: int
    transactions: list[BankStatementTransaction]
    errors: list[BankStatementImportError]


class BankStatementPersistenceError(Exception):
    """Raised when a transaction cannot be committed to bank_lines."""

    def __init__(self, total_rows: int, duplicate_rows: int) -> None:
        super().__init__("Bank statement persistence failed.")
        self.total_rows = total_rows
        self.duplicate_rows = duplicate_rows


def rupees_to_paise(value: str, field_name: str) -> int:
    """Convert a non-negative decimal rupee string to exact integer paise."""
    normalized = value.strip()
    if not normalized or not _RUPEE_AMOUNT.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a non-negative decimal rupee amount.")

    try:
        rupees = Decimal(normalized)
        if not rupees.is_finite() or rupees < 0:
            raise ValueError(f"{field_name} must be a non-negative decimal rupee amount.")
        paise = rupees * Decimal(100)
        integral_paise = paise.to_integral_value()
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError(f"{field_name} is not a valid rupee amount.") from exc

    if paise != integral_paise:
        raise ValueError(f"{field_name} cannot have fractions smaller than one paise.")
    result = int(integral_paise)
    if result > _MAX_BIGINT:
        raise ValueError(f"{field_name} exceeds the supported BIGINT range.")
    return result


def parse_bank_statement_csv(contents: bytes) -> ParsedBankStatement:
    """Parse a UTF-8 CSV, validate every row, and collect clear row errors."""
    try:
        decoded = contents.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ParsedBankStatement(
            total_rows=0,
            transactions=[],
            errors=[
                BankStatementImportError(
                    row_number=1, message="CSV must use UTF-8 text encoding."
                )
            ],
        )

    reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
    try:
        raw_headers = next(reader)
    except StopIteration:
        return ParsedBankStatement(
            total_rows=0,
            transactions=[],
            errors=[
                BankStatementImportError(
                    row_number=1, message="CSV is empty; the required header row is missing."
                )
            ],
        )
    except csv.Error:
        return ParsedBankStatement(
            total_rows=0,
            transactions=[],
            errors=[
                BankStatementImportError(
                    row_number=1, message="CSV header row is malformed."
                )
            ],
        )

    headers = [header.strip().casefold() for header in raw_headers]
    errors: list[BankStatementImportError] = []
    duplicate_headers = sorted(
        {header for header in headers if headers.count(header) > 1}
    )
    if duplicate_headers:
        errors.append(
            BankStatementImportError(
                row_number=1,
                message="CSV contains duplicate headers: " + ", ".join(duplicate_headers),
            )
        )
    missing_headers = [header for header in REQUIRED_HEADERS if header not in headers]
    if missing_headers:
        errors.append(
            BankStatementImportError(
                row_number=1,
                message="Missing required header(s): " + ", ".join(missing_headers),
            )
        )

    header_valid = not errors
    total_rows = 0
    transactions: list[BankStatementTransaction] = []
    previous_physical_line = reader.line_num

    try:
        for row in reader:
            row_number = previous_physical_line + 1
            previous_physical_line = reader.line_num
            if not row or all(not field.strip() for field in row):
                continue
            total_rows += 1

            if not header_valid:
                continue
            if len(row) != len(headers):
                errors.append(
                    BankStatementImportError(
                        row_number=row_number,
                        message=(
                            f"Expected {len(headers)} columns but found {len(row)}."
                        ),
                    )
                )
                continue

            fields = dict(zip(headers, row))
            row_errors: list[str] = []

            raw_date = fields["transaction_date"].strip()
            parsed_date: date | None = None
            if not _DATE_FORMAT.fullmatch(raw_date):
                row_errors.append("transaction_date must use YYYY-MM-DD format.")
            else:
                try:
                    parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
                except ValueError:
                    row_errors.append("transaction_date is not a valid calendar date.")

            description = fields["description"].strip()
            if not description:
                row_errors.append("description must not be empty.")

            debit_paise: int | None = None
            credit_paise: int | None = None
            for name in ("debit", "credit"):
                try:
                    amount = rupees_to_paise(fields[name], name)
                except ValueError as exc:
                    row_errors.append(str(exc))
                else:
                    if name == "debit":
                        debit_paise = amount
                    else:
                        credit_paise = amount

            if (
                debit_paise is not None
                and credit_paise is not None
                and ((debit_paise > 0) == (credit_paise > 0))
            ):
                row_errors.append(
                    "Exactly one of debit or credit must be greater than zero."
                )

            if row_errors:
                errors.extend(
                    BankStatementImportError(row_number=row_number, message=message)
                    for message in row_errors
                )
                continue

            assert parsed_date is not None
            assert debit_paise is not None and credit_paise is not None
            signed_amount = debit_paise if debit_paise > 0 else -credit_paise
            reference = fields["reference"].strip() or None
            transactions.append(
                BankStatementTransaction(
                    transaction_date=parsed_date,
                    description=description,
                    amount_paise=signed_amount,
                    external_reference=reference,
                )
            )
    except csv.Error:
        total_rows += 1
        errors.append(
            BankStatementImportError(
                row_number=reader.line_num or None,
                message="CSV contains a malformed quoted field or row.",
            )
        )

    if total_rows == 0 and not errors:
        errors.append(
            BankStatementImportError(
                row_number=2, message="CSV contains no transaction rows."
            )
        )

    return ParsedBankStatement(total_rows, transactions, errors)


def deduplicate_transactions(
    transactions: Iterable[BankStatementTransaction], existing_rows: Iterable[object] = ()
) -> tuple[list[BankStatementTransaction], int]:
    """Keep first occurrences and count matches in the batch or existing rows."""
    known = {
        (
            row.transaction_date,
            " ".join(row.description.split()).casefold(),
            row.amount_paise,
            (row.external_reference or "").strip().casefold(),
        )
        for row in existing_rows
    }
    accepted: list[BankStatementTransaction] = []
    duplicate_count = 0
    for transaction in transactions:
        if transaction.fingerprint in known:
            duplicate_count += 1
            continue
        known.add(transaction.fingerprint)
        accepted.append(transaction)
    return accepted, duplicate_count


def _advisory_lock_key(transaction: BankStatementTransaction) -> int:
    """Return a stable signed 64-bit key for a transaction fingerprint."""
    serializable = [
        transaction.transaction_date.isoformat(),
        transaction.fingerprint[1],
        transaction.amount_paise,
        transaction.fingerprint[3],
    ]
    digest = hashlib.blake2b(
        json.dumps(serializable, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def import_bank_statement(
    db: Session, contents: bytes, business_id: UUID
) -> BankStatementImportResponse:
    """Validate and atomically import a CSV, skipping duplicate fingerprints."""
    parsed = parse_bank_statement_csv(contents)
    if parsed.errors:
        return BankStatementImportResponse(
            total_rows=parsed.total_rows,
            inserted_rows=0,
            duplicate_rows=0,
            rejected_rows=parsed.total_rows,
            errors=parsed.errors,
        )

    unique_candidates, duplicate_rows = deduplicate_transactions(parsed.transactions)
    if not unique_candidates:
        return BankStatementImportResponse(
            total_rows=parsed.total_rows,
            inserted_rows=0,
            duplicate_rows=duplicate_rows,
            rejected_rows=0,
        )

    try:
        with db.begin():
            lock_keys = sorted({_advisory_lock_key(tx) for tx in unique_candidates})
            for lock_key in lock_keys:
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": lock_key},
                )

            dates = {transaction.transaction_date for transaction in unique_candidates}
            existing_rows = db.scalars(
                select(BankLine).where(
                    BankLine.business_id == business_id,
                    BankLine.transaction_date.in_(dates),
                )
            ).all()
            rows_to_insert, existing_duplicates = deduplicate_transactions(
                unique_candidates, existing_rows
            )
            duplicate_rows += existing_duplicates
            db.add_all(
                BankLine(
                    business_id=business_id,
                    transaction_date=transaction.transaction_date,
                    description=transaction.description,
                    amount_paise=transaction.amount_paise,
                    currency=transaction.currency,
                    external_reference=transaction.external_reference,
                )
                for transaction in rows_to_insert
            )
    except SQLAlchemyError as exc:
        db.rollback()
        raise BankStatementPersistenceError(parsed.total_rows, duplicate_rows) from exc

    return BankStatementImportResponse(
        total_rows=parsed.total_rows,
        inserted_rows=len(rows_to_insert),
        duplicate_rows=duplicate_rows,
        rejected_rows=0,
    )
