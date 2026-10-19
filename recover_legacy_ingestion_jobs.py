"""Inspect or explicitly requeue pre-lease running ingestion jobs.

Dry-run is the default. Never run --apply until all old worker processes have stopped.
"""

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

import database
from app.services.ingestion_job_service import DEFAULT_MAX_ATTEMPTS


LEGACY_CANDIDATES = """
    SELECT j.id AS job_id, j.document_id, j.status, j.worker_id,
           j.lease_token, j.lease_expires_at, j.attempt_count,
           j.started_at, j.error_message,
           d.organization_id, d.status AS document_status, d.storage_key,
           (SELECT COUNT(*) FROM document_chunks AS c
            WHERE c.document_id = j.document_id) AS chunk_count,
           j.started_at <= datetime('now', ?) AS old_enough
    FROM ingestion_jobs AS j
    LEFT JOIN documents AS d ON d.id = j.document_id
    WHERE j.status = 'running'
      AND (j.worker_id IS NULL OR j.lease_token IS NULL
           OR j.lease_expires_at IS NULL)
"""


def _skip_reason(row: sqlite3.Row) -> str | None:
    if any(row[field] is not None for field in
           ("worker_id", "lease_token", "lease_expires_at")):
        return "partial_lease_fields"
    if row["document_status"] is None:
        return "missing_document"
    if row["document_status"] != "processing":
        return "document_not_processing"
    if not row["storage_key"]:
        return "missing_pdf_reference"
    if not row["started_at"] or not row["old_enough"]:
        return "not_old_enough"
    if row["attempt_count"] >= DEFAULT_MAX_ATTEMPTS:
        return "attempt_limit_reached"
    if row["chunk_count"]:
        return "document_has_chunks"
    return None


def inspect_legacy_jobs(
    *,
    apply: bool = False,
    confirmed_old_workers_stopped: bool = False,
    min_age_seconds: int = 3600,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Report legacy candidates or atomically requeue only safe, stale ones."""
    if min_age_seconds < 300:
        raise ValueError("min_age_seconds must be at least 300")
    if apply and not confirmed_old_workers_stopped:
        raise ValueError("--apply requires --confirm-old-workers-stopped")
    if job_id is not None and job_id <= 0:
        raise ValueError("job_id must be positive")

    database_path = Path(database.DATABASE_NAME)
    if not database_path.is_file():
        raise FileNotFoundError(f"Database does not exist: {database_path}")

    age_modifier = f"-{min_age_seconds} seconds"
    results: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    requeued = 0
    would_requeue = 0
    skipped = 0

    if apply:
        connection_context = database.get_connection()
    else:
        connection_context = closing(sqlite3.connect(
            database_path.resolve().as_uri() + "?mode=ro", uri=True,
        ))

    with connection_context as conn:
        conn.row_factory = sqlite3.Row
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        candidate_query = LEGACY_CANDIDATES
        parameters: tuple[str | int, ...] = (age_modifier,)
        if job_id is not None:
            candidate_query += " AND j.id = ?"
            parameters += (job_id,)
        rows = conn.execute(candidate_query + " ORDER BY j.id", parameters).fetchall()
        for row in rows:
            reason = _skip_reason(row)
            item = {
                "job_id": row["job_id"],
                "document_id": row["document_id"],
                "organization_id": row["organization_id"],
                "attempt_count": row["attempt_count"],
                "previous_started_at": row["started_at"],
                "previous_error_message": row["error_message"],
            }
            if reason is not None:
                skipped += 1
                reasons[reason] += 1
                item.update(action="skipped", reason=reason)
            elif not apply:
                would_requeue += 1
                item.update(action="would_requeue")
            else:
                updated = conn.execute(
                    """
                    UPDATE ingestion_jobs
                    SET status = 'queued', started_at = NULL, finished_at = NULL
                    WHERE id = ? AND document_id = ? AND status = 'running'
                      AND worker_id IS NULL AND lease_token IS NULL
                      AND lease_expires_at IS NULL
                      AND started_at <= datetime('now', ?)
                      AND attempt_count < ?
                      AND EXISTS (
                          SELECT 1 FROM documents AS d
                          WHERE d.id = ingestion_jobs.document_id
                            AND d.organization_id = ?
                            AND d.status = 'processing'
                            AND d.storage_key IS NOT NULL AND d.storage_key != ''
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM document_chunks AS c
                          WHERE c.document_id = ingestion_jobs.document_id
                      )
                    """,
                    (row["job_id"], row["document_id"], age_modifier,
                     DEFAULT_MAX_ATTEMPTS, row["organization_id"]),
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        f"Legacy job {row['job_id']} changed during recovery; rolled back"
                    )
                after = conn.execute(
                    "SELECT status, document_id, attempt_count FROM ingestion_jobs "
                    "WHERE id = ?", (row["job_id"],),
                ).fetchone()
                if (
                    after is None or after["status"] != "queued"
                    or after["document_id"] != row["document_id"]
                    or after["attempt_count"] != row["attempt_count"]
                ):
                    raise RuntimeError(
                        f"Legacy job {row['job_id']} failed post-update check; rolled back"
                    )
                requeued += 1
                item.update(action="requeued")
            results.append(item)

    return {
        "mode": "apply" if apply else "dry-run",
        "database": str(database_path.resolve()),
        "identified": len(rows),
        "would_requeue": would_requeue,
        "requeued": requeued,
        "skipped": skipped,
        "skip_reasons": dict(reasons),
        "jobs": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Modify eligible jobs")
    parser.add_argument(
        "--confirm-old-workers-stopped", action="store_true",
        help="Confirm all pre-lease worker processes have stopped",
    )
    parser.add_argument("--min-age-seconds", type=int, default=3600)
    parser.add_argument("--job-id", type=int, help="Inspect or recover one job only")
    args = parser.parse_args()
    try:
        report = inspect_legacy_jobs(
            apply=args.apply,
            confirmed_old_workers_stopped=args.confirm_old_workers_stopped,
            min_age_seconds=args.min_age_seconds,
            job_id=args.job_id,
        )
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
