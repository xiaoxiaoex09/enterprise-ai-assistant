"""Fault-injection tests for polling recovery and bounded SQLite backoff."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import database
import pdf_rag_service
from app.services.ingestion_job_service import RecoveryResult


class PollingResilienceTests(unittest.TestCase):
    def test_recovery_database_error_is_retried(self) -> None:
        with patch.object(pdf_rag_service, "recover_expired_jobs", side_effect=[
            sqlite3.OperationalError("database is locked"),
            RecoveryResult((), ()),
            KeyboardInterrupt(),
        ]) as recover, patch.object(
            pdf_rag_service, "claim_job", return_value=None
        ) as claim, patch.object(pdf_rag_service.time, "sleep") as sleep:
            with self.assertLogs(pdf_rag_service.logger, level="WARNING") as logs:
                with self.assertRaises(KeyboardInterrupt):
                    pdf_rag_service.run_ingestion_worker(storage_dir="unused", poll_seconds=0.2)
        self.assertEqual(recover.call_count, 3)
        self.assertEqual(claim.call_count, 1)
        self.assertEqual(sleep.call_args_list, [call(1.0), call(0.2)])
        self.assertTrue(any("stage=recover" in entry for entry in logs.output))

    def test_claim_database_error_is_retried(self) -> None:
        with patch.object(
            pdf_rag_service, "recover_expired_jobs", return_value=RecoveryResult((), ())
        ) as recover, patch.object(pdf_rag_service, "claim_job", side_effect=[
            sqlite3.OperationalError("database is locked"), None, KeyboardInterrupt(),
        ]) as claim, patch.object(pdf_rag_service.time, "sleep") as sleep:
            with self.assertLogs(pdf_rag_service.logger, level="WARNING") as logs:
                with self.assertRaises(KeyboardInterrupt):
                    pdf_rag_service.run_ingestion_worker(storage_dir="unused", poll_seconds=0.2)
        self.assertEqual(recover.call_count, 3)
        self.assertEqual(claim.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1.0), call(0.2)])
        self.assertTrue(any("stage=claim" in entry for entry in logs.output))

    def test_initialization_database_error_is_retried(self) -> None:
        with patch.object(pdf_rag_service, "init_db", side_effect=[
            sqlite3.OperationalError("database is locked"), None,
        ]) as initialize, patch.object(
            pdf_rag_service, "process_next_queued_job",
            side_effect=[None, KeyboardInterrupt()],
        ) as poll, patch.object(pdf_rag_service.time, "sleep") as sleep:
            with self.assertLogs(pdf_rag_service.logger, level="WARNING") as logs:
                with self.assertRaises(KeyboardInterrupt):
                    pdf_rag_service.run_ingestion_worker(
                        storage_dir="unused", poll_seconds=0.2, initialize=True,
                    )
        self.assertEqual(initialize.call_count, 2)
        self.assertEqual(poll.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(1.0), call(0.2)])
        self.assertTrue(any("stage=initialize" in entry for entry in logs.output))

    def test_backoff_is_bounded_and_resets_after_success(self) -> None:
        transient = sqlite3.OperationalError("database is locked")
        outcomes = [transient] * 3 + [None, transient, KeyboardInterrupt()]
        with patch.object(pdf_rag_service, "process_next_queued_job", side_effect=outcomes), patch.object(
            pdf_rag_service.time, "sleep"
        ) as sleep:
            with self.assertLogs(pdf_rag_service.logger, level="WARNING"):
                with self.assertRaises(KeyboardInterrupt):
                    pdf_rag_service.run_ingestion_worker(storage_dir="unused", poll_seconds=0.2)
        self.assertEqual(
            sleep.call_args_list,
            [call(1.0), call(2.0), call(4.0), call(0.2), call(1.0)],
        )

    def test_retry_budget_exhaustion_exposes_io_error(self) -> None:
        io_error = sqlite3.OperationalError("disk I/O error")
        io_error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        with patch.object(
            pdf_rag_service, "process_next_queued_job", side_effect=io_error,
        ) as poll, patch.object(pdf_rag_service.time, "sleep") as sleep:
            with self.assertLogs(pdf_rag_service.logger, level="ERROR"):
                with self.assertRaises(sqlite3.OperationalError):
                    pdf_rag_service.run_ingestion_worker(storage_dir="unused")
        self.assertEqual(poll.call_count, pdf_rag_service.DB_RETRY_MAX_FAILURES)
        self.assertEqual(sleep.call_args_list, [call(1.0), call(2.0), call(4.0), call(8.0)])

    def test_missing_database_parent_exposes_real_cantopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_db = Path(directory) / "missing-parent" / "jobs.db"
            with patch.object(database, "DATABASE_NAME", missing_db), patch.object(
                pdf_rag_service.time, "sleep",
            ) as sleep:
                with self.assertLogs(pdf_rag_service.logger, level="ERROR"):
                    with self.assertRaises(sqlite3.OperationalError) as raised:
                        pdf_rag_service.run_ingestion_worker(
                            storage_dir=directory, initialize=True,
                        )
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_CANTOPEN)
            self.assertFalse(missing_db.parent.exists())
            sleep.assert_not_called()

    def test_non_retryable_database_error_is_not_hidden(self) -> None:
        with patch.object(
            pdf_rag_service, "process_next_queued_job",
            side_effect=sqlite3.OperationalError("no such table: ingestion_jobs"),
        ), patch.object(pdf_rag_service.time, "sleep") as sleep:
            with self.assertLogs(pdf_rag_service.logger, level="ERROR"):
                with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                    pdf_rag_service.run_ingestion_worker(storage_dir="unused")
        sleep.assert_not_called()


class RealPollingContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        database_patch = patch.object(database, "DATABASE_NAME", self.root / "jobs.db")
        database_patch.start()
        self.addCleanup(database_patch.stop)
        database.init_db()
        (self.root / "manual.pdf").write_bytes(b"%PDF-test")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES (1, 'test')")
            for identifier in (1, 2):
                conn.execute(
                    "INSERT INTO documents "
                    "(id, organization_id, family_key, title, document_type, version, "
                    "status, storage_key) "
                    "VALUES (?, 1, ?, 'Manual', 'manual', '1', 'processing', 'manual.pdf')",
                    (identifier, str(identifier)),
                )
                conn.execute(
                    "INSERT INTO ingestion_jobs (id, document_id) VALUES (?, ?)",
                    (identifier, identifier),
                )

    def test_one_failed_job_does_not_stop_following_job(self) -> None:
        with patch.object(pdf_rag_service, "extract_pdf_text", side_effect=[
            ValueError("broken PDF"), [{"page": 1, "text": "device manual"}],
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1]), patch.object(
            pdf_rag_service.time, "sleep", side_effect=KeyboardInterrupt()
        ):
            with self.assertLogs(pdf_rag_service.logger, level="ERROR"):
                with self.assertRaises(KeyboardInterrupt):
                    pdf_rag_service.run_ingestion_worker(
                        storage_dir=self.root, poll_seconds=0.2,
                    )
        with database.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, status FROM ingestion_jobs ORDER BY id"
            ).fetchall()
            documents = conn.execute(
                "SELECT id, status FROM documents ORDER BY id"
            ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1, "failed"), (2, "succeeded")])
        self.assertEqual([tuple(row) for row in documents], [(1, "failed"), (2, "review")])

    def test_real_busy_lock_then_worker_processes_queued_jobs(self) -> None:
        real_connect = sqlite3.connect
        lock = real_connect(self.root / "jobs.db", timeout=0)
        self.addCleanup(lock.close)
        lock.execute("BEGIN IMMEDIATE")
        delays = []

        def fast_connection(*args, **kwargs):
            kwargs["timeout"] = 0
            return real_connect(*args, **kwargs)

        def release_lock_then_stop(delay):
            delays.append(delay)
            if len(delays) == 1:
                lock.rollback()
            else:
                raise KeyboardInterrupt()

        with patch.object(database.sqlite3, "connect", side_effect=fast_connection), patch.object(
            pdf_rag_service.time, "sleep", side_effect=release_lock_then_stop,
        ), patch.object(pdf_rag_service, "extract_pdf_text", return_value=[
            {"page": 1, "text": "device manual"},
        ]), patch.object(pdf_rag_service, "get_embedding", return_value=[0.1]):
            with self.assertLogs(pdf_rag_service.logger, level="WARNING"):
                with self.assertRaises(KeyboardInterrupt):
                    pdf_rag_service.run_ingestion_worker(
                        storage_dir=self.root, poll_seconds=0.2,
                    )
        self.assertEqual(delays, [1.0, 0.2])
        with database.get_connection() as conn:
            states = [tuple(row) for row in conn.execute(
                "SELECT id, status FROM ingestion_jobs ORDER BY id",
            )]
        self.assertEqual(states, [(1, "succeeded"), (2, "succeeded")])


if __name__ == "__main__":
    unittest.main()
