"""Offline, real-PDF demonstration of the complete pilot HTTP workflow."""

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
import pdf_rag_service
from pdf_service import extract_pdf_text


DEMO_PDF = Path(__file__).resolve().parent / "demo" / "assets" / "asteredge_x100_guide.pdf"


def request(method: str, path: str, *, key: str | None = None,
            payload: dict | None = None, body: bytes | None = None,
            query: dict | None = None, content_type: str | None = None) -> tuple[int, dict]:
    """Call the actual ASGI app without an external server or HTTP dependency."""
    body = json.dumps(payload).encode() if payload is not None else (body or b"")
    headers = []
    if key is not None:
        headers.append((b"x-api-key", key.encode()))
    if payload is not None:
        headers.append((b"content-type", b"application/json"))
    elif content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    query_string = urlencode(query or {}, doseq=True).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": query_string, "root_path": "",
        "headers": headers, "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
    }
    messages = []

    async def run() -> None:
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await main.app(scope, receive, send)

    asyncio.run(run())
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    response_body = b"".join(item.get("body", b"") for item in messages
                             if item["type"] == "http.response.body")
    return status, json.loads(response_body)


class DemoFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = Path(temporary.name) / "uploads"
        db_patch = patch.object(database, "DATABASE_NAME", Path(temporary.name) / "pilot.db")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        self.admin_a = "a" * 32
        self.engineer_a = "e" * 32
        self.admin_b = "b" * 32
        keys = {
            self.admin_a: {"organization_id": 1, "role": "admin"},
            self.engineer_a: {"organization_id": 1, "role": "engineer"},
            self.admin_b: {"organization_id": 2, "role": "admin"},
        }
        env_patch = patch.dict(os.environ, {
            "PILOT_API_KEYS": json.dumps(keys),
            "DOCUMENT_STORAGE_PATH": str(self.storage),
            "PDF_INGESTION_MODE": "worker",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)

        async def start() -> None:
            async with main.lifespan(main.app):
                pass

        asyncio.run(start())

    def test_real_pdf_to_published_cited_answer_and_tenant_isolation(self) -> None:
        self.assertEqual(request("GET", "/health")[0], 200)
        self.assertEqual(request("POST", "/v1/organization", key=self.admin_a,
                                 payload={"name": "Demo Enterprise A"})[0], 201)
        self.assertEqual(request("POST", "/v1/organization", key=self.admin_b,
                                 payload={"name": "Demo Enterprise B"})[0], 201)
        status, model_a = request("POST", "/v1/device-models", key=self.admin_a,
                                  payload={"model_code": "X100", "name": "Demo sensor"})
        self.assertEqual(status, 201)
        status, model_b = request("POST", "/v1/device-models", key=self.admin_b,
                                  payload={"model_code": "X100", "name": "Other tenant sensor"})
        self.assertEqual(status, 201)
        pdf_bytes = DEMO_PDF.read_bytes()
        self.assertIn("Fault code E-17", extract_pdf_text(pdf_bytes)[0]["text"])
        status, upload = request(
            "POST", "/v1/documents", key=self.admin_a, body=pdf_bytes,
            content_type="application/pdf",
            query={"family_key": "asteredge-x100", "version": "1.0",
                   "title": "AsterEdge X100 Demo Guide",
                   "device_model_ids": [model_a["id"]]},
        )
        self.assertEqual((status, upload["status"]), (202, "queued"))
        document_id, job_id = upload["document_id"], upload["job_id"]
        self.assertEqual(request("GET", f"/v1/ingestion-jobs/{job_id}", key=self.admin_a)[1]["status"], "queued")
        self.assertEqual(request("GET", f"/v1/documents/{document_id}", key=self.admin_b)[0], 404)
        self.assertEqual(request("GET", f"/v1/ingestion-jobs/{job_id}", key=self.admin_b)[0], 404)
        self.assertEqual(request("POST", f"/v1/documents/{document_id}/publish",
                                 key=self.engineer_a)[0], 403)

        def embed(_text: str) -> list[float]:
            return [1.0, 0.0, 0.0]

        question = {"device_model_id": model_a["id"],
                    "question": "What should I check for fault code E-17?"}
        with patch.object(pdf_rag_service, "get_embedding", side_effect=embed):
            before = request("POST", "/v1/answers", key=self.engineer_a, payload=question)
            self.assertEqual((before[0], before[1]["status"]), (200, "insufficient_evidence"))
            self.assertEqual(before[1]["references"], [])

            self.assertEqual(pdf_rag_service.process_next_queued_job(storage_dir=self.storage), job_id)
            status, job = request("GET", f"/v1/ingestion-jobs/{job_id}", key=self.admin_a)
            self.assertEqual((status, job["status"]), (200, "succeeded"))
            status, detail = request("GET", f"/v1/documents/{document_id}", key=self.engineer_a)
            self.assertEqual((status, detail["status"]), (200, "review"))
            self.assertGreater(detail["chunks"], 0)
            self.assertEqual(request("POST", "/v1/answers", key=self.engineer_a,
                                     payload=question)[1]["status"], "insufficient_evidence")
            self.assertEqual(request("POST", f"/v1/documents/{document_id}/publish",
                                     key=self.admin_b)[0], 404)

            status, published = request("POST", f"/v1/documents/{document_id}/publish",
                                        key=self.admin_a)
            self.assertEqual((status, published["status"]), (200, "published"))
            with patch("pdf_chat_service.chat_completion",
                       return_value="Check the 12 V DC supply and cable [资料1].") as chat:
                status, answer = request("POST", "/v1/answers", key=self.engineer_a,
                                         payload=question)
            self.assertEqual((status, answer["status"]), (200, "answered"))
            self.assertEqual(len(answer["references"]), 1)
            self.assertEqual(answer["references"][0]["document_id"], document_id)
            self.assertEqual(answer["references"][0]["page"], 1)
            self.assertIn("12 V DC", answer["references"][0]["content"])
            chat.assert_called_once()
            self.assertEqual(request("POST", "/v1/answers", key=self.admin_b,
                                     payload={"device_model_id": model_a["id"],
                                              "question": question["question"]})[0], 404)
            status, other_answer = request("POST", "/v1/answers", key=self.admin_b,
                                           payload={"device_model_id": model_b["id"],
                                                    "question": question["question"]})
            self.assertEqual((status, other_answer["status"], other_answer["references"]),
                             (200, "insufficient_evidence", []))

        with database.get_connection() as conn:
            row = conn.execute("SELECT status, completed_lease_token FROM ingestion_jobs WHERE id = ?",
                               (job_id,)).fetchone()
            self.assertEqual(row["status"], "succeeded")
            self.assertTrue(row["completed_lease_token"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id = ?",
                                          (document_id,)).fetchone()[0], detail["chunks"])


if __name__ == "__main__":
    unittest.main()
