"""Validated receipt storage, extraction, and transactional persistence."""

from __future__ import annotations

import asyncio
import logging
import warnings
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image
from PIL import UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models import ChaseEvent, ClarificationReceipt, ClarificationRequest
from ..schemas.clarification_receipt import (
    ClarificationReceiptResponse,
    ReceiptExtraction,
    ReceiptExtractionResult,
)
from .invoice_service import ALLOWED_FORMATS, MAX_UPLOAD_SIZE
from .invoice_service import validate_image_upload
from .evidence_quota import EvidenceQuotaExceeded, enforce_business_quota
from .evidence_storage import EvidenceStorageError, build_evidence_key, get_storage
from .receipt_extraction_service import (
    ReceiptExtractionError,
    ReceiptExtractionNotConfiguredError,
    ReceiptProvider,
)

logger = logging.getLogger(__name__)
RECEIPT_DIRECTORY = Path(__file__).resolve().parents[3] / "data" / "receipts"
MEDIA_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}


class ClarificationReceiptNotFoundError(Exception):
    """Raised when no clarification request exists for the supplied ID."""


class ReceiptImageValidationError(Exception):
    """Raised for invalid images or filename/content type mismatches."""


class ReceiptUnsupportedFormatError(ReceiptImageValidationError):
    """Raised for unsupported extensions or extension/content mismatches."""


class ReceiptTooLargeError(Exception):
    """Raised when the image exceeds the 10 MB limit."""


class ReceiptPersistenceError(Exception):
    """Raised when receipt metadata and its audit event cannot be committed."""


