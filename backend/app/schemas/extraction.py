"""Structured schema for invoice extraction results."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

ExtractionField = Literal[
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "subtotal_paise",
    "tax_amount_paise",
    "total_amount_paise",
    "currency",
]

ConfidenceScore = Annotated[float, Field(ge=0.0, le=1.0)]
PaiseAmount = Annotated[int, Field(strict=True, ge=0)]


class FieldConfidence(BaseModel):
    """Confidence score for each extracted value, when a value is available."""

    vendor_name: ConfidenceScore | None = None
    invoice_number: ConfidenceScore | None = None
    invoice_date: ConfidenceScore | None = None
    due_date: ConfidenceScore | None = None
    subtotal_paise: ConfidenceScore | None = None
    tax_amount_paise: ConfidenceScore | None = None
    total_amount_paise: ConfidenceScore | None = None
    currency: ConfidenceScore | None = None


class InvoiceExtraction(BaseModel):
    """Normalized invoice values returned by a configured extraction provider."""

    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    subtotal_paise: PaiseAmount | None = None
    tax_amount_paise: PaiseAmount | None = None
    total_amount_paise: PaiseAmount | None = None
    currency: str | None = None
    confidence: FieldConfidence = Field(default_factory=FieldConfidence)
    uncertain_fields: list[ExtractionField] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_confidence_coverage(self) -> "InvoiceExtraction":
        """Mark null values and values without confidence estimates for review."""
        field_names = (
            "vendor_name",
            "invoice_number",
            "invoice_date",
            "due_date",
            "subtotal_paise",
            "tax_amount_paise",
            "total_amount_paise",
            "currency",
        )
        score_updates: dict[str, None] = {}
        uncertain_fields = list(self.uncertain_fields)

        for field_name in field_names:
            value = getattr(self, field_name)
            score = getattr(self.confidence, field_name)
            if value is None:
                if score is not None:
                    score_updates[field_name] = None
                if field_name not in uncertain_fields:
                    uncertain_fields.append(field_name)
            elif score is None and field_name not in uncertain_fields:
                uncertain_fields.append(field_name)

        if score_updates:
            self.confidence = self.confidence.model_copy(update=score_updates)
        self.uncertain_fields = uncertain_fields
        return self


class ExtractionResponse(BaseModel):
    """Response envelope for the invoice extraction endpoint."""

    invoice_id: str
    status: Literal["not_configured", "completed"]
    message: str
    extraction: InvoiceExtraction | None = None
    validation_warnings: list[str] = Field(default_factory=list)
