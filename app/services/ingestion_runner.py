"""Run a queued ingestion job from FastAPI's background-task scheduler."""

import logging

from pdf_rag_service import process_ingestion_job


logger = logging.getLogger(__name__)


def run_background_ingestion(job_id: int, storage_dir: str) -> None:
    """Preserve the legacy background task's failure logging behavior."""
    try:
        process_ingestion_job(job_id, storage_dir=storage_dir)
    except Exception:
        logger.exception("Background ingestion failed for job %s", job_id)
