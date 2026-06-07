"""Liveness endpoint."""

from __future__ import annotations

from importlib.metadata import version

from fastapi import APIRouter

from phare.api.schemas import HealthResponse
from phare.core.config import get_settings

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Process liveness. Does not touch external dependencies."""
    return HealthResponse(
        status="ok",
        service=get_settings().service_name,
        version=version("phare"),
    )
