"""Organization-scoped document endpoints."""

import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from app.api.schemas.document import PilotDocumentQueueResponse, PilotDocumentResponse
from app.core.auth import PilotIdentity, get_pilot_admin, get_pilot_identity
from app.services.document_service import (
    DocumentNotFoundError,
    DocumentPublishConflictError,
    get_document,
    publish_document,
)
from app.services.ingestion_service import MAX_PDF_BYTES, create_ingestion_job
from app.services.ingestion_runner import run_background_ingestion


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/v1/documents",
    status_code=202,
    response_model=PilotDocumentQueueResponse,
    tags=["pilot"],
)
async def upload_pilot_document(
    request: Request,
    background_tasks: BackgroundTasks,
    family_key: Annotated[str, Query(min_length=1, max_length=100)],
    version: Annotated[str, Query(min_length=1, max_length=50)],
    title: Annotated[str, Query(min_length=1, max_length=200)],
    device_model_ids: Annotated[list[int], Query()],
    identity: Annotated[PilotIdentity, Depends(get_pilot_admin)],
) -> dict[str, int | str]:
    """Queue one PDF manual for the configured ingestion runner."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/pdf":
        raise HTTPException(status_code=415, detail="请使用application/pdf上传")
    if not device_model_ids:
        raise HTTPException(status_code=422, detail="至少选择一个设备型号")
    storage_dir = os.getenv("DOCUMENT_STORAGE_PATH")
    if not storage_dir:
        raise HTTPException(status_code=503, detail="文档存储目录尚未配置")
    ingestion_mode = os.getenv("PDF_INGESTION_MODE", "background").strip().lower()
    if ingestion_mode not in {"background", "worker"}:
        raise HTTPException(status_code=503, detail="PDF入库模式配置无效")

    pdf_data = bytearray()
    async for chunk in request.stream():
        if len(pdf_data) + len(chunk) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="PDF不能超过50MB")
        pdf_data.extend(chunk)

    try:
        result = await run_in_threadpool(
            create_ingestion_job,
            bytes(pdf_data),
            organization_id=identity.organization_id,
            device_model_ids=device_model_ids,
            family_key=family_key,
            version=version,
            title=title,
            storage_dir=storage_dir,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except OSError as error:
        logger.exception("PDF storage failed")
        raise HTTPException(status_code=503, detail="文档存储不可用") from error

    if ingestion_mode == "background":
        background_tasks.add_task(run_background_ingestion, result["job_id"], storage_dir)
    return result


@router.get(
    "/v1/documents/{document_id}",
    response_model=PilotDocumentResponse,
    tags=["pilot"],
)
def get_pilot_document(
    document_id: int,
    identity: Annotated[PilotIdentity, Depends(get_pilot_identity)],
) -> dict[str, Any]:
    try:
        return get_document(document_id, identity.organization_id)
    except DocumentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/v1/documents/{document_id}/publish",
    response_model=PilotDocumentResponse,
    tags=["pilot"],
)
def publish_pilot_document(
    document_id: int,
    identity: Annotated[PilotIdentity, Depends(get_pilot_admin)],
) -> dict[str, Any]:
    try:
        return publish_document(document_id, identity.organization_id)
    except DocumentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DocumentPublishConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
