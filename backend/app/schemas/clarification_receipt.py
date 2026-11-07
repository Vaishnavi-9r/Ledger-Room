"""Schemas for receipt evidence and vision extraction results."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

ReceiptField = Literal[
    "merchant_name",
    "document_type",
    "document_number",
    "document_date",
    "total_amount_paise",
    "payment_method",
    "payment_reference",
]
DocumentType = Literal["invoice", "payment_receipt", "cash_receipt", "unknown"]
ConfidenceScore = Annotated[float, Field(ge=0.0, le=1.0)]


class ReceiptFieldConfidence(BaseModel):
    merchant_name: ConfidenceScore | None = None
    document_type: ConfidenceScore | None = None
    document_number: ConfidenceScore | None = None
    document_date: ConfidenceScore | None = None
    total_amount_paise: ConfidenceScore | None = None
    payment_method: ConfidenceScore | None = None
    payment_reference: ConfidenceScore | None = None


class ReceiptExtraction(BaseModel):
    merchant_name: str | None = None
    document_type: DocumentType | None = None
    document_number: str | None = None
    document_date: date | None = None
    total_amount_paise: Annotated[int, Field(strict=True, ge=0)] | None = None
    payment_method: str | None = None
    payment_reference: str | None = None
    confidence: ReceiptFieldConfidence = Field(default_factory=ReceiptFieldConfidence)
    uncertain_fields: list[ReceiptField] = Field(default_factory=list)

    @model_validator(mode="after")
    def mark_missing_or_uncertain_fields(self) -> "ReceiptExtraction":
        uncertain = list(self.uncertain_fields)
        for field_name in ReceiptField.__args__:
            if getattr(self, field_name) is None or getattr(self.confidence, field_name) is None:
                if field_name not in uncertain:
                    uncertain.append(field_name)
        self.uncertain_fields = uncertain
        return self


class ReceiptExtractionResult(BaseModel):
    status: Literal["completed", "failed", "not_configured"]
    message: str
    extraction: ReceiptExtraction | None = None


class ClarificationReceiptResponse(BaseModel):
    receipt_id: str
    clarification_id: str
    stored_filename: str
    original_filename: str | None
    content_type: str
    file_size: int
    status: str
    extraction: ReceiptExtractionResult
    uploaded_at: datetime
