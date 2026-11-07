"""Application entry point for the LedgerRoom API."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api.bank_statements import router as bank_statements_router
from .api.auth import router as auth_router
from .api.accountants import accountant_router, business_router as accountant_business_router
from .api.businesses import router as businesses_router
from .api.clarifications import router as clarifications_router
from .api.health import router as health_router
from .api.invoices import router as invoices_router
from .api.financial_records import router as financial_records_router
from .api.reconciliation import router as reconciliation_router
from .api.conversation import router as conversation_router
from .api.reports import router as reports_router
from .api.mattermost import router as mattermost_router


class WelcomeResponse(BaseModel):
    message: str
    project_name: str


class HealthResponse(BaseModel):
    status: str


class InfoResponse(BaseModel):
    project_name: str
    version: str
    description: str


class ErrorResponse(BaseModel):
    detail: Any


PROJECT_NAME = "LedgerRoom"
API_VERSION = "0.1.0"
API_DESCRIPTION = "An API for the LedgerRoom project."

app = FastAPI(
    title="LedgerRoom API",
    version=API_VERSION,
    description=API_DESCRIPTION,
)

app.include_router(invoices_router)
app.include_router(auth_router)
app.include_router(businesses_router)
app.include_router(accountant_business_router)
app.include_router(accountant_router)
app.include_router(bank_statements_router)
app.include_router(clarifications_router)
app.include_router(reconciliation_router)
app.include_router(conversation_router)
app.include_router(reports_router)
app.include_router(mattermost_router)
app.include_router(financial_records_router)
app.include_router(health_router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Return HTTP errors in a consistent JSON structure."""
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(detail=exc.detail).model_dump(),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return request validation errors in the API's standard structure."""
    return JSONResponse(
        status_code=422,
        # Pydantic's raw errors may contain the submitted input (including
        # passwords and tokens). Keep only structural validation details.
        content=ErrorResponse(detail=[
            {key: error[key] for key in ("loc", "msg", "type") if key in error}
            for error in exc.errors()
        ]).model_dump(),
    )


@app.get("/", response_model=WelcomeResponse, tags=["General"])
async def read_root() -> WelcomeResponse:
    """Return a short welcome message for the project."""
    return WelcomeResponse(
        message="Welcome to the LedgerRoom API.",
        project_name=PROJECT_NAME,
    )


@app.get("/health", response_model=HealthResponse, tags=["General"])
async def read_health() -> HealthResponse:
    """Report that the API is running."""
    return HealthResponse(status="healthy")


@app.get("/api/v1/info", response_model=InfoResponse, tags=["General"])
async def read_info() -> InfoResponse:
    """Return basic project and API information."""
    return InfoResponse(
        project_name=PROJECT_NAME,
        version=API_VERSION,
        description=API_DESCRIPTION,
    )
