"""HTTP contract checks for the migrated versioned document upload."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

import database
import main
from app.api.routes import documents as document_routes
from app.services import ingestion_runner
from app.core.factory import create_app


class UploadRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.storage_dir = Path(temporary_directory.name) / "storage"
        database_patch = patch.object(
            database, "DATABASE_NAME", Path(temporary_directory.name) / "test.db"
        )
        database_patch.start()
        self.addCleanup(database_patch.stop)
        keys = {
            "a" * 32: {"organization_id": 1, "role": "admin"},
            "e" * 32: {"organization_id": 1, "role": "engineer"},
            "b" * 32: {"organization_id": 2, "role": "admin"},
        }
        environment_patch = patch.dict(os.environ, {
            "PILOT_API_KEYS": json.dumps(keys),
            "DOCUMENT_STORAGE_PATH": str(self.storage_dir),
            "PDF_INGESTION_MODE": "worker",
        })
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
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

    def upload(
        self,
        application,
        *,
        api_key: str | None = "a" * 32,
        pdf_bytes: bytes = b"%PDF-test",
        family_key: str = "guide",
        device_model_ids: tuple[int, ...] = (10,),
        content_type: str = "application/pdf",
    ) -> tuple[int, dict]:
        query = urlencode({
            "family_key": family_key,
            "version": "1.0",
            "title": "设备手册",
            "device_model_ids": device_model_ids,
        }, doseq=True).encode()
        headers = [(b"content-type", content_type.encode())]
        if api_key is not None:
            headers.append((b"x-api-key", api_key.encode()))
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/v1/documents",
            "raw_path": b"/v1/documents", "query_string": query, "root_path": "",
            "headers": headers, "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": pdf_bytes, "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(application(scope, receive, send))
        status = next(
            message["status"] for message in messages
            if message["type"] == "http.response.start"
        )
        body = b"".join(
            message.get("body", b"") for message in messages
            if message["type"] == "http.response.body"
        )
        return status, json.loads(body)

    def test_success_duplicate_and_permissions_on_both_entry_points(self) -> None:
        for index, application in enumerate((main.app, create_app())):
            with self.subTest(application=index):
                family_key = f"guide-{index}"
                status, result = self.upload(application, family_key=family_key)
                self.assertEqual(status, 202)
                self.assertEqual(set(result), {"document_id", "job_id", "status"})
                self.assertEqual(result["status"], "queued")
                with database.get_connection() as conn:
                    document = conn.execute(
                        "SELECT organization_id, status, storage_key FROM documents WHERE id = ?",
                        (result["document_id"],),
                    ).fetchone()
                    job = conn.execute(
                        "SELECT status FROM ingestion_jobs WHERE id = ?",
                        (result["job_id"],),
                    ).fetchone()
                self.assertEqual((document["organization_id"], document["status"]), (1, "processing"))
                self.assertEqual(job["status"], "queued")
                self.assertEqual(
                    (self.storage_dir / document["storage_key"]).read_bytes(), b"%PDF-test"
                )

                status, body = self.upload(application, family_key=family_key)
                self.assertEqual((status, body["detail"]), (400, "该企业的同一文档版本已存在"))
                self.assertEqual(self.upload(application, api_key="x" * 32)[0], 401)
                self.assertEqual(self.upload(application, api_key=None)[0], 401)
                self.assertEqual(self.upload(application, api_key="e" * 32)[0], 403)
                status, body = self.upload(application, device_model_ids=(20,))
                self.assertEqual((status, body["detail"]), (400, "设备型号不属于该企业"))

    def test_invalid_pdf_oversize_and_media_type(self) -> None:
        self.assertEqual(self.upload(main.app, pdf_bytes=b"not-a-pdf")[0], 400)
        self.assertEqual(self.upload(main.app, content_type="text/plain")[0], 415)
        self.assertEqual(self.upload(main.app, device_model_ids=())[0], 422)
        with patch.object(document_routes, "MAX_PDF_BYTES", 8):
            self.assertEqual(self.upload(main.app, pdf_bytes=b"%PDF-xxxx")[0], 413)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    def test_background_mode_still_dispatches_existing_worker(self) -> None:
        with patch.dict(os.environ, {"PDF_INGESTION_MODE": "background"}):
            with patch.object(ingestion_runner, "process_ingestion_job") as process:
                status, result = self.upload(main.app)
        self.assertEqual(status, 202)
        process.assert_called_once_with(result["job_id"], storage_dir=str(self.storage_dir))


if __name__ == "__main__":
    unittest.main()
