"""Database-only lease and state transitions for PDF ingestion jobs."""

import json
import math
from dataclasses import dataclass
from typing import Sequence
from uuid import uuid4

from database import get_connection


DB_NOW = "CAST(strftime('%s', 'now') AS INTEGER)"
DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 3


class JobLeaseLostError(Exception):
    """The job is no longer owned by this execution attempt."""


class CompletionOwnershipUnknownError(JobLeaseLostError):
    """A legacy success has no persisted attempt identity to verify."""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: int
    document_id: int
    storage_key: str | None
    sha256: str | None
    worker_id: str
    lease_token: str
    lease_expires_at: int
    attempt_count: int


@dataclass(frozen=True)
class IngestionChunk:
    page: int
    content: str
    embedding: Sequence[float]
    embedding_model: str


@dataclass(frozen=True)
class RecoveryResult:
    requeued_job_ids: tuple[int, ...]
    failed_job_ids: tuple[int, ...]


def _validate_owner(worker_id: str, lease_token: str | None = None) -> None:
    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
        raise ValueError("Worker ID无效")
    if lease_token is not None and (not isinstance(lease_token, str) or not lease_token):
        raise ValueError("租约令牌无效")


def _validate_lease_seconds(lease_seconds: int) -> None:
    if not isinstance(lease_seconds, int) or lease_seconds <= 0:
        raise ValueError("租约时长必须为正整数")


def _validate_max_attempts(max_attempts: int) -> None:
    if not isinstance(max_attempts, int) or max_attempts <= 0:
        raise ValueError("最大尝试次数必须为正整数")


