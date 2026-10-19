"""Dry-run, guarded apply, skip rules, and rollback for legacy jobs."""

import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import database
import recover_legacy_ingestion_jobs as legacy


class LegacyRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database_path = Path(temporary_directory.name) / "legacy.db"
        database_patch = patch.object(database, "DATABASE_NAME", self.database_path)
        database_patch.start()
        self.addCleanup(database_patch.stop)
        database.init_db()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES (1, 'test')")
        self.add_job(1, status="running", started_at="old", attempt_count=1)
        self.add_job(2, status="running", started_at="recent")
        self.add_job(3, status="running", started_at="old", worker_id="partial")
        self.add_job(4, status="running", started_at="old", worker_id="live",
                     lease_token="live-token", lease_expires_at=9999999999)
        self.add_job(5, status="running", started_at="old", document_status="review")
        self.add_job(6, status="running", started_at="old")
        self.add_job(7, status="running", started_at="old", attempt_count=3)
        self.add_job(8, status="succeeded", started_at="old", document_status="review")
        self.add_job(9, status="running", started_at="old", storage_key=None)
        self.add_job(10, status="running", started_at="old", document_status="failed")
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO document_chunks "
                "(document_id, chunk_index, page, content, embedding, embedding_model) "
                "VALUES (6, 0, 1, 'old', '[0.1]', 'test')"
            )

    def add_job(
        self, identifier: int, *, status: str, started_at: str,
        document_status: str = "processing", attempt_count: int = 0,
        worker_id: str | None = None, lease_token: str | None = None,
        lease_expires_at: int | None = None, storage_key: str | None = "manual.pdf",
    ) -> None:
        start_expression = (
            "datetime('now', '-2 hours')" if started_at == "old"
            else "datetime('now', '-1 minute')"
        )
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO documents "
                "(id, organization_id, family_key, title, document_type, version, "
                "status, storage_key) VALUES (?, 1, ?, 'Manual', 'manual', '1', ?, ?)",
                (identifier, str(identifier), document_status, storage_key),
            )
            conn.execute(
                "INSERT INTO ingestion_jobs "
                "(id, document_id, status, started_at, worker_id, lease_token, "
                "lease_expires_at, attempt_count) "
                f"VALUES (?, ?, ?, {start_expression}, ?, ?, ?, ?)",
                (identifier, identifier, status, worker_id, lease_token,
                 lease_expires_at, attempt_count),
            )

    def statuses(self) -> dict[int, tuple[str, int]]:
        with database.get_connection() as conn:
            return {
                row["id"]: (row["status"], row["attempt_count"])
                for row in conn.execute(
                    "SELECT id, status, attempt_count FROM ingestion_jobs"
                )
            }

    def test_dry_run_is_read_only_and_reports_skip_reasons(self) -> None:
        before = self.statuses()
        report = legacy.inspect_legacy_jobs()
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual((report["identified"], report["would_requeue"],
                          report["requeued"], report["skipped"]), (8, 1, 0, 7))
        self.assertEqual(report["skip_reasons"], {
            "not_old_enough": 1,
            "partial_lease_fields": 1,
            "document_not_processing": 2,
            "document_has_chunks": 1,
            "attempt_limit_reached": 1,
            "missing_pdf_reference": 1,
        })
        self.assertEqual(self.statuses(), before)
        self.assertEqual(next(item for item in report["jobs"] if item["job_id"] == 1)[
            "attempt_count"], 1)

    def test_apply_requires_explicit_confirmation(self) -> None:
        before = self.statuses()
        with self.assertRaisesRegex(ValueError, "confirm-old-workers-stopped"):
            legacy.inspect_legacy_jobs(apply=True)
        self.assertEqual(self.statuses(), before)
        with patch.object(sys, "argv", ["recover_legacy_ingestion_jobs.py", "--apply"]):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exit_result:
                    legacy.main()
        self.assertEqual(exit_result.exception.code, 2)
        self.assertEqual(self.statuses(), before)

    def test_old_recovery_entry_requires_confirmation_and_uses_safe_filter(self) -> None:
        from pdf_rag_service import recover_stale_running_job

        with self.assertRaisesRegex(ValueError, "confirm-old-workers-stopped"):
            recover_stale_running_job(1)
        recover_stale_running_job(1, confirmed_old_workers_stopped=True)
        self.assertEqual(self.statuses()[1], ("queued", 1))
        self.assertEqual(self.statuses()[4], ("running", 0))

    def test_apply_only_requeues_eligible_job(self) -> None:
        before = self.statuses()
        report = legacy.inspect_legacy_jobs(
            apply=True, confirmed_old_workers_stopped=True,
        )
        self.assertEqual((report["requeued"], report["skipped"]), (1, 7))
        after = self.statuses()
        self.assertEqual(after[1], ("queued", 1))
        for identifier in range(2, 11):
            self.assertEqual(after[identifier], before[identifier])
        with database.get_connection() as conn:
            live = conn.execute(
                "SELECT worker_id, lease_token, lease_expires_at "
                "FROM ingestion_jobs WHERE id = 4"
            ).fetchone()
            document = conn.execute(
                "SELECT status FROM documents WHERE id = 1"
            ).fetchone()[0]
            chunks = conn.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE document_id = 6"
            ).fetchone()[0]
        self.assertEqual(tuple(live), ("live", "live-token", 9999999999))
        self.assertEqual(document, "processing")
        self.assertEqual(chunks, 1)

    def test_apply_rolls_back_all_jobs_if_later_update_fails(self) -> None:
        self.add_job(11, status="running", started_at="old")
        with database.get_connection() as conn:
            conn.execute(
                "CREATE TRIGGER reject_legacy_recovery BEFORE UPDATE OF status "
                "ON ingestion_jobs WHEN OLD.id = 11 "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            legacy.inspect_legacy_jobs(
                apply=True, confirmed_old_workers_stopped=True,
            )
        self.assertEqual(self.statuses()[1], ("running", 1))
        self.assertEqual(self.statuses()[11], ("running", 0))


if __name__ == "__main__":
    unittest.main()
