"""Basic liveness endpoint."""

from fastapi import APIRouter


router = APIRouter()


@router.get("/health", response_model=None)
def health_check() -> dict[str, str]:
    return {"status": "healthy"}