async def upload_clarification_receipt(
    db: Session,
    business_id: UUID,
    request_id: UUID,
    original_filename: str,
    contents: bytes,
    provider: ReceiptProvider,
    declared_content_type: str | None = None,
) -> ClarificationReceiptResponse:
    """Store one receipt and append an extraction outcome to ChaseEvent history.

    The image remains evidence when Gemini is unavailable or returns an error.
    Database failures remove the newly written image; prior uploads are untouched.
    """
    if len(contents) > MAX_UPLOAD_SIZE:
        raise ReceiptTooLargeError("The receipt image exceeds the 10 MB upload limit.")
    from .invoice_service import InvoiceFormatError, InvoiceImageError
    try:
        safe_filename, extension, media_type = validate_image_upload(
            contents, original_filename, declared_content_type,
        )
    except InvoiceFormatError as exc:
        raise ReceiptUnsupportedFormatError(str(exc)) from exc
    except InvoiceImageError as exc:
        raise ReceiptImageValidationError(str(exc)) from exc

    # Confirm association before doing storage/network work.
    if db.scalar(select(ClarificationRequest.id).where(
        ClarificationRequest.id == request_id,
        ClarificationRequest.business_id == business_id,
    )) is None:
        db.rollback()
        raise ClarificationReceiptNotFoundError("Clarification request not found.")
    db.rollback()

    try:
        with db.begin():
            enforce_business_quota(db, business_id, len(contents), lock=False)
    except EvidenceQuotaExceeded:
        db.rollback()
        raise

    receipt_id = uuid4()
    stored_filename = f"{receipt_id}.{extension}"
    storage = get_storage()
    storage_key = build_evidence_key(business_id, "receipts", receipt_id, extension)
    try:
        await asyncio.to_thread(storage.put, storage_key, contents, media_type)
    except EvidenceStorageError as exc:
        await _remove_new_object(storage, storage_key)
        logger.error("Receipt object upload failed exception_type=%s", type(exc).__name__)
        raise ReceiptPersistenceError("Receipt image could not be stored.") from exc

    extraction: ReceiptExtraction | None = None
    extraction_status = "completed"
    extraction_message = "Receipt image stored and extraction completed."
    try:
        extraction = await provider.extract(contents, MEDIA_TYPES[extension])
    except ReceiptExtractionNotConfiguredError as exc:
        extraction_status = "not_configured"
        extraction_message = str(exc)
    except ReceiptExtractionError as exc:
        extraction_status = "failed"
        extraction_message = str(exc)
        logger.warning(
            "Receipt extraction failed clarification_id=%s exception_type=%s",
            request_id,
            type(exc).__name__,
        )
    except Exception as exc:
        extraction_status = "failed"
        extraction_message = "Receipt image was stored, but extraction failed."
        logger.error(
            "Receipt extraction failed clarification_id=%s exception_type=%s",
            request_id,
            type(exc).__name__,
        )

    try:
        with db.begin():
            enforce_business_quota(db, business_id, len(contents), lock=True)
            clarification = db.scalar(
                select(ClarificationRequest)
                .where(
                    ClarificationRequest.id == request_id,
                    ClarificationRequest.business_id == business_id,
                )
                .with_for_update()
            )
            if clarification is None:
                raise ClarificationReceiptNotFoundError(
                    "Clarification request not found."
                )

            receipt = ClarificationReceipt(
                id=receipt_id,
                business_id=business_id,
                clarification_request_id=request_id,
                original_filename=safe_filename,
                stored_filename=stored_filename,
                storage_provider=storage.provider,
                storage_key=storage_key,
                content_type=media_type,
                file_size=len(contents),
                extraction_status=extraction_status,
                merchant_name=extraction.merchant_name if extraction else None,
                document_type=extraction.document_type if extraction else None,
                document_number=extraction.document_number if extraction else None,
                document_date=extraction.document_date if extraction else None,
                total_amount_paise=extraction.total_amount_paise if extraction else None,
                payment_method=extraction.payment_method if extraction else None,
                payment_reference=extraction.payment_reference if extraction else None,
                confidence=(
                    extraction.confidence.model_dump(mode="json") if extraction else {}
                ),
                uncertain_fields=(
                    list(extraction.uncertain_fields)
                    if extraction
                    else [
                        "merchant_name", "document_type", "document_number",
                        "document_date", "total_amount_paise", "payment_method",
                        "payment_reference",
                    ]
                ),
                extraction_message=extraction_message,
            )
            db.add(receipt)
            clarification.status = "receipt_received"
            clarification.updated_at = datetime.now(timezone.utc)
            db.add(
                ChaseEvent(
                    id=uuid4(),
                    business_id=business_id,
                    bank_line_id=clarification.bank_line_id,
                    clarification_request_id=clarification.id,
                    event_type="clarification_receipt_uploaded",
                    details={
                        "receipt_id": str(receipt_id),
                        "stored_filename": stored_filename,
                        "storage_provider": storage.provider,
                        "extraction_status": extraction_status,
                    },
                )
            )
            db.flush()
    except (ClarificationReceiptNotFoundError, EvidenceQuotaExceeded):
        await _remove_new_object(storage, storage_key)
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        await _remove_new_object(storage, storage_key)
        logger.error(
            "Clarification receipt persistence failed clarification_id=%s exception_type=%s",
            request_id,
            type(exc).__name__,
        )
        raise ReceiptPersistenceError(
            "Receipt metadata could not be saved."
        ) from exc
    except Exception as exc:
        db.rollback()
        await _remove_new_object(storage, storage_key)
        logger.error(
            "Clarification receipt transaction failed clarification_id=%s exception_type=%s",
            request_id,
            type(exc).__name__,
        )
        raise ReceiptPersistenceError(
            "Receipt metadata could not be saved."
        ) from exc

    if extraction is None:
        extraction_result = ReceiptExtractionResult(
            status=extraction_status, message=extraction_message, extraction=None
        )
    else:
        extraction_result = ReceiptExtractionResult(
            status="completed",
            message=extraction_message,
            extraction=extraction,
        )
    return ClarificationReceiptResponse(
        receipt_id=str(receipt_id),
        clarification_id=str(request_id),
        stored_filename=stored_filename,
        original_filename=safe_filename,
        content_type=media_type,
        file_size=len(contents),
        status="receipt_received",
        extraction=extraction_result,
        uploaded_at=receipt.uploaded_at or datetime.now(timezone.utc),
    )


def _validate_image(contents: bytes, extension: str) -> str:
    expected_format = ALLOWED_FORMATS[extension]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(contents)) as image:
                detected_format = image.format
                image.verify()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ReceiptImageValidationError(
            "The uploaded file is not a valid image."
        ) from exc
    if detected_format != expected_format:
        raise ReceiptUnsupportedFormatError(
            "The image content does not match its filename extension."
        )
    return detected_format


async def _remove_new_object(storage, key: str) -> None:
    try:
        await asyncio.to_thread(storage.delete, key)
    except EvidenceStorageError as exc:
        logger.error(
            "Receipt rollback could not remove its new object exception_type=%s",
            type(exc).__name__,
        )
