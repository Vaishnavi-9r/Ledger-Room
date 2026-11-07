"""Owner-facing clarification request endpoints."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from ..db.session import get_db
from ..dependencies.business import BusinessContext, get_business_context, require_owner
from ..models import ClarificationReceipt, ClarificationRequest
from ..schemas.clarification import (
    ClarificationOwnerResponseRequest,
    ClarificationRequestListResponse,
    ClarificationRequestResponse,
    ClarificationStatus,
)
from ..schemas.clarification_receipt import ClarificationReceiptResponse
from ..schemas.receipt_verification import ReceiptVerificationResponse
from ..services.clarification_service import (
    ClarificationNotFoundError,
    ClarificationStateConflictError,
    clarification_response,
    respond_to_clarification,
)
from ..services.clarification_receipt_service import (
    ClarificationReceiptNotFoundError,
    ReceiptImageValidationError,
    ReceiptPersistenceError,
    ReceiptTooLargeError,
    ReceiptUnsupportedFormatError,
    upload_clarification_receipt,
)
from ..services.invoice_service import MAX_UPLOAD_SIZE
from ..services.receipt_extraction_service import (
    ReceiptProvider,
    get_receipt_extraction_provider,
)
from ..services.receipt_verification_service import (
    ReceiptVerificationNotFoundError,
    ReceiptVerificationPersistenceError,
    ReceiptVerificationUnavailableError,
    get_receipt_verification,
    verify_clarification_receipt,
)
from ..services.evidence_quota import EvidenceQuotaExceeded
from ..services.evidence_storage import (
    EvidenceObjectNotFoundError,
    EvidenceStorageError,
    get_storage,
)

router = APIRouter(prefix="/api/v1/clarifications", tags=["Clarifications"])
logger = logging.getLogger(__name__)


def get_receipt_provider() -> ReceiptProvider:
    """Provide the configured receipt-specific Gemini adapter."""
    return get_receipt_extraction_provider()


def _parse_request_id(request_id: str) -> UUID:
    try:
        return UUID(request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Clarification request not found.",
        ) from exc


@router.get("/", response_model=ClarificationRequestListResponse)
def list_clarifications(
    request_status: ClarificationStatus | None = Query(default=None, alias="status"),
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> ClarificationRequestListResponse:
    """List clarification requests, optionally filtered by lifecycle status."""
    statement = (
        select(ClarificationRequest)
        .where(ClarificationRequest.business_id == context.business_id)
        .options(selectinload(ClarificationRequest.bank_line))
        .order_by(ClarificationRequest.created_at.desc())
    )
    if request_status is not None:
        statement = statement.where(ClarificationRequest.status == request_status)
    rows = db.scalars(statement).all()
    return ClarificationRequestListResponse(
        clarifications=[clarification_response(row) for row in rows]
    )


@router.get("/{request_id}", response_model=ClarificationRequestResponse)
def get_clarification(
    request_id: str, context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db)
) -> ClarificationRequestResponse:
    """Return one clarification request and its linked bank transaction."""
    parsed_id = _parse_request_id(request_id)
    try:
        request = db.scalar(
            select(ClarificationRequest)
            .options(selectinload(ClarificationRequest.bank_line))
            .where(ClarificationRequest.id == parsed_id,
                   ClarificationRequest.business_id == context.business_id)
        )
    except SQLAlchemyError as exc:
        logger.error(
            "Clarification lookup failed exception_type=%s", type(exc).__name__
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Clarification request could not be loaded.",
        ) from exc
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Clarification request not found.",
        )
    return clarification_response(request)


@router.post(
    "/{request_id}/respond",
    response_model=ClarificationRequestResponse,
    responses={
        404: {"description": "Clarification request not found."},
        409: {"description": "Clarification request is no longer pending."},
    },
)
def respond(
    request_id: str,
    body: ClarificationOwnerResponseRequest,
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> ClarificationRequestResponse:
    """Record an owner response without changing invoice or match state."""
    require_owner(context)
    parsed_id = _parse_request_id(request_id)
    try:
        request = respond_to_clarification(db, context.business_id, parsed_id, body.owner_response)
    except ClarificationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Clarification request not found.",
        ) from exc
    except ClarificationStateConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        logger.error(
            "Clarification response persistence failed exception_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Owner response could not be saved.",
        ) from exc
    return clarification_response(request)


@router.post(
    "/{request_id}/receipt",
    response_model=ClarificationReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Clarification request not found."},
        413: {"description": "Image exceeds the 10 MB upload limit."},
        415: {"description": "Unsupported or mismatched image format."},
        503: {"description": "Receipt metadata could not be saved."},
    },
)
async def upload_receipt(
    request_id: str,
    file: UploadFile = File(...),
    context: BusinessContext = Depends(get_business_context),
    provider: ReceiptProvider = Depends(get_receipt_provider),
    db: Session = Depends(get_db),
) -> ClarificationReceiptResponse:
    """Attach a validated receipt image and vision result to one clarification."""
    require_owner(context)
    parsed_id = _parse_request_id(request_id)
    filename = file.filename or ""
    try:
        contents = await file.read(MAX_UPLOAD_SIZE + 1)
        result = await upload_clarification_receipt(
            db, context.business_id, parsed_id, filename, contents, provider,
            file.content_type,
        )
        return result
    except EvidenceQuotaExceeded:
        raise HTTPException(status_code=507, detail="Business evidence storage quota exceeded.") from None
    except ReceiptTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except ReceiptUnsupportedFormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    except ReceiptImageValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ClarificationReceiptNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Clarification request not found.",
        ) from exc
    except ReceiptPersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Clarification receipt processing failed exception_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Receipt metadata could not be saved.",
        ) from exc
    finally:
        await file.close()


@router.post(
    "/{request_id}/receipts/{receipt_id}/verify",
    response_model=ReceiptVerificationResponse,
    responses={
        404: {"description": "Receipt was not found for this clarification."},
        503: {"description": "Verification could not be saved."},
    },
)
def verify_receipt(
    request_id: str,
    receipt_id: str,
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> ReceiptVerificationResponse:
    """Run deterministic checks without changing invoice or match state."""
    require_owner(context)
    parsed_request_id = _parse_request_id(request_id)
    try:
        parsed_receipt_id = UUID(receipt_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Receipt not found for this clarification.",
        ) from exc
    try:
        return verify_clarification_receipt(
            db, context.business_id, parsed_request_id, parsed_receipt_id
        )
    except ReceiptVerificationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ReceiptVerificationUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ReceiptVerificationPersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get(
    "/{request_id}/receipts/{receipt_id}/verification",
    response_model=ReceiptVerificationResponse,
    responses={
        404: {"description": "Receipt was not found for this clarification."},
        409: {"description": "The receipt has not been verified yet."},
    },
)
def get_receipt_verification_result(
    request_id: str,
    receipt_id: str,
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> ReceiptVerificationResponse:
    """Retrieve the latest persisted verification for a clarification receipt."""
    parsed_request_id = _parse_request_id(request_id)
    try:
        parsed_receipt_id = UUID(receipt_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Receipt not found for this clarification.",
        ) from exc
    try:
        return get_receipt_verification(db, context.business_id, parsed_request_id, parsed_receipt_id)
    except ReceiptVerificationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ReceiptVerificationUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.get("/{request_id}/receipts/{receipt_id}/image")
def download_receipt_image(
    request_id: str,
    receipt_id: str,
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
):
    """Serve receipt evidence only after checking both business and request IDs."""
    parsed_request_id = _parse_request_id(request_id)
    try:
        parsed_receipt_id = UUID(receipt_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="Receipt not found.") from None
    receipt = db.scalar(select(ClarificationReceipt).where(
        ClarificationReceipt.id == parsed_receipt_id,
        ClarificationReceipt.clarification_request_id == parsed_request_id,
        ClarificationReceipt.business_id == context.business_id,
    ))
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    try:
        source = get_storage(receipt.storage_provider).open(receipt.storage_key)
    except EvidenceObjectNotFoundError:
        raise HTTPException(status_code=404, detail="Receipt image not found.")
    except EvidenceStorageError:
        logger.error("Receipt download storage failure provider=%s", receipt.storage_provider)
        raise HTTPException(status_code=503, detail="Receipt image is unavailable.") from None
    return StreamingResponse(
        iter(lambda: source.read(64 * 1024), b""),
        media_type=receipt.content_type,
        headers={"Content-Disposition": f'attachment; filename="{receipt.stored_filename}"'},
        background=BackgroundTask(source.close),
    )
