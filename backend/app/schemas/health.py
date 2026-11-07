"""Health-check response schemas."""

from typing import Literal

from pydantic import BaseModel


class DatabaseHealthResponse(BaseModel):
    status: Literal["healthy"]
    database: Literal["connected"]
