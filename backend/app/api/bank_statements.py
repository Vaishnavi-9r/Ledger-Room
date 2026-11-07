"""Bank statement CSV upload endpoint."""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..dependencies.business import BusinessContext, get_business_context, require_owner
from ..models import BankLine
from ..schemas.bank_statement import BankLineItem, BankStatementImportResponse
from ..services.bank_statement_service import (
    BankStatementPersistenceError,
    import_bank_statement,
)

router = APIRouter(prefix="/api/v1/bank-statements", tags=["Bank Statements"])
logger = logging.getLogger(__name__)
MAX_CSV_SIZE = 10 * 1024 * 1024


@router.post(
    "/upload",
    response_model=BankStatementImportResponse,
    responses={
        413: {"description": "CSV exceeds the 10 MB upload limit."},
        415: {"description": "Upload a CSV file."},
        422: {"description": "CSV rows failed validation; no rows were imported."},
        503: {"description": "Database persistence failed; no rows were imported."},
    },
)
async def upload_bank_statement(
    file: UploadFile = File(...),
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> BankStatementImportResponse | JSONResponse:
    """Validate and transactionally import a CSV bank statement."""
    require_owner(context)
    filename = file.filename or ""
    if not filename.casefold().endswith(".csv"):
        await file.close()
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload a .csv file.",
        )

    try:
        contents = await file.read(MAX_CSV_SIZE + 1)
    finally:
        await file.close()

    if len(contents) > MAX_CSV_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="The CSV exceeds the 10 MB upload limit.",
        )

    try:
        result = import_bank_statement(db, contents, context.business_id)
    except BankStatementPersistenceError as exc:
        logger.error(
            "Bank statement persistence failed exception_type=%s",
            type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        )
        response = BankStatementImportResponse(
            total_rows=exc.total_rows,
            inserted_rows=0,
            duplicate_rows=exc.duplicate_rows,
            rejected_rows=max(exc.total_rows - exc.duplicate_rows, 0),
            errors=[
                {
                    "row_number": None,
                    "message": "Database persistence failed; no rows were imported.",
                }
            ],
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response.model_dump(mode="json"),
        )

    if result.errors:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=result.model_dump(mode="json"),
        )
    return result


@router.get("/", response_model=list[BankLineItem])
def list_bank_lines(
    context: BusinessContext = Depends(get_business_context), db: Session = Depends(get_db),
):
    rows = db.scalars(select(BankLine).where(
        BankLine.business_id == context.business_id,
    ).order_by(BankLine.transaction_date.desc(), BankLine.created_at.desc())).all()
    return [
        {
            "bank_line_id": row.id,
            "transaction_date": row.transaction_date,
            "description": row.description,
            "amount_paise": row.amount_paise,
            "currency": row.currency,
            "reference": row.external_reference,
            "created_at": row.created_at,
        }
        for row in rows
    ]
