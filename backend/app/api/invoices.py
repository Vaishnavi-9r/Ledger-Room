"""Invoice upload and listing endpoints."""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..dependencies.business import BusinessContext, get_business_context, require_owner
from ..models import Invoice
from ..schemas.extraction import ExtractionResponse
from ..schemas.invoice import (
    InvoiceListResponse,
    InvoiceUploadResponse,
)
from ..services.extraction_service import (
    ExtractionAuthenticationError,
    ExtractionModelUnavailableError,
    ExtractionNotConfiguredError,
    ExtractionProviderError,
    ExtractionProviderUnavailableError,
    ExtractionProvider,
    ExtractionRateLimitError,
    ExtractionTimeoutError,
    InvalidExtractionResponseError,
    extract_invoice as run_extraction,
    get_extraction_provider as create_extraction_provider,
    validate_extraction_totals,
)
from ..services.invoice_service import (
    MAX_UPLOAD_SIZE,
    InvoiceImageError,
    InvoiceFormatError,
    InvoiceNotFoundError,
    store_invoice,
    read_invoice_evidence,
    remove_evidence,
    validate_image_upload,
)
from ..services.evidence_quota import (
    EvidenceBusinessUnavailable,
    EvidenceQuotaExceeded,
    enforce_business_quota,
)
from ..services.evidence_storage import (
    EvidenceObjectNotFoundError,
    EvidenceStorageConfigurationError,
    EvidenceStorageError,
    get_storage,
)

router = APIRouter(prefix="/api/v1/invoices", tags=["Invoices"])
logger = logging.getLogger(__name__)


def get_extraction_provider() -> ExtractionProvider:
    """Provide the configured extraction implementation to the route."""
    return create_extraction_provider()


@router.post(
    "/upload",
    response_model=InvoiceUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_invoice(
    file: UploadFile = File(...),
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> InvoiceUploadResponse:
    """Validate and store one JPG, JPEG, or PNG invoice image."""
    require_owner(context)
    try:
        filename = file.filename or ""
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension not in {"jpg", "jpeg", "png"}:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Unsupported file extension. Upload a JPG, JPEG, or PNG image.",
            )

        contents = await file.read(MAX_UPLOAD_SIZE + 1)
        if len(contents) > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="The invoice image exceeds the 10 MB upload limit.",
            )
        validate_image_upload(contents, filename, file.content_type)

        # Storage is written only after validation and quota authorization.
        result = None
    except InvoiceFormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    except InvoiceImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    finally:
        await file.close()

    try:
        with db.begin():
            enforce_business_quota(db, context.business_id, len(contents))
            result = await run_in_threadpool(
                store_invoice, contents, filename, extension, context.business_id,
                file.content_type,
            )
            invoice = Invoice(
                id=UUID(str(result["invoice_id"])),
                business_id=context.business_id,
                original_filename=str(result["original_filename"]),
                stored_filename=str(result["stored_filename"]),
                storage_provider=str(result["storage_provider"]),
                storage_key=str(result["storage_key"]),
                content_type=str(result["content_type"]),
                file_size=int(result["file_size"]),
                upload_status="uploaded",
                extraction_status="pending",
            )
            db.add(invoice)
            db.flush()
    except EvidenceQuotaExceeded:
        raise HTTPException(status_code=507, detail="Business evidence storage quota exceeded.") from None
    except EvidenceBusinessUnavailable:
        raise HTTPException(status_code=404, detail="Business not found.") from None
    except (EvidenceStorageConfigurationError, EvidenceStorageError) as exc:
        if result is not None:
            try:
                await run_in_threadpool(remove_evidence, str(result["storage_provider"]), str(result["storage_key"]))
            except EvidenceStorageError as cleanup_error:
                logger.error("Invoice object rollback failed exception_type=%s", type(cleanup_error).__name__)
        logger.error("Invoice object storage failed exception_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Invoice image could not be stored.") from None
    except SQLAlchemyError as exc:
        db.rollback()
        if result is not None:
            try:
                await run_in_threadpool(remove_evidence, str(result["storage_provider"]), str(result["storage_key"]))
            except EvidenceStorageError as cleanup_error:
                logger.error("Invoice upload rollback left an orphan exception_type=%s", type(cleanup_error).__name__)
        logger.error(
            "Invoice metadata insert failed exception_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Invoice metadata could not be saved to the database.",
        ) from exc
    except Exception:
        db.rollback()
        if result is not None:
            try:
                await run_in_threadpool(remove_evidence, str(result["storage_provider"]), str(result["storage_key"]))
            except EvidenceStorageError as cleanup_error:
                logger.error("Invoice upload rollback left an orphan exception_type=%s", type(cleanup_error).__name__)
        raise

    return InvoiceUploadResponse(**result)