def claim_job(
    worker_id: str,
    *,
    job_id: int | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> ClaimedJob | None:
    """Atomically claim a queued job or take over an expired owned job."""
    _validate_owner(worker_id)
    _validate_lease_seconds(lease_seconds)
    _validate_max_attempts(max_attempts)
    if job_id is not None and (not isinstance(job_id, int) or job_id <= 0):
        raise ValueError("任务ID无效")

    job_filter = "AND j.id = ?" if job_id is not None else ""
    parameters: tuple[int, ...] = (max_attempts,)
    if job_id is not None:
        parameters += (job_id,)

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        candidate = conn.execute(
            f"""
            SELECT j.id, j.document_id, j.attempt_count,
                   d.storage_key, d.sha256
            FROM ingestion_jobs AS j
            JOIN documents AS d ON d.id = j.document_id
            WHERE d.status = 'processing'
              AND j.attempt_count < ?
              AND (j.status = 'queued' OR
                   (j.status = 'running' AND j.lease_expires_at <= {DB_NOW}))
              {job_filter}
            ORDER BY CASE WHEN j.status = 'queued' THEN 0 ELSE 1 END,
                     j.created_at, j.id
            LIMIT 1
            """,
            parameters,
        ).fetchone()
        if candidate is None:
            return None

        lease_token = uuid4().hex
        updated = conn.execute(
            f"""
            UPDATE ingestion_jobs
            SET status = 'running', worker_id = ?, lease_token = ?,
                completed_lease_token = NULL,
                lease_expires_at = {DB_NOW} + ?,
                attempt_count = attempt_count + 1,
                started_at = CURRENT_TIMESTAMP, finished_at = NULL,
                error_message = NULL
            WHERE id = ? AND attempt_count = ? AND attempt_count < ?
              AND (status = 'queued' OR
                   (status = 'running' AND lease_expires_at <= {DB_NOW}))
              AND EXISTS (
                  SELECT 1 FROM documents
                  WHERE id = ingestion_jobs.document_id AND status = 'processing'
              )
            """,
            (worker_id, lease_token, lease_seconds,
             candidate["id"], candidate["attempt_count"], max_attempts),
        )
        if updated.rowcount != 1:
            return None
        lease_expires_at = conn.execute(
            "SELECT lease_expires_at FROM ingestion_jobs WHERE id = ?",
            (candidate["id"],),
        ).fetchone()[0]

    return ClaimedJob(
        job_id=candidate["id"],
        document_id=candidate["document_id"],
        storage_key=candidate["storage_key"],
        sha256=candidate["sha256"],
        worker_id=worker_id,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        attempt_count=candidate["attempt_count"] + 1,
    )


def renew_lease(
    job_id: int,
    worker_id: str,
    lease_token: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> int:
    """Extend a live lease and return its new expiry time."""
    _validate_owner(worker_id, lease_token)
    _validate_lease_seconds(lease_seconds)
    with get_connection() as conn:
        updated = conn.execute(
            f"""
            UPDATE ingestion_jobs
            SET lease_expires_at = MAX(lease_expires_at, {DB_NOW} + ?)
            WHERE id = ? AND status = 'running'
              AND worker_id = ? AND lease_token = ?
              AND lease_expires_at > {DB_NOW}
            """,
            (lease_seconds, job_id, worker_id, lease_token),
        )
        if updated.rowcount != 1:
            raise JobLeaseLostError("任务租约已失效")
        return conn.execute(
            "SELECT lease_expires_at FROM ingestion_jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]


def _validate_chunks(chunks: Sequence[IngestionChunk]) -> tuple[IngestionChunk, ...]:
    records = tuple(chunks)
    if not records:
        raise ValueError("文档片段不能为空")
    dimensions = {len(chunk.embedding) for chunk in records}
    models = {chunk.embedding_model for chunk in records}
    if len(dimensions) != 1 or 0 in dimensions or len(models) != 1:
        raise ValueError("Embedding维度或模型不一致")
    for chunk in records:
        if (
            chunk.page <= 0 or not chunk.content or not chunk.embedding_model
            or any(not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in chunk.embedding)
        ):
            raise ValueError("文档片段无效")
    return records


def complete_job(
    job_id: int,
    worker_id: str,
    lease_token: str,
    chunks: Sequence[IngestionChunk],
) -> None:
    """Commit chunks and success states only for the current lease owner."""
    _validate_owner(worker_id, lease_token)
    records = _validate_chunks(chunks)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT document_id FROM ingestion_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None:
            raise JobLeaseLostError("任务租约已失效")
        updated = conn.execute(
            f"""
            UPDATE ingestion_jobs
            SET status = 'succeeded', lease_token = NULL,
                completed_lease_token = ?,
                lease_expires_at = NULL, finished_at = CURRENT_TIMESTAMP,
                error_message = NULL
            WHERE id = ? AND status = 'running'
              AND worker_id = ? AND lease_token = ?
              AND lease_expires_at > {DB_NOW}
            """,
            (lease_token, job_id, worker_id, lease_token),
        )
        if updated.rowcount != 1:
            raise JobLeaseLostError("任务租约已失效")
        existing = conn.execute(
            "SELECT 1 FROM document_chunks WHERE document_id = ? LIMIT 1",
            (job["document_id"],),
        ).fetchone()
        if existing is not None:
            raise ValueError("文档已有片段，无法重复完成任务")
        conn.executemany(
            """
            INSERT INTO document_chunks
                (document_id, chunk_index, page, content, embedding, embedding_model)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (job["document_id"], index, chunk.page, chunk.content,
                 json.dumps(list(chunk.embedding)), chunk.embedding_model)
                for index, chunk in enumerate(records)
            ),
        )
        document_update = conn.execute(
            "UPDATE documents SET status = 'review' WHERE id = ? AND status = 'processing'",
            (job["document_id"],),
        )
        if document_update.rowcount != 1:
            raise JobLeaseLostError("文档状态不允许完成任务")


def inspect_completion(
    job_id: int,
    worker_id: str,
    lease_token: str,
    chunks: Sequence[IngestionChunk],
) -> str:
    """Resolve an uncertain commit before retrying it on a fresh connection.

    Returns ``succeeded`` only for this worker's exact committed result, or
    ``running`` only while this worker still owns a live, uncommitted lease.
    """
    _validate_owner(worker_id, lease_token)
    records = _validate_chunks(chunks)
    with get_connection() as conn:
        conn.execute("BEGIN")
        job = conn.execute(
            f"""
            SELECT j.document_id, j.status, j.worker_id, j.lease_token,
                   j.completed_lease_token,
                   j.lease_expires_at, d.status AS document_status,
                   j.lease_expires_at > {DB_NOW} AS lease_is_live
            FROM ingestion_jobs AS j
            JOIN documents AS d ON d.id = j.document_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if job is None or job["worker_id"] != worker_id:
            raise JobLeaseLostError("任务已被其他Worker接管或不存在")
        stored = conn.execute(
            "SELECT chunk_index, page, content, embedding, embedding_model "
            "FROM document_chunks "
            "WHERE document_id = ? ORDER BY chunk_index",
            (job["document_id"],),
        ).fetchall()
        if job["status"] == "succeeded":
            if job["completed_lease_token"] is None:
                raise CompletionOwnershipUnknownError("历史成功任务缺少完成尝试标识")
            if job["completed_lease_token"] != lease_token:
                raise JobLeaseLostError("任务由其他执行尝试完成")
            if job["document_status"] != "review" or len(stored) != len(records):
                raise RuntimeError("任务已成功但文档或片段状态不一致")
            for index, (actual, expected) in enumerate(zip(stored, records)):
                if (
                    actual["chunk_index"] != index
                    or actual["page"] != expected.page
                    or actual["content"] != expected.content
                    or actual["embedding_model"] != expected.embedding_model
                    or json.loads(actual["embedding"]) != list(expected.embedding)
                ):
                    raise RuntimeError("任务已成功但片段内容与提交结果不一致")
            return "succeeded"
        if (
            job["status"] != "running" or job["lease_token"] != lease_token
            or not job["lease_is_live"] or job["document_status"] != "processing"
        ):
            raise JobLeaseLostError("任务租约已失效")
        if stored:
            raise RuntimeError("运行中的任务已有片段，禁止重复提交")
        return "running"


def fail_job(job_id: int, worker_id: str, lease_token: str, error_message: str) -> None:
    """Commit failed states only for the current lease owner."""
    _validate_owner(worker_id, lease_token)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT document_id FROM ingestion_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None:
            raise JobLeaseLostError("任务租约已失效")
        updated = conn.execute(
            f"""
            UPDATE ingestion_jobs
            SET status = 'failed', lease_token = NULL,
                completed_lease_token = NULL,
                lease_expires_at = NULL, error_message = ?,
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'running'
              AND worker_id = ? AND lease_token = ?
              AND lease_expires_at > {DB_NOW}
            """,
            (str(error_message)[:500], job_id, worker_id, lease_token),
        )
        if updated.rowcount != 1:
            raise JobLeaseLostError("任务租约已失效")
        document_update = conn.execute(
            "UPDATE documents SET status = 'failed' WHERE id = ? AND status = 'processing'",
            (job["document_id"],),
        )
        if document_update.rowcount != 1:
            raise JobLeaseLostError("文档状态不允许失败提交")


def recover_expired_jobs(*, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> RecoveryResult:
    """Requeue expired leases and fail expired or queued exhausted jobs."""
    _validate_max_attempts(max_attempts)
    requeued: list[int] = []
    failed: list[int] = []
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        expired = conn.execute(
            f"""
            SELECT j.id, j.document_id, j.status, j.attempt_count
            FROM ingestion_jobs AS j
            JOIN documents AS d ON d.id = j.document_id
            WHERE d.status = 'processing'
              AND ((j.status = 'running'
                    AND j.lease_expires_at IS NOT NULL
                    AND j.lease_expires_at <= {DB_NOW})
                   OR (j.status = 'queued' AND j.attempt_count >= ?))
            ORDER BY j.id
            """,
            (max_attempts,),
        ).fetchall()
        for job in expired:
            if job["attempt_count"] < max_attempts:
                updated = conn.execute(
                    """
                    UPDATE ingestion_jobs
                    SET status = 'queued', worker_id = NULL, lease_token = NULL,
                        completed_lease_token = NULL,
                        lease_expires_at = NULL, started_at = NULL,
                        finished_at = NULL, error_message = NULL
                    WHERE id = ? AND status = 'running'
                    """,
                    (job["id"],),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("过期任务重新排队失败")
                requeued.append(job["id"])
            else:
                updated = conn.execute(
                    """
                    UPDATE ingestion_jobs
                    SET status = 'failed', worker_id = NULL, lease_token = NULL,
                        completed_lease_token = NULL,
                        lease_expires_at = NULL, finished_at = CURRENT_TIMESTAMP,
                        error_message = '任务已达到最大尝试次数'
                    WHERE id = ? AND status = ?
                    """,
                    (job["id"], job["status"]),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("超限任务状态更新失败")
                document_update = conn.execute(
                    "UPDATE documents SET status = 'failed' WHERE id = ? AND status = 'processing'",
                    (job["document_id"],),
                )
                if document_update.rowcount != 1:
                    raise RuntimeError("超限任务关联文档状态更新失败")
                failed.append(job["id"])
    return RecoveryResult(tuple(requeued), tuple(failed))
