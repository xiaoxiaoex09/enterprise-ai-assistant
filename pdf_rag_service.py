
import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from threading import Event, Thread
from typing import Sequence
from uuid import uuid4

from app.services.ingestion_job_service import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    ClaimedJob,
    IngestionChunk,
    JobLeaseLostError,
    claim_job,
    complete_job,
    fail_job,
    inspect_completion,
    recover_expired_jobs,
    renew_lease,
)
from app.services.ingestion_service import MAX_PDF_BYTES, create_ingestion_job
from database import get_connection, init_db
from embedding_service import (
    EMBEDDING_MODEL,
    get_embedding,
    cosine_similarity
)
from pdf_service import extract_pdf_text
from recover_legacy_ingestion_jobs import inspect_legacy_jobs


logger = logging.getLogger(__name__)
MAX_PAGES = 1000
MAX_VERSIONED_CHUNKS = 2000
DB_RETRY_INITIAL_SECONDS = 1.0
DB_RETRY_MAX_SECONDS = 30.0
DB_RETRY_MAX_FAILURES = 5


class WorkerDatabaseRetriesExhausted(RuntimeError):
    """The worker needs external supervision after its database retry budget."""


RETRYABLE_SQLITE_CODES = frozenset({
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
    sqlite3.SQLITE_IOERR,
})


def _is_retryable_database_error(error: sqlite3.Error) -> bool:
    """Classify bounded retries; opening a database is a configuration error."""
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in RETRYABLE_SQLITE_CODES
    return str(error).lower() in {
        "database is locked", "database is busy",
        "disk i/o error",
    }


def _database_retry_delay(failure_count: int) -> float:
    return min(DB_RETRY_MAX_SECONDS, DB_RETRY_INITIAL_SECONDS * 2 ** (failure_count - 1))


# 将长文本切成有重叠的小段
def split_text(text, size=500, overlap=80):
    text = " ".join(text.split())

    if not text:
        return []

    chunks = []
    start = 0

    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])

        if end == len(text):
            break

        start = end - overlap

    return chunks
# 解析并保存 PDF
def ingest_pdf(
    pdf_bytes,
    filename,
    doc_type="unknown"
):

    pages = extract_pdf_text(pdf_bytes)


    if len(pages) > 1000:
        raise ValueError(
            "PDF不能超过1000页"
        )


    records = []


    for page in pages:

        page_number = page["page"]

        text = page["text"]


        for chunk in split_text(text):

            records.append({
                "source": filename,
                "page": page_number,
                "content": chunk
            })


    if not records:

        raise ValueError(
            "未提取到文字，可能是扫描版PDF"
        )


    if len(records) > 100000:

        raise ValueError(
            "文本片段超过100000段"
        )


    # 生成向量

    for record in records:

        record["embedding"] = get_embedding(
            record["content"]
        )


    # 保存数据库

    with get_connection() as conn:

        conn.execute(
            "DELETE FROM knowledge_chunks WHERE source=?",
            (filename,)
        )


        for record in records:
            conn.execute(
                """
                INSERT INTO knowledge_chunks
                    (source, page, content, embedding, model, doc_type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record["source"],
                    record["page"],
                    record["content"],
                    json.dumps(record["embedding"]),
                    EMBEDDING_MODEL,
                    doc_type
                )
            )


        conn.commit()

    return {
        "filename": filename,
        "doc_type": doc_type,
        "pages": len(pages),
        "chunks": len(records)
    }

# 语义检索
def search_pdf_knowledge(question, top_k=3):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT source, page, content, embedding
            FROM knowledge_chunks
            WHERE model = ?
            """,
            (EMBEDDING_MODEL,)
        ).fetchall()

    if not rows:
        return []

    question_vector = get_embedding(question)

    results = []

    for row in rows:
        vector = json.loads(row["embedding"])

        score = cosine_similarity(
            question_vector, vector
        )
        results.append({
            "source": row["source"],
            "page": row["page"],
            "content": row["content"],
            "score": round(score, 4)
        })




    # 关键词增强排序

    question_words = [
        word.lower()
        for word in question.replace(
            "？", ""
        ).split()
    ]

    for item in results:

        keyword_bonus = 0

        content = item["content"].lower()

        for word in question_words:

            if word in content:
                keyword_bonus += 0.05

        item["score"] = round(
            item["score"] + keyword_bonus,
            4
        )

    results.sort(
        key=lambda item: item["score"],
        reverse=True
    )
    results = [
        item for item in results
        if item["score"] >= 0.65
    ]
    return results[:top_k]


