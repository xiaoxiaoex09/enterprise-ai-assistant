"""Lease, ownership, recovery, and transaction tests for ingestion jobs."""

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import database
from pdf_rag_service import retry_failed_job
from app.services.ingestion_job_service import (
    CompletionOwnershipUnknownError,
    IngestionChunk,
    JobLeaseLostError,
    claim_job,
    complete_job,
    fail_job,
    inspect_completion,
    recover_expired_jobs,
    renew_lease,
)


class IngestionJobServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database_path = Path(temporary_directory.name) / "jobs.db"
        database_patch = patch.object(database, "DATABASE_NAME", self.database_path)
        database_patch.start()
        self.addCleanup(database_patch.stop)
        database.init_db()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES (1, '企业甲')")
            conn.execute(
                "INSERT INTO documents "
                "(id, organization_id, family_key, title, document_type, version, status, storage_key) "
                "VALUES (10, 1, 'guide', '设备手册', 'manual', '1.0', 'processing', '1/test.pdf')"
            )
            conn.execute("INSERT INTO ingestion_jobs (id, document_id) VALUES (1, 10)")

    def expire(self, job_id: int = 1) -> None:
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE ingestion_jobs "
                "SET lease_expires_at = CAST(strftime('%s', 'now') AS INTEGER) - 1 "
                "WHERE id = ?",
                (job_id,),
            )

    def test_two_workers_cannot_claim_the_same_job(self) -> None:
        barrier = threading.Barrier(2)

        def competing_claim(worker_id: str):
            barrier.wait(timeout=10)
            return claim_job(worker_id, job_id=1)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(competing_claim, ("worker-a", "worker-b")))
        winners = [claim for claim in claims if claim is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].attempt_count, 1)
        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT status, attempt_count, worker_id, lease_token FROM ingestion_jobs WHERE id = 1"
            ).fetchone()
        self.assertEqual((row["status"], row["attempt_count"]), ("running", 1))
        self.assertEqual(row["worker_id"], winners[0].worker_id)
        self.assertEqual(row["lease_token"], winners[0].lease_token)

    def test_expired_lease_can_be_taken_over_but_old_worker_cannot_commit(self) -> None:
        first = claim_job("worker-a", job_id=1)
        self.assertIsNotNone(first)
        with self.assertRaises(JobLeaseLostError):
            renew_lease(1, "worker-a", "wrong-token")
        self.assertGreater(renew_lease(1, "worker-a", first.lease_token), 0)

        self.expire()
        second = claim_job("worker-b", job_id=1)
        self.assertIsNotNone(second)
        self.assertEqual(second.attempt_count, 2)
        self.assertNotEqual(first.lease_token, second.lease_token)
        chunks = [IngestionChunk(1, "内容", [0.1, 0.2], "test-model")]
        with self.assertRaises(JobLeaseLostError):
            renew_lease(1, "worker-a", first.lease_token)
        with self.assertRaises(JobLeaseLostError):
            complete_job(1, "worker-a", first.lease_token, chunks)
        with self.assertRaises(JobLeaseLostError):
            fail_job(1, "worker-a", first.lease_token, "old worker failed")

        complete_job(1, "worker-b", second.lease_token, chunks)
        with database.get_connection() as conn:
            job = conn.execute("SELECT status, lease_token FROM ingestion_jobs WHERE id = 1").fetchone()
            document = conn.execute("SELECT status FROM documents WHERE id = 10").fetchone()
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE document_id = 10"
            ).fetchone()[0]
        self.assertEqual((job["status"], job["lease_token"]), ("succeeded", None))
        self.assertEqual(document["status"], "review")
        self.assertEqual(chunk_count, 1)

    def test_reused_worker_id_cannot_claim_new_attempt_success(self) -> None:
        first = claim_job("stable-worker", job_id=1)
        self.expire()
        second = claim_job("stable-worker", job_id=1)
        self.assertNotEqual(first.lease_token, second.lease_token)
        chunks = [IngestionChunk(1, "内容", [0.1], "test-model")]
        complete_job(1, second.worker_id, second.lease_token, chunks)

        with self.assertRaises(JobLeaseLostError):
            inspect_completion(1, first.worker_id, first.lease_token, chunks)
        with self.assertRaises(JobLeaseLostError):
            renew_lease(1, first.worker_id, first.lease_token)
        with self.assertRaises(JobLeaseLostError):
            complete_job(1, first.worker_id, first.lease_token, chunks)
        with self.assertRaises(JobLeaseLostError):
            fail_job(1, first.worker_id, first.lease_token, "old attempt")
        self.assertEqual(inspect_completion(1, second.worker_id, second.lease_token,
                                            chunks), "succeeded")
        with database.get_connection() as conn:
            job = conn.execute(
                "SELECT status, worker_id, lease_token, completed_lease_token, attempt_count "
                "FROM ingestion_jobs WHERE id = 1"
            ).fetchone()
            document = conn.execute("SELECT status FROM documents WHERE id = 10").fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE document_id = 10"
            ).fetchone()[0]
        self.assertEqual((job["status"], job["worker_id"], job["lease_token"],
                          job["completed_lease_token"], job["attempt_count"]),
                         ("succeeded", "stable-worker", None, second.lease_token, 2))
        self.assertEqual((document["status"], count), ("review", 1))

    def test_different_worker_cannot_inspect_another_attempt_success(self) -> None:
        first = claim_job("worker-a", job_id=1)
        self.expire()
        second = claim_job("worker-b", job_id=1)
        chunks = [IngestionChunk(1, "内容", [0.1], "test-model")]
        complete_job(1, second.worker_id, second.lease_token, chunks)
        with self.assertRaises(JobLeaseLostError):
            inspect_completion(1, first.worker_id, first.lease_token, chunks)
        self.assertEqual(inspect_completion(1, second.worker_id, second.lease_token,
                                            chunks), "succeeded")

    def test_live_running_attempt_is_retryable_but_unavailable_db_is_unknown(self) -> None:
        claim = claim_job("worker-a", job_id=1)
        chunks = [IngestionChunk(1, "内容", [0.1], "test-model")]
        self.assertEqual(inspect_completion(1, claim.worker_id, claim.lease_token,
                                            chunks), "running")
        unavailable_path = self.database_path.parent / "missing-parent" / "jobs.db"
        with patch.object(database, "DATABASE_NAME", unavailable_path):
            with self.assertRaises(sqlite3.OperationalError):
                inspect_completion(1, claim.worker_id, claim.lease_token, chunks)
        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT status, lease_token, completed_lease_token "
                "FROM ingestion_jobs WHERE id = 1"
            ).fetchone()
        self.assertEqual(tuple(row), ("running", claim.lease_token, None))

    def test_historical_success_without_completion_token_is_not_attributed(self) -> None:
        claim = claim_job("worker-a", job_id=1)
        chunks = [IngestionChunk(1, "内容", [0.1], "test-model")]
        complete_job(1, claim.worker_id, claim.lease_token, chunks)
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE ingestion_jobs SET completed_lease_token = NULL WHERE id = 1"
            )
        database.init_db()
        database.init_db()
        with self.assertRaises(CompletionOwnershipUnknownError):
            inspect_completion(1, claim.worker_id, claim.lease_token, chunks)
        with database.get_connection() as conn:
            job = conn.execute(
                "SELECT status, lease_token, completed_lease_token "
                "FROM ingestion_jobs WHERE id = 1"
            ).fetchone()
            document = conn.execute("SELECT status FROM documents WHERE id = 10").fetchone()
            count = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        self.assertEqual(tuple(job), ("succeeded", None, None))
        self.assertEqual((document["status"], count), ("review", 1))

    def test_failure_submission_requires_current_lease(self) -> None:
        claim = claim_job("worker-a", job_id=1)
        with self.assertRaises(JobLeaseLostError):
            fail_job(1, "worker-b", claim.lease_token, "wrong owner")
        fail_job(1, "worker-a", claim.lease_token, "embedding failed")
        with database.get_connection() as conn:
            job = conn.execute(
                "SELECT status, error_message FROM ingestion_jobs WHERE id = 1"
            ).fetchone()
            document = conn.execute("SELECT status FROM documents WHERE id = 10").fetchone()
        self.assertEqual((job["status"], job["error_message"]), ("failed", "embedding failed"))
        self.assertEqual(document["status"], "failed")

    def test_renewing_lease_never_shortens_it(self) -> None:
        claim = claim_job("worker-a", job_id=1, lease_seconds=180)
        renewed_expiry = renew_lease(1, "worker-a", claim.lease_token, lease_seconds=1)
        self.assertGreaterEqual(renewed_expiry, claim.lease_expires_at)

    def test_recovery_only_touches_expired_and_respects_attempt_limit(self) -> None:
        first = claim_job("worker-a", job_id=1)
        self.assertEqual(recover_expired_jobs().requeued_job_ids, ())
        self.expire()
        recovered = recover_expired_jobs()
        self.assertEqual(recovered.requeued_job_ids, (1,))
        with self.assertRaises(JobLeaseLostError):
            complete_job(1, "worker-a", first.lease_token,
                         [IngestionChunk(1, "内容", [0.1], "test-model")])
        second = claim_job("worker-b", job_id=1)
        self.assertEqual(second.attempt_count, 2)
        self.expire()
        self.assertEqual(recover_expired_jobs().requeued_job_ids, (1,))
        third = claim_job("worker-c", job_id=1)
        self.assertEqual(third.attempt_count, 3)
        self.expire()
        recovered = recover_expired_jobs()
        self.assertEqual(recovered.failed_job_ids, (1,))
        self.assertIsNone(claim_job("worker-d", job_id=1))
        with database.get_connection() as conn:
            job = conn.execute(
                "SELECT status, attempt_count FROM ingestion_jobs WHERE id = 1"
            ).fetchone()
            document = conn.execute("SELECT status FROM documents WHERE id = 10").fetchone()
        self.assertEqual((job["status"], job["attempt_count"]), ("failed", 3))
        self.assertEqual(document["status"], "failed")
        with self.assertRaisesRegex(ValueError, "最大尝试次数"):
            retry_failed_job(1)
        self.assertIsNone(claim_job("worker-d", job_id=1))

    def test_manual_retry_preserves_attempt_limit(self) -> None:
        first = claim_job("worker-a", job_id=1)
        fail_job(1, "worker-a", first.lease_token, "temporary failure")
        retry_failed_job(1)
        second = claim_job("worker-b", job_id=1)
        self.assertEqual(second.attempt_count, 2)
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE ingestion_jobs SET status = 'queued', worker_id = NULL, "
                "lease_token = NULL, lease_expires_at = NULL, attempt_count = 3 "
                "WHERE id = 1"
            )
        self.assertIsNone(claim_job("worker-c", job_id=1))

    def test_exhausted_queued_job_is_failed_by_recovery(self) -> None:
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE ingestion_jobs SET attempt_count = 3 WHERE id = 1"
            )
        self.assertEqual(recover_expired_jobs().failed_job_ids, (1,))
        with database.get_connection() as conn:
            job = conn.execute(
                "SELECT status, attempt_count, error_message "
                "FROM ingestion_jobs WHERE id = 1"
            ).fetchone()
            document = conn.execute(
                "SELECT status FROM documents WHERE id = 10"
            ).fetchone()[0]
        self.assertEqual((job["status"], job["attempt_count"]), ("failed", 3))
        self.assertIn("最大尝试次数", job["error_message"])
        self.assertEqual(document, "failed")

    def test_concurrent_manual_retry_cannot_requeue_twice(self) -> None:
        first = claim_job("worker-a", job_id=1)
        fail_job(1, "worker-a", first.lease_token, "temporary failure")
        barrier = threading.Barrier(2)

        def retry_once(_index: int) -> str:
            barrier.wait(timeout=10)
            try:
                retry_failed_job(1)
                return "retried"
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(retry_once, range(2)))
        self.assertEqual(sorted(results), ["rejected", "retried"])
        with database.get_connection() as conn:
            job = conn.execute(
                "SELECT status, attempt_count FROM ingestion_jobs WHERE id = 1"
            ).fetchone()
            document = conn.execute(
                "SELECT status FROM documents WHERE id = 10"
            ).fetchone()[0]
        self.assertEqual(tuple(job), ("queued", 1))
        self.assertEqual(document, "processing")

    def test_chunk_insert_failure_rolls_back_success_transition(self) -> None:
        claim = claim_job("worker-a", job_id=1)
        with database.get_connection() as conn:
            conn.execute(
                "CREATE TRIGGER reject_chunk BEFORE INSERT ON document_chunks "
                "BEGIN SELECT RAISE(ABORT, 'injected chunk failure'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected chunk failure"):
            complete_job(1, "worker-a", claim.lease_token,
                         [IngestionChunk(1, "内容", [0.1], "test-model")])
        with database.get_connection() as conn:
            job = conn.execute(
                "SELECT status, lease_token, completed_lease_token "
                "FROM ingestion_jobs WHERE id = 1"
            ).fetchone()
            document = conn.execute("SELECT status FROM documents WHERE id = 10").fetchone()
            count = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        self.assertEqual((job["status"], job["lease_token"]), ("running", claim.lease_token))
        self.assertIsNone(job["completed_lease_token"])
        self.assertEqual(document["status"], "processing")
        self.assertEqual(count, 0)

    def test_existing_job_schema_migrates_idempotently(self) -> None:
        legacy_path = self.database_path.with_name("legacy.db")
        with closing(sqlite3.connect(legacy_path)) as conn, conn:
            conn.execute(
                "CREATE TABLE ingestion_jobs (id INTEGER PRIMARY KEY, document_id INTEGER, "
                "status TEXT, error_message TEXT, created_at TEXT, started_at TEXT, finished_at TEXT)"
            )
            conn.execute(
                "INSERT INTO ingestion_jobs VALUES (7, 99, 'queued', NULL, "
                "CURRENT_TIMESTAMP, NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO ingestion_jobs VALUES (8, 99, 'succeeded', NULL, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        with patch.object(database, "DATABASE_NAME", legacy_path):
            database.init_db()
            database.init_db()
            with database.get_connection() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(ingestion_jobs)")}
                row = conn.execute(
                    "SELECT status, attempt_count, worker_id, lease_token, "
                    "lease_expires_at, completed_lease_token "
                    "FROM ingestion_jobs WHERE id = 7"
                ).fetchone()
                historical_success = conn.execute(
                    "SELECT status, completed_lease_token FROM ingestion_jobs WHERE id = 8"
                ).fetchone()
        self.assertTrue({"worker_id", "lease_token", "lease_expires_at",
                         "attempt_count", "completed_lease_token"} <= columns)
        self.assertEqual(tuple(row), ("queued", 0, None, None, None, None))
        self.assertEqual(tuple(historical_success), ("succeeded", None))


if __name__ == "__main__":
    unittest.main()
