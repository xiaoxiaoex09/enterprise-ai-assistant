"""Integration tests for the existing ingestion worker and lease service."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import database
import pdf_rag_service
from app.services import ingestion_runner
from app.services.ingestion_job_service import (
    IngestionChunk, JobLeaseLostError, claim_job, complete_job, recover_expired_jobs,
)


class WorkerLeaseIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        database_patch = patch.object(database, "DATABASE_NAME", self.root / "worker.db")
        database_patch.start()
        self.addCleanup(database_patch.stop)
        database.init_db()
        self.pdf_path = self.root / "manual.pdf"
        self.pdf_path.write_bytes(b"%PDF-test")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES (1, 'test')")
            conn.execute(
                "INSERT INTO documents "
                "(id, organization_id, family_key, title, document_type, version, "
                "status, storage_key) "
                "VALUES (10, 1, 'manual', 'Manual', 'manual', '1', 'processing', 'manual.pdf')"
            )
            conn.execute("INSERT INTO ingestion_jobs (id, document_id) VALUES (1, 10)")

    def states(self) -> tuple[sqlite3.Row, sqlite3.Row, int]:
        with database.get_connection() as conn:
            job = conn.execute(
                "SELECT status, worker_id, lease_token, completed_lease_token, "
                "attempt_count, error_message "
                "FROM ingestion_jobs WHERE id = 1"
            ).fetchone()
            document = conn.execute("SELECT status FROM documents WHERE id = 10").fetchone()
            chunks = conn.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE document_id = 10"
            ).fetchone()[0]
        return job, document, chunks

    def test_existing_worker_entry_claims_and_completes_with_token(self) -> None:
        real_complete = pdf_rag_service.complete_job
        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"}
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1, 0.2]), patch.object(
            pdf_rag_service, "complete_job", wraps=real_complete
        ) as complete:
            result = pdf_rag_service.process_ingestion_job(1, storage_dir=self.root)
        self.assertEqual(result, {
            "document_id": 10, "job_id": 1, "status": "review", "pages": 1, "chunks": 1,
        })
        job, document, chunks = self.states()
        self.assertEqual((job["status"], job["attempt_count"]), ("succeeded", 1))
        self.assertIsNone(job["lease_token"])
        self.assertEqual(job["completed_lease_token"], complete.call_args.args[2])
        self.assertEqual((document["status"], chunks), ("review", 1))
        self.assertEqual(complete.call_args.args[:2], (1, job["worker_id"]))
        self.assertTrue(complete.call_args.args[2])

    def test_worker_failure_uses_fail_job_and_records_error(self) -> None:
        with patch.object(pdf_rag_service, "extract_pdf_text", side_effect=ValueError("bad PDF")):
            with self.assertLogs(pdf_rag_service.logger, level="ERROR"):
                with self.assertRaisesRegex(ValueError, "bad PDF"):
                    pdf_rag_service.process_ingestion_job(1, storage_dir=self.root)
        job, document, chunks = self.states()
        self.assertEqual((job["status"], job["error_message"]), ("failed", "bad PDF"))
        self.assertIsNone(job["completed_lease_token"])
        self.assertEqual((document["status"], chunks), ("failed", 0))
        self.assertIsNone(job["lease_token"])

    def test_completion_busy_once_then_commits_without_business_failure(self) -> None:
        real_complete = pdf_rag_service.complete_job
        attempts = []

        def fail_once_then_commit(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_complete(*args)

        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"},
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1]), patch.object(
            pdf_rag_service, "complete_job", side_effect=fail_once_then_commit,
        ), patch.object(pdf_rag_service, "fail_job") as fail, patch.object(
            pdf_rag_service.time, "sleep",
        ) as sleep:
            result = pdf_rag_service.process_ingestion_job(1, storage_dir=self.root)
        job, document, chunks = self.states()
        self.assertEqual(result["status"], "review")
        self.assertEqual((job["status"], document["status"], chunks),
                         ("succeeded", "review", 1))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleep.call_args_list[0].args, (1.0,))
        fail.assert_not_called()

    def test_completion_error_after_commit_is_recognized_without_replay(self) -> None:
        real_complete = pdf_rag_service.complete_job
        attempts = []

        def commit_then_raise(*args):
            attempts.append(args)
            real_complete(*args)
            raise sqlite3.OperationalError("database is locked")

        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"},
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1]), patch.object(
            pdf_rag_service, "complete_job", side_effect=commit_then_raise,
        ), patch.object(pdf_rag_service, "fail_job") as fail, patch.object(
            pdf_rag_service.time, "sleep",
        ) as sleep:
            result = pdf_rag_service.process_ingestion_job(1, storage_dir=self.root)
        job, document, chunks = self.states()
        self.assertEqual(result["status"], "review")
        self.assertEqual((job["status"], document["status"], chunks),
                         ("succeeded", "review", 1))
        self.assertEqual(job["completed_lease_token"], attempts[0][2])
        self.assertEqual(len(attempts), 1)
        sleep.assert_not_called()
        fail.assert_not_called()

    def test_unavailable_outcome_check_does_not_replay_committed_result(self) -> None:
        real_complete = pdf_rag_service.complete_job
        real_inspect = pdf_rag_service.inspect_completion
        commits = []
        inspections = []

        def commit_then_raise(*args):
            commits.append(args)
            real_complete(*args)
            raise sqlite3.OperationalError("database is locked")

        def inspect_after_temporary_error(*args):
            inspections.append(args)
            if len(inspections) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_inspect(*args)

        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"},
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1]), patch.object(
            pdf_rag_service, "complete_job", side_effect=commit_then_raise,
        ), patch.object(
            pdf_rag_service, "inspect_completion", side_effect=inspect_after_temporary_error,
        ), patch.object(pdf_rag_service, "fail_job") as fail, patch.object(
            pdf_rag_service.time, "sleep",
        ) as sleep:
            result = pdf_rag_service.process_ingestion_job(1, storage_dir=self.root)
        job, document, chunks = self.states()
        self.assertEqual(result["status"], "review")
        self.assertEqual((job["status"], document["status"], chunks),
                         ("succeeded", "review", 1))
        self.assertEqual(job["completed_lease_token"], commits[0][2])
        self.assertEqual((len(commits), len(inspections)), (1, 2))
        self.assertEqual(sleep.call_args_list[0].args, (1.0,))
        fail.assert_not_called()

    def test_completion_retry_exhaustion_leaves_recoverable_running_job(self) -> None:
        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"},
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1]), patch.object(
            pdf_rag_service, "complete_job",
            side_effect=sqlite3.OperationalError("database is locked"),
        ) as complete, patch.object(pdf_rag_service, "fail_job") as fail, patch.object(
            pdf_rag_service.time, "sleep",
        ) as sleep:
            with self.assertRaises(pdf_rag_service.WorkerDatabaseRetriesExhausted):
                pdf_rag_service.process_ingestion_job(1, storage_dir=self.root)
        job, document, chunks = self.states()
        self.assertEqual((job["status"], document["status"], chunks),
                         ("running", "processing", 0))
        self.assertTrue(job["lease_token"])
        self.assertEqual(complete.call_count, pdf_rag_service.DB_RETRY_MAX_FAILURES)
        self.assertEqual(sleep.call_count, pdf_rag_service.DB_RETRY_MAX_FAILURES - 1)
        fail.assert_not_called()
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE ingestion_jobs SET lease_expires_at = "
                "CAST(strftime('%s', 'now') AS INTEGER) - 1 WHERE id = 1",
            )
        self.assertEqual(recover_expired_jobs().requeued_job_ids, (1,))
        job, document, chunks = self.states()
        self.assertEqual((job["status"], document["status"], chunks),
                         ("queued", "processing", 0))

    def test_polling_worker_exits_on_completion_retry_exhaustion(self) -> None:
        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"},
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1]), patch.object(
            pdf_rag_service, "complete_job",
            side_effect=sqlite3.OperationalError("database is locked"),
        ), patch.object(pdf_rag_service, "fail_job") as fail, patch.object(
            pdf_rag_service.time, "sleep",
        ):
            with self.assertLogs(pdf_rag_service.logger, level="ERROR"):
                with self.assertRaises(pdf_rag_service.WorkerDatabaseRetriesExhausted):
                    pdf_rag_service.run_ingestion_worker(storage_dir=self.root)
        job, document, chunks = self.states()
        self.assertEqual((job["status"], document["status"], chunks),
                         ("running", "processing", 0))
        fail.assert_not_called()

    def test_replacement_during_commit_retry_stops_old_worker(self) -> None:
        replacement = []
        real_complete = pdf_rag_service.complete_job
        attempts = []

        def fail_first_attempt(*args):
            attempts.append(args)
            raise sqlite3.OperationalError("database is locked")

        def replace_during_backoff(_delay):
            with database.get_connection() as conn:
                conn.execute(
                    "UPDATE ingestion_jobs SET lease_expires_at = "
                    "CAST(strftime('%s', 'now') AS INTEGER) - 1 WHERE id = 1",
                )
            replacement.append(claim_job("worker-b", job_id=1))

        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"},
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1]), patch.object(
            pdf_rag_service, "complete_job", side_effect=fail_first_attempt,
        ), patch.object(pdf_rag_service, "fail_job") as fail, patch.object(
            pdf_rag_service.time, "sleep", side_effect=replace_during_backoff,
        ):
            with self.assertLogs(pdf_rag_service.logger, level="ERROR"):
                with self.assertRaises(JobLeaseLostError):
                    pdf_rag_service.process_ingestion_job(1, storage_dir=self.root)
        self.assertEqual(len(attempts), 1)
        fail.assert_not_called()
        self.assertIsNotNone(replacement[0])
        real_complete(1, replacement[0].worker_id, replacement[0].lease_token,
                      [IngestionChunk(1, "device manual", [0.1], pdf_rag_service.EMBEDDING_MODEL)])
        job, document, chunks = self.states()
        self.assertEqual((job["status"], document["status"], chunks),
                         ("succeeded", "review", 1))

    def test_chunk_insert_error_rolls_back_without_business_failure(self) -> None:
        with database.get_connection() as conn:
            conn.execute(
                "CREATE TRIGGER reject_chunk BEFORE INSERT ON document_chunks "
                "BEGIN SELECT RAISE(ABORT, 'injected chunk failure'); END",
            )
        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"},
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1]), patch.object(
            pdf_rag_service, "fail_job",
        ) as fail:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected chunk failure"):
                pdf_rag_service.process_ingestion_job(1, storage_dir=self.root)
        job, document, chunks = self.states()
        self.assertEqual((job["status"], document["status"], chunks),
                         ("running", "processing", 0))
        fail.assert_not_called()

    def test_old_worker_cannot_overwrite_new_lease_after_embedding(self) -> None:
        replacement = []

        def replace_owner(_content: str) -> list[float]:
            with database.get_connection() as conn:
                conn.execute(
                    "UPDATE ingestion_jobs SET lease_expires_at = "
                    "CAST(strftime('%s', 'now') AS INTEGER) - 1 WHERE id = 1"
                )
            replacement.append(claim_job("worker-b", job_id=1))
            return [0.1, 0.2]

        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"}
        ]), patch.object(pdf_rag_service, "get_embedding", side_effect=replace_owner):
            with self.assertLogs(pdf_rag_service.logger, level="ERROR"):
                with self.assertRaises(JobLeaseLostError):
                    pdf_rag_service.process_ingestion_job(1, storage_dir=self.root)
        self.assertIsNotNone(replacement[0])
        job, document, chunks = self.states()
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["worker_id"], "worker-b")
        self.assertEqual(job["lease_token"], replacement[0].lease_token)
        self.assertEqual((document["status"], chunks), ("processing", 0))

    def test_background_runner_uses_existing_entry_and_real_job_service(self) -> None:
        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"}
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1, 0.2]):
            ingestion_runner.run_background_ingestion(1, str(self.root))
        job, document, chunks = self.states()
        self.assertEqual((job["status"], document["status"], chunks),
                         ("succeeded", "review", 1))

    def test_long_running_worker_renews_its_lease(self) -> None:
        renewed = Event()
        real_renew = pdf_rag_service.renew_lease

        def observe_renewal(*args: object) -> int:
            expiry = real_renew(*args)
            renewed.set()
            return expiry

        def slow_embedding(_content: str) -> list[float]:
            self.assertTrue(renewed.wait(timeout=3), "Worker did not renew its lease")
            return [0.1, 0.2]

        with patch.object(pdf_rag_service, "DEFAULT_LEASE_SECONDS", 1), patch.object(
            pdf_rag_service, "extract_pdf_text", return_value=[
                {"page": 1, "text": "device manual"}
            ]
        ), patch.object(pdf_rag_service, "get_embedding", side_effect=slow_embedding), patch.object(
            pdf_rag_service, "renew_lease", side_effect=observe_renewal
        ):
            result = pdf_rag_service.process_ingestion_job(1, storage_dir=self.root)
        self.assertEqual(result["status"], "review")
        self.assertTrue(renewed.is_set())

    def test_polling_worker_claims_and_processes_without_double_claim(self) -> None:
        with patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"}
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1, 0.2]):
            self.assertEqual(pdf_rag_service.process_next_queued_job(storage_dir=self.root), 1)
        job, document, chunks = self.states()
        self.assertEqual((job["status"], job["attempt_count"]), ("succeeded", 1))
        self.assertEqual((document["status"], chunks), ("review", 1))
        self.assertIsNone(pdf_rag_service.process_next_queued_job(storage_dir=self.root))

    def test_manual_recovery_cannot_steal_live_lease(self) -> None:
        claimed = claim_job("worker-a", job_id=1)
        self.assertIsNotNone(claimed)
        with self.assertRaises(ValueError):
            pdf_rag_service.recover_stale_running_job(
                1, min_age_seconds=300, confirmed_old_workers_stopped=True,
            )
        job, _, _ = self.states()
        self.assertEqual(job["lease_token"], claimed.lease_token)


if __name__ == "__main__":
    unittest.main()
