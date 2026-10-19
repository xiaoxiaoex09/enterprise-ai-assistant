"""Document API response schemas."""

from typing import Literal

from pydantic import BaseModel


class PilotDocumentResponse(BaseModel):
    id: int
    family_key: str
    title: str
    version: str
    status: Literal["processing", "review", "published", "superseded", "failed"]
    document_type: Literal["manual", "case"]
    device_model_ids: list[int]
    chunks: int


class PilotDocumentQueueResponse(BaseModel):
    document_id: int
    job_id: int
    status: Literal["queued"]