@router.get("/", response_model=InvoiceListResponse)
async def get_invoices(
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> InvoiceListResponse:
    """List only invoice records belonging to the selected business."""
    rows = db.scalars(select(Invoice).where(Invoice.business_id == context.business_id).order_by(Invoice.uploaded_at.desc())).all()
    return InvoiceListResponse(invoices=[
        {
            "invoice_id": str(row.id),
            "stored_filename": row.stored_filename,
            "file_size": row.file_size,
            "upload_status": row.upload_status,
        }
        for row in rows
    ])


@router.get("/{invoice_id}/image")
def download_invoice_image(
    invoice_id: str,
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
):
    """Download invoice evidence only after a tenant-scoped metadata lookup."""
    try:
        parsed_id = UUID(invoice_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="Invoice not found.") from None
    invoice = db.scalar(select(Invoice).where(
        Invoice.id == parsed_id, Invoice.business_id == context.business_id,
    ))
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    try:
        storage = get_storage(invoice.storage_provider)
        source = storage.open(invoice.storage_key)
    except EvidenceObjectNotFoundError:
        raise HTTPException(status_code=404, detail="Invoice image not found.") from None
    except EvidenceStorageError:
        logger.error("Invoice download storage failure provider=%s", invoice.storage_provider)
        raise HTTPException(status_code=503, detail="Invoice image is unavailable.") from None
    return StreamingResponse(
        iter(lambda: source.read(64 * 1024), b""),
        media_type=invoice.content_type,
        headers={"Content-Disposition": f'attachment; filename="{invoice.stored_filename}"'},
        background=BackgroundTask(source.close),
    )


@router.post(
    "/{invoice_id}/extract",
    response_model=ExtractionResponse,
    responses={
        404: {"description": "The invoice ID does not identify a stored invoice."},
        429: {"description": "Gemini rate or quota limit was reached."},
        502: {"description": "Gemini returned an invalid response or provider error."},
        503: {"description": "Gemini credentials, model, or service are unavailable."},
        504: {"description": "Gemini did not respond before the timeout."},
    },
)
async def extract_uploaded_invoice(
    invoice_id: str,
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
    provider: ExtractionProvider = Depends(get_extraction_provider),
) -> ExtractionResponse | JSONResponse:
    """Attempt extraction for an uploaded invoice using the configured provider."""
    require_owner(context)
    try:
        parsed_id = UUID(invoice_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="Invoice not found.") from None
    invoice = db.scalar(select(Invoice).where(
        Invoice.id == parsed_id, Invoice.business_id == context.business_id,
    ))
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    try:
        image_bytes = read_invoice_evidence(invoice.storage_provider, invoice.storage_key)
        media_type = invoice.content_type
        extraction = await provider.extract(image_bytes, media_type)
    except (InvoiceNotFoundError, EvidenceObjectNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except EvidenceStorageError as exc:
        logger.error("Invoice extraction storage failure provider=%s exception_type=%s", invoice.storage_provider, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Invoice image is unavailable.") from None
    except ExtractionNotConfiguredError as exc:
        body = ExtractionResponse(
            invoice_id=invoice_id,
            status="not_configured",
            message=str(exc),
            extraction=None,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body.model_dump(mode="json"),
        )
    except (ExtractionAuthenticationError, ExtractionModelUnavailableError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ExtractionProviderUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ExtractionRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except ExtractionTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except InvalidExtractionResponseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except ExtractionProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    validation_warnings = validate_extraction_totals(extraction)

    try:
        invoice.vendor_name = extraction.vendor_name
        invoice.invoice_number = extraction.invoice_number
        invoice.invoice_date = extraction.invoice_date
        invoice.due_date = extraction.due_date
        invoice.subtotal_paise = extraction.subtotal_paise
        invoice.tax_amount_paise = extraction.tax_amount_paise
        invoice.total_amount_paise = extraction.total_amount_paise
        invoice.currency = extraction.currency
        invoice.confidence = extraction.confidence.model_dump(mode="json")
        invoice.uncertain_fields = list(extraction.uncertain_fields)
        invoice.validation_warnings = validation_warnings
        invoice.extraction_status = "completed"
        invoice.extracted_at = datetime.now(timezone.utc)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Invoice extraction persistence failed exception_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Extraction completed, but invoice metadata could not be saved.",
        ) from exc

    return ExtractionResponse(
        invoice_id=invoice_id,
        status="completed",
        message=(
            "Invoice extraction completed. Confidence values are model estimates, "
            "not calibrated certainty."
        ),
        extraction=extraction,
        validation_warnings=validation_warnings,
    )