def queue_versioned_pdf(
    pdf_bytes: bytes,
    *,
    organization_id: int,
    device_model_ids: Sequence[int],
    family_key: str,
    version: str,
    title: str,
    storage_dir: str | Path,
) -> dict:
    """Preserve the existing upload entry point while delegating preparation."""
    return create_ingestion_job(
        pdf_bytes,
        organization_id=organization_id,
        device_model_ids=device_model_ids,
        family_key=family_key,
        version=version,
        title=title,
        storage_dir=storage_dir,
    )


def _stored_pdf_path(storage_dir: str | Path, storage_key: str) -> Path:
    root = Path(storage_dir).resolve()
    path = (root / storage_key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("文档存储路径无效")
    return path


def process_ingestion_job(job_id: int, *, storage_dir: str | Path) -> dict:
    """Keep the existing entry point while using lease-owned job transitions."""
    job = claim_job(uuid4().hex, job_id=job_id)
    if job is None:
        raise ValueError("入库任务不存在或状态不可处理")
    return _process_claimed_ingestion(job, storage_dir=storage_dir)


def _complete_ingestion_with_retry(job: ClaimedJob, chunks: list[IngestionChunk]) -> None:
    """Retry a commit only after its previous outcome has been checked."""
    failures = 0
    needs_inspection = False
    while True:
        try:
            if needs_inspection:
                outcome = inspect_completion(
                    job.job_id, job.worker_id, job.lease_token, chunks,
                )
                if outcome == "succeeded":
                    logger.info("Worker stage=complete job_id=%s already committed", job.job_id)
                    return
                needs_inspection = False
            complete_job(job.job_id, job.worker_id, job.lease_token, chunks)
            return
        except JobLeaseLostError:
            logger.error(
                "Worker stage=complete job_id=%s lost lease; stopping submission",
                job.job_id,
            )
            raise
        except sqlite3.Error as error:
            failures += 1
            needs_inspection = True
            inspection_error = None
            try:
                outcome = inspect_completion(
                    job.job_id, job.worker_id, job.lease_token, chunks,
                )
                if outcome == "succeeded":
                    logger.info("Worker stage=complete job_id=%s committed", job.job_id)
                    return
            except JobLeaseLostError:
                logger.error(
                    "Worker stage=complete-check job_id=%s lost lease; stopping submission",
                    job.job_id,
                )
                raise
            except sqlite3.Error as check_error:
                inspection_error = check_error
                logger.warning(
                    "Worker stage=complete-check job_id=%s unavailable after %s "
                    "database failure(s): %s: %s",
                    job.job_id, failures, type(check_error).__name__, check_error,
                )
            permanent_error = (
                inspection_error if inspection_error is not None
                and not _is_retryable_database_error(inspection_error) else error
            )
            if not _is_retryable_database_error(permanent_error):
                logger.error(
                    "Worker stage=complete job_id=%s non-retryable database "
                    "failure(s): %s: %s",
                    job.job_id, type(permanent_error).__name__, permanent_error,
                )
                raise permanent_error
            if failures >= DB_RETRY_MAX_FAILURES:
                logger.error(
                    "Worker stage=complete job_id=%s database retry budget "
                    "exhausted after %s failure(s): %s: %s",
                    job.job_id, failures, type(error).__name__, error,
                )
                raise WorkerDatabaseRetriesExhausted(
                    f"Job {job.job_id} completion database retries exhausted"
                ) from error
            delay = _database_retry_delay(failures)
            logger.warning(
                "Worker stage=complete job_id=%s database error (%s); "
                "retrying verification in %.1fs, failure=%s/%s: %s",
                job.job_id, type(error).__name__, delay, failures,
                DB_RETRY_MAX_FAILURES, error,
            )
            time.sleep(delay)


def _process_claimed_ingestion(job: ClaimedJob, *, storage_dir: str | Path) -> dict:
    """Run the existing PDF pipeline with a renewable lease."""
    heartbeat_stop = Event()
    heartbeat_errors: list[Exception] = []

    def heartbeat() -> None:
        while not heartbeat_stop.wait(DEFAULT_LEASE_SECONDS / 3):
            try:
                renew_lease(job.job_id, job.worker_id, job.lease_token)
            except Exception as error:
                heartbeat_errors.append(error)
                logger.exception("Lease renewal failed for ingestion job %s", job.job_id)
                return

    heartbeat_thread = Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    try:
        try:
            if not job.storage_key:
                raise ValueError("文档缺少原始文件")
            pdf_bytes = _stored_pdf_path(storage_dir, job.storage_key).read_bytes()
            if job.sha256 and hashlib.sha256(pdf_bytes).hexdigest() != job.sha256:
                raise ValueError("PDF文件校验失败，原始文件可能已被修改")
            pages = extract_pdf_text(pdf_bytes)
            if len(pages) > MAX_PAGES:
                raise ValueError("PDF不能超过1000页")

            records = [
                (page["page"], chunk)
                for page in pages
                for chunk in split_text(page["text"])
            ]
            if not records:
                raise ValueError("未提取到文字，可能是扫描版PDF")
            if len(records) > MAX_VERSIONED_CHUNKS:
                raise ValueError("文档片段超过2000段，请拆分PDF后上传")

            embeddings = [get_embedding(content) for _, content in records]
            dimensions = {len(vector) for vector in embeddings}
            if len(dimensions) != 1 or 0 in dimensions:
                raise RuntimeError("Embedding维度不一致")
        except Exception as error:
            if isinstance(error, (JobLeaseLostError, sqlite3.Error)):
                raise
            logger.exception("PDF ingestion failed for job %s", job.job_id)
            try:
                fail_job(job.job_id, job.worker_id, job.lease_token, str(error))
            except JobLeaseLostError:
                logger.warning("Ingestion job %s lost its lease; failure was not committed", job.job_id)
            except Exception:
                logger.exception("Could not record failure for ingestion job %s", job.job_id)
            raise

        if heartbeat_errors:
            raise heartbeat_errors[0]
        chunks = [
            IngestionChunk(page, content, embeddings[index], EMBEDDING_MODEL)
            for index, (page, content) in enumerate(records)
        ]
        _complete_ingestion_with_retry(job, chunks)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join()

    return {
        "document_id": job.document_id,
        "job_id": job.job_id,
        "status": "review",
        "pages": len(pages),
        "chunks": len(records),
    }


def search_versioned_knowledge(
    question: str,
    *,
    organization_id: int,
    device_model_id: int,
    top_k: int = 3,
    threshold: float = 0.65,
) -> list[dict]:
    """Search only published material for one organization and device model."""
    if not question.strip():
        raise ValueError("问题不能为空")
    if organization_id <= 0 or device_model_id <= 0 or not 1 <= top_k <= 20:
        raise ValueError("检索参数无效")

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id AS chunk_id, c.page, c.content, c.embedding,
                   d.id AS document_id, d.title, d.version
            FROM document_chunks AS c
            JOIN documents AS d ON d.id = c.document_id
            JOIN document_models AS dm ON dm.document_id = d.id
            WHERE d.organization_id = ?
              AND dm.organization_id = ?
              AND dm.device_model_id = ?
              AND d.status = 'published'
              AND c.embedding_model = ?
            """,
            (organization_id, organization_id, device_model_id, EMBEDDING_MODEL),
        ).fetchall()
    if not rows:
        return []

    question_vector = get_embedding(question)
    keywords = re.findall(r"[a-z0-9_-]{2,}", question.lower())
    results = []
    for row in rows:
        vector = json.loads(row["embedding"])
        if len(vector) != len(question_vector):
            continue
        score = cosine_similarity(question_vector, vector)
        score += 0.05 * sum(word in row["content"].lower() for word in keywords)
        if score >= threshold:
            results.append({
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "title": row["title"],
                "version": row["version"],
                "page": row["page"],
                "content": row["content"],
                "score": round(score, 4),
            })
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:top_k]


def process_next_queued_job(*, storage_dir: str | Path) -> int | None:
    """Claim and process one available document, including expired leases."""
    try:
        recovered = recover_expired_jobs()
    except sqlite3.Error as error:
        logger.exception("Worker stage=recover failed: %s", type(error).__name__)
        raise
    if recovered.requeued_job_ids or recovered.failed_job_ids:
        logger.info(
            "Worker recovered expired jobs: requeued=%s failed=%s",
            recovered.requeued_job_ids, recovered.failed_job_ids,
        )
    try:
        job = claim_job(uuid4().hex)
    except sqlite3.Error as error:
        logger.exception("Worker stage=claim failed: %s", type(error).__name__)
        raise
    if job is None:
        return None

    try:
        _process_claimed_ingestion(job, storage_dir=storage_dir)
    except WorkerDatabaseRetriesExhausted:
        logger.exception(
            "Worker stage=process job_id=%s retry budget exhausted; stopping",
            job.job_id,
        )
        raise
    except sqlite3.Error as error:
        logger.exception(
            "Worker stage=process job_id=%s database failure: %s",
            job.job_id, type(error).__name__,
        )
        raise
    except Exception as error:
        logger.exception(
            "Worker stage=process job_id=%s failed: %s",
            job.job_id, type(error).__name__,
        )
    return job.job_id


def run_ingestion_worker(
    *, storage_dir: str | Path, poll_seconds: float = 3.0,
    initialize: bool = False,
) -> None:
    """Continuously poll for queued jobs until the process is interrupted."""
    if poll_seconds < 0.1:
        raise ValueError("轮询间隔不能小于0.1秒")
    logger.info("PDF ingestion worker started")
    consecutive_db_failures = 0
    initialized = not initialize
    while True:
        try:
            if not initialized:
                try:
                    init_db()
                except sqlite3.Error as error:
                    logger.exception("Worker stage=initialize failed: %s", type(error).__name__)
                    raise
                initialized = True
            job_id = process_next_queued_job(storage_dir=storage_dir)
        except sqlite3.OperationalError as error:
            if not _is_retryable_database_error(error):
                logger.exception(
                    "Worker non-retryable database error; stopping: %s",
                    type(error).__name__,
                )
                raise
            consecutive_db_failures += 1
            if consecutive_db_failures >= DB_RETRY_MAX_FAILURES:
                logger.exception(
                    "Worker database retry budget exhausted after %s failures; "
                    "stopping at stage=initialize/recover/claim: %s",
                    consecutive_db_failures, type(error).__name__,
                )
                raise
            delay = _database_retry_delay(consecutive_db_failures)
            logger.warning(
                "Worker database error (%s); retrying in %.1fs after %s/%s "
                "consecutive failure(s): %s",
                type(error).__name__, delay, consecutive_db_failures,
                DB_RETRY_MAX_FAILURES, error,
            )
            time.sleep(delay)
            continue
        consecutive_db_failures = 0
        if job_id is None:
            time.sleep(poll_seconds)


def retry_failed_job(job_id: int) -> None:
    """Requeue a failed job after its cause has been addressed."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT j.status, j.attempt_count,
                   d.id AS document_id, d.status AS document_status
            FROM ingestion_jobs AS j
            JOIN documents AS d ON d.id = j.document_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None or row["status"] != "failed" or row["document_status"] != "failed":
            raise ValueError("只有失败的入库任务可以重试")
        if row["attempt_count"] >= DEFAULT_MAX_ATTEMPTS:
            raise ValueError("任务已达到最大尝试次数，不能继续重试")
        chunk_count = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?",
            (row["document_id"],),
        ).fetchone()[0]
        if chunk_count:
            raise ValueError("文档已有片段，无法安全重试")
        document_update = conn.execute(
            "UPDATE documents SET status = 'processing' "
            "WHERE id = ? AND status = 'failed'",
            (row["document_id"],),
        )
        if document_update.rowcount != 1:
            raise ValueError("文档状态不允许重试")
        job_update = conn.execute(
            """
            UPDATE ingestion_jobs
            SET status = 'queued', error_message = NULL,
                started_at = NULL, finished_at = NULL,
                worker_id = NULL, lease_token = NULL, lease_expires_at = NULL
            WHERE id = ? AND status = 'failed' AND attempt_count < ?
            """,
            (job_id, DEFAULT_MAX_ATTEMPTS),
        )
        if job_update.rowcount != 1:
            raise ValueError("任务状态不允许重试")


