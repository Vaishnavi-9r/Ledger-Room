"""Response schemas for invoice endpoints."""

from pydantic import BaseModel, ConfigDict


class InvoiceUploadResponse(BaseModel):
    invoice_id: str
    original_filename: str
    stored_filename: str
    file_size: int
    upload_status: str


class InvoiceListItem(BaseModel):
    invoice_id: str
    stored_filename: str
    file_size: int
    upload_status: str


class InvoiceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoices: list[InvoiceListItem]
