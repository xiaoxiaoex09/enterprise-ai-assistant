"""Contract checks for the versioned upload preparation service."""

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
from app.services.ingestion_service import create_ingestion_job
from pdf_rag_service import queue_versioned_pdf


class IngestionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.storage_dir = Path(temporary_directory.name) / "storage"
        database_path = Path(temporary_directory.name) / "test.db"
        database_patch = patch.object(database, "DATABASE_NAME", database_path)
        database_patch.start()
        self.addCleanup(database_patch.stop)
        database.init_db()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES (1, '企业甲')")
            conn.execute("INSERT INTO organizations (id, name) VALUES (2, '企业乙')")
            conn.execute(
                "INSERT INTO device_models (id, organization_id, model_code, name) "
                "VALUES (10, 1, 'A', '设备A')"
            )
            conn.execute(
                "INSERT INTO device_models (id, organization_id, model_code, name) "
                "VALUES (20, 2, 'B', '设备B')"
            )

    def queue(self, **overrides):
        arguments = {
            "organization_id": 1,
            "device_model_ids": [10],
            "family_key": "guide",
            "version": "1.0",
            "title": "设备手册",
            "storage_dir": self.storage_dir,
        }
        arguments.update(overrides)
        return queue_versioned_pdf(b"%PDF-test", **arguments)

    def test_legacy_entry_point_creates_file_document_and_job(self) -> None:
        result = self.queue(device_model_ids=[10, 10])
        self.assertEqual(result["status"], "queued")
        with database.get_connection() as conn:
            document = conn.execute(
                "SELECT organization_id, family_key, version, status, storage_key, sha256 "
                "FROM documents WHERE id = ?", (result["document_id"],)
            ).fetchone()
            model_ids = conn.execute(
                "SELECT device_model_id FROM document_models WHERE document_id = ?",
                (result["document_id"],),
            ).fetchall()
            job = conn.execute(
                "SELECT document_id, status FROM ingestion_jobs WHERE id = ?",
                (result["job_id"],),
            ).fetchone()
        self.assertEqual(tuple(document)[:4], (1, "guide", "1.0", "processing"))
        self.assertEqual(document["sha256"], hashlib.sha256(b"%PDF-test").hexdigest())
        self.assertEqual((self.storage_dir / document["storage_key"]).read_bytes(), b"%PDF-test")
        self.assertEqual([row[0] for row in model_ids], [10])
        self.assertEqual(tuple(job), (result["document_id"], "queued"))

    def test_rejects_invalid_pdf_and_cross_organization_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "上传内容不是有效的PDF"):
            create_ingestion_job(
                b"invalid", organization_id=1, device_model_ids=[10],
                family_key="guide", version="1.0", title="设备手册",
                storage_dir=self.storage_dir,
            )
        with self.assertRaisesRegex(ValueError, "设备型号不属于该企业"):
            self.queue(device_model_ids=[20])
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
        self.assertFalse(self.storage_dir.exists())

    def test_duplicate_version_keeps_original_and_cleans_new_file(self) -> None:
        original = self.queue()
        with self.assertRaisesRegex(ValueError, "该企业的同一文档版本已存在"):
            self.queue()
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ingestion_jobs").fetchone()[0], 1)
            original_key = conn.execute(
                "SELECT storage_key FROM documents WHERE id = ?",
                (original["document_id"],),
            ).fetchone()[0]
        self.assertEqual(
            list(self.storage_dir.rglob("*.pdf")),
            [self.storage_dir / original_key],
        )

    def test_interrupted_file_write_removes_partial_temp_file(self) -> None:
        original_open = Path.open

        def interrupted_open(path, mode="r", *args, **kwargs):
            handle = original_open(path, mode, *args, **kwargs)
            if mode != "xb":
                return handle

            class InterruptedFile:
                def __enter__(self):
                    handle.__enter__()
                    return self

                def __exit__(self, *exception):
                    return handle.__exit__(*exception)

                def write(self, content):
                    handle.write(content[:4])
                    raise OSError("injected partial write")

            return InterruptedFile()

        with patch.object(Path, "open", interrupted_open):
            with self.assertRaisesRegex(OSError, "injected partial write"):
                self.queue()
        self.assertEqual(list(self.storage_dir.rglob("*.pdf*")), [])
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ingestion_jobs").fetchone()[0], 0)

    def test_organization_directory_symlink_cannot_escape_storage(self) -> None:
        self.storage_dir.mkdir()
        outside = self.storage_dir.parent / "outside"
        outside.mkdir()
        try:
            (self.storage_dir / "1").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Directory symlinks unavailable: {error}")

        with self.assertRaisesRegex(ValueError, "文档存储路径无效"):
            self.queue()
        self.assertEqual(list(outside.iterdir()), [])
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    def test_escaped_storage_key_is_rejected_before_file_write(self) -> None:
        with patch("app.services.ingestion_service.uuid4") as generated_id:
            generated_id.return_value.hex = "../../escape"
            with self.assertRaisesRegex(ValueError, "文档存储路径无效"):
                self.queue()
        self.assertFalse((self.storage_dir.parent / "escape.pdf").exists())
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    def test_nonduplicate_integrity_error_rolls_back_and_preserves_cause(self) -> None:
        with database.get_connection() as conn:
            conn.execute(
                "CREATE TRIGGER fail_job BEFORE INSERT ON ingestion_jobs "
                "BEGIN SELECT RAISE(ABORT, 'injected job failure'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected job failure"):
            self.queue()
        with database.get_connection() as conn:
            for table in ("documents", "document_models", "ingestion_jobs"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(list(self.storage_dir.rglob("*.pdf*")), [])

    def test_atomic_replace_failure_removes_temp_file(self) -> None:
        with patch("app.services.ingestion_service.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.queue()
        self.assertEqual(list(self.storage_dir.rglob("*.pdf*")), [])


if __name__ == "__main__":
    unittest.main()