def recover_stale_running_job(
    job_id: int, *, min_age_seconds: int = 3600,
    confirmed_old_workers_stopped: bool = False,
) -> None:
    """Preserve the old entry point, but require explicit legacy-worker confirmation."""
    report = inspect_legacy_jobs(
        apply=True,
        confirmed_old_workers_stopped=confirmed_old_workers_stopped,
        min_age_seconds=min_age_seconds,
        job_id=job_id,
    )
    if report["requeued"] != 1:
        raise ValueError("任务不符合安全恢复条件；请先运行只读检查命令")


def _main() -> None:
    parser = argparse.ArgumentParser(description="PDF knowledge-base ingestion worker")
    commands = parser.add_subparsers(dest="command", required=True)
    work = commands.add_parser("work", help="Process queued documents")
    work.add_argument("--once", action="store_true", help="Process at most one job")
    work.add_argument("--poll-seconds", type=float, default=3.0)
    retry = commands.add_parser("retry-failed", help="Requeue a failed job")
    retry.add_argument("job_id", type=int)
    recover = commands.add_parser("recover-running", help="Requeue a stale running job")
    recover.add_argument("job_id", type=int)
    recover.add_argument("--min-age-seconds", type=int, default=3600)
    recover.add_argument("--apply", action="store_true")
    recover.add_argument("--confirm-old-workers-stopped", action="store_true")
    args = parser.parse_args()

    if args.command == "retry-failed":
        init_db()
        retry_failed_job(args.job_id)
        print(f"Job {args.job_id} is queued")
        return
    if args.command == "recover-running":
        if args.apply:
            try:
                recover_stale_running_job(
                    args.job_id,
                    min_age_seconds=args.min_age_seconds,
                    confirmed_old_workers_stopped=args.confirm_old_workers_stopped,
                )
            except (ValueError, FileNotFoundError) as error:
                parser.error(str(error))
            print(f"Job {args.job_id} is queued")
        else:
            report = inspect_legacy_jobs(
                min_age_seconds=args.min_age_seconds, job_id=args.job_id,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    storage_dir = os.getenv("DOCUMENT_STORAGE_PATH")
    if not storage_dir or not Path(storage_dir).is_dir():
        parser.error("DOCUMENT_STORAGE_PATH must point to the existing PDF storage directory")
    if args.once:
        init_db()
        job_id = process_next_queued_job(storage_dir=storage_dir)
        print("No queued jobs" if job_id is None else f"Handled job {job_id}; check its status")
    else:
        run_ingestion_worker(
            storage_dir=storage_dir, poll_seconds=args.poll_seconds, initialize=True,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _main()
