"""Request result schemas for bank statement CSV imports."""

from pydantic import BaseModel, ConfigDict, Field
from datetime import date, datetime
from uuid import UUID


class BankStatementImportError(BaseModel):
    """A row-specific or file-level bank statement import error."""

    row_number: int | None = None
    message: str


class BankStatementImportResponse(BaseModel):
    """Summary of a bank statement import attempt."""

    model_config = ConfigDict(extra="forbid")

    total_rows: int = Field(ge=0)
    inserted_rows: int = Field(ge=0)
    duplicate_rows: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    errors: list[BankStatementImportError] = Field(default_factory=list)


class BankLineItem(BaseModel):
    bank_line_id: UUID
    transaction_date: date
    description: str
    amount_paise: int
    currency: str | None
    reference: str | None
    created_at: datetime
