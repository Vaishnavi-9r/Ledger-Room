"""Service health endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..schemas.health import DatabaseHealthResponse

router = APIRouter(prefix="/api/v1/health", tags=["Health"])
logger = logging.getLogger(__name__)


@router.get("/database", response_model=DatabaseHealthResponse)
def check_database(db: Session = Depends(get_db)) -> DatabaseHealthResponse:
    """Run a read-only connectivity probe without exposing connection details."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.error(
            "Database health check failed exception_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable.",
        ) from exc

    return DatabaseHealthResponse(status="healthy", database="connected")
