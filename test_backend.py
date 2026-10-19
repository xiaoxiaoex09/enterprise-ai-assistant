"""Offline checks for the ticket API and database migration."""

import asyncio
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi import HTTPException, Request, Response

import database
import deepseek_client
import main
import pdf_rag_service
import pdf_service
from app.services import document_service
from app.api.routes.health import health_check
from app.api.routes.documents import (
    get_pilot_document,
    publish_pilot_document,
    upload_pilot_document,
)
from app.api.schemas.document import PilotDocumentQueueResponse, PilotDocumentResponse
from app.core.auth import PilotIdentity, get_pilot_admin, get_pilot_identity
from app.core.factory import (
    close_legacy_routes_in_pilot_mode,
    create_app,
    lifespan as factory_lifespan,
)
from app.main import app as package_app, lifespan as package_lifespan


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        database_path = Path(self.temp_dir.name) / "tickets.db"
        self.path_patch = patch.object(database, "DATABASE_NAME", database_path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_startup_and_ticket_flow(self):
        async def start():
            async with main.lifespan(main.app):
                pass

        asyncio.run(start())
        self.assertEqual(main.get_tickets(), [])
        created = main.create_ticket(
            main.TicketCreate(customer_name="客户", device_name="ESP32", problem="无法连接")
        )
        self.assertEqual(created["status"], "待处理")
        self.assertEqual(main.get_ticket(created["id"])["problem"], "无法连接")
        self.assertEqual(main.get_tickets()[0]["id"], created["id"])
        with patch.object(main, "analyze_ticket_with_ai", return_value="检查网络配置"):
            main.analyze_ticket(created["id"])
        analyzed = main.get_ticket(created["id"])
        self.assertEqual(analyzed["ai_analysis"], "检查网络配置")
        self.assertIsNone(analyzed["priority"])
        self.assertIsNone(analyzed["category"])
        with self.assertRaises(HTTPException) as error:
            main.get_ticket(created["id"] + 1)
        self.assertEqual(error.exception.status_code, 404)

    def test_old_schema_is_migrated_idempotently(self):
        with closing(sqlite3.connect(database.DATABASE_NAME)) as conn, conn:
            conn.execute(
                "CREATE TABLE tickets (id INTEGER PRIMARY KEY, customer_name TEXT, "
                "device_name TEXT, problem TEXT, status TEXT)"
            )
            conn.execute(
                "CREATE TABLE knowledge_chunks (id INTEGER PRIMARY KEY, source TEXT, "
                "page INTEGER, content TEXT, embedding TEXT, model TEXT)"
            )
            conn.execute(
                "INSERT INTO tickets VALUES (1, '原客户', 'STM32', '故障', '待处理')"
            )
        database.init_db()
        database.init_db()
        with database.get_connection() as conn:
            ticket_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
            chunk_columns = {row["name"] for row in conn.execute("PRAGMA table_info(knowledge_chunks)")}
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        self.assertTrue({"ai_analysis", "priority", "category"} <= ticket_columns)
        self.assertIn("doc_type", chunk_columns)

    def test_pdf_upload_passes_document_type(self):
        class FakeRequest:
            async def body(self):
                return b"%PDF-fake"

        expected = {"filename": "manual.pdf", "doc_type": "manual", "pages": 1, "chunks": 2}
        with patch.object(main, "ingest_pdf", return_value=expected) as ingest:
            result = asyncio.run(main.upload_pdf(FakeRequest(), "../manual.pdf", "manual"))
        ingest.assert_called_once_with(b"%PDF-fake", "manual.pdf", "manual")
        self.assertEqual(result["doc_type"], "manual")

    def test_pdf_parser_keeps_page_shape_and_legacy_import(self):
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "sample manual")
            pdf_bytes = document.tobytes()

        pages = pdf_service.extract_pdf_text(pdf_bytes)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["page"], 1)
        self.assertIn("sample manual", pages[0]["text"])
        self.assertIs(pdf_rag_service.extract_pdf_text, pdf_service.extract_pdf_text)

    def test_pdf_parser_reports_invalid_and_read_errors(self):
        for source in (b"not a PDF", b""):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "PDF无法解析"):
                    pdf_service.extract_pdf_text(source)
        with patch.object(pdf_service.pymupdf, "open", side_effect=OSError("read failed")):
            with self.assertRaisesRegex(ValueError, "PDF无法解析"):
                pdf_service.extract_pdf_text(b"%PDF-test")

    def test_routes_are_unique_and_model_errors_are_handled(self):
        routes = []
        for route in main.app.routes:
            included_routes = (
                route.original_router.routes
                if hasattr(route, "original_router") else [route]
            )
            routes.extend(
                (included.path, tuple(sorted(included.methods)))
                for included in included_routes
                if hasattr(included, "path") and hasattr(included, "methods")
            )
        self.assertEqual(len(routes), len(set(routes)))
        self.assertEqual(sum(len(methods) for methods in main.app.openapi()["paths"].values()), 19)
        self.assertEqual(set(main.app.openapi()["paths"]["/health"]), {"get"})
        self.assertEqual(
            set(main.app.openapi()["paths"]["/v1/documents/{document_id}/publish"]),
            {"post"},
        )
        self.assertIs(main.health_check, health_check)
        self.assertEqual(health_check(), {"status": "healthy"})
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
            with patch.object(deepseek_client.urllib.request, "urlopen", return_value=io.BytesIO(b"{}")):
                with self.assertRaisesRegex(RuntimeError, "不完整"):
                    deepseek_client.chat_completion([{"role": "user", "content": "测试"}])

    def test_factory_keeps_entry_points_on_the_shared_app(self):
        self.assertIs(main.app, package_app)
        self.assertIs(main.lifespan, factory_lifespan)
        self.assertIs(package_lifespan, factory_lifespan)
        self.assertIs(main.close_legacy_routes_in_pilot_mode, close_legacy_routes_in_pilot_mode)
        self.assertIs(main.PilotIdentity, PilotIdentity)
        self.assertIs(main.get_pilot_admin, get_pilot_admin)
        self.assertIs(main.get_pilot_identity, get_pilot_identity)
        self.assertIs(main.PilotDocumentResponse, PilotDocumentResponse)
        self.assertIs(main.get_pilot_document, get_pilot_document)
        self.assertIs(main.publish_pilot_document, publish_pilot_document)
        self.assertIs(main.upload_pilot_document, upload_pilot_document)
        self.assertIs(main.PilotDocumentQueueResponse, PilotDocumentQueueResponse)

        isolated_app = create_app()
        self.assertIsNot(isolated_app, package_app)
        self.assertEqual(
            set(isolated_app.openapi()["paths"]),
            {
                "/health", "/v1/documents", "/v1/documents/{document_id}",
                "/v1/documents/{document_id}/publish",
            },
        )
        self.assertEqual(len(isolated_app.user_middleware), 1)
        self.assertIs(
            isolated_app.user_middleware[0].kwargs["dispatch"],
            close_legacy_routes_in_pilot_mode,
        )

    def test_factory_pilot_middleware_preserves_legacy_access_rules(self):
        async def allow_request(_request: Request) -> Response:
            return Response(status_code=200)

        def request_for(path: str) -> Request:
            return Request({
                "type": "http", "method": "GET", "scheme": "http",
                "path": path, "root_path": "", "query_string": b"",
                "headers": [], "server": ("testserver", 80),
            })

        with patch.dict(os.environ, {"PILOT_API_KEYS": "{}"}):
            blocked = asyncio.run(close_legacy_routes_in_pilot_mode(
                request_for("/tickets"), allow_request,
            ))
            allowed = asyncio.run(close_legacy_routes_in_pilot_mode(
                request_for("/health"), allow_request,
            ))
            self.assertEqual(blocked.status_code, 404)
            self.assertEqual(allowed.status_code, 200)

    def test_migrated_document_detail_keeps_auth_and_tenant_isolation(self):
        database.init_db()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES (1, '企业甲')")
            conn.execute("INSERT INTO organizations (id, name) VALUES (2, '企业乙')")
            conn.execute(
                "INSERT INTO device_models (id, organization_id, model_code, name) "
                "VALUES (3, 1, 'ESP32', 'ESP32')"
            )
            conn.execute(
                "INSERT INTO documents "
                "(id, organization_id, family_key, title, document_type, version, status) "
                "VALUES (10, 1, 'guide', '设备手册', 'manual', '1.0', 'review')"
            )
            conn.execute(
                "INSERT INTO document_models "
                "(organization_id, document_id, device_model_id) VALUES (1, 10, 3)"
            )
            conn.execute(
                "INSERT INTO document_chunks "
                "(document_id, chunk_index, page, content, embedding, embedding_model) "
                "VALUES (10, 0, 1, '测试内容', '[]', 'test')"
            )

        async def request(application, document_id: int, api_key: str | None):
            path = f"/v1/documents/{document_id}"
            headers = [(b"x-api-key", api_key.encode())] if api_key else []
            messages = []
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": path,
                "raw_path": path.encode(), "query_string": b"", "root_path": "",
                "headers": headers, "client": ("127.0.0.1", 1234),
                "server": ("127.0.0.1", 8000),
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await application(scope, receive, send)
            status = next(item["status"] for item in messages if item["type"] == "http.response.start")
            response_body = b"".join(
                item.get("body", b"") for item in messages if item["type"] == "http.response.body"
            )
            return status, json.loads(response_body)

        first_key = "a" * 32
        second_key = "b" * 32
        keys = {
            first_key: {"organization_id": 1, "role": "engineer"},
            second_key: {"organization_id": 2, "role": "admin"},
        }
        with patch.dict(os.environ, {"PILOT_API_KEYS": json.dumps(keys)}):
            for application in (main.app, create_app()):
                with self.subTest(application=application):
                    status, detail = asyncio.run(request(application, 10, first_key))
                    self.assertEqual(status, 200)
                    self.assertEqual(detail["title"], "设备手册")
                    self.assertEqual(detail["device_model_ids"], [3])
                    self.assertEqual(detail["chunks"], 1)
                    self.assertEqual(asyncio.run(request(application, 10, None))[0], 401)
                    self.assertEqual(asyncio.run(request(application, 10, "x" * 32))[0], 401)
                    self.assertEqual(asyncio.run(request(application, 10, second_key))[0], 404)
                    self.assertEqual(asyncio.run(request(application, 99, first_key))[0], 404)

        with patch.dict(os.environ, {"PILOT_API_KEYS": "invalid-json"}):
            for application in (main.app, create_app()):
                self.assertEqual(asyncio.run(request(application, 10, first_key))[0], 503)

        published = main.publish_pilot_document(
            10, PilotIdentity(organization_id=1, role="admin")
        )
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["device_model_ids"], [3])

    def test_migrated_document_publish_keeps_permissions_and_state_rules(self):
        database.init_db()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES (1, '企业甲')")
            conn.execute("INSERT INTO organizations (id, name) VALUES (2, '企业乙')")
            conn.executemany(
                "INSERT INTO documents "
                "(id, organization_id, family_key, title, document_type, version, status) "
                "VALUES (?, ?, ?, ?, 'manual', ?, ?)",
                [
                    (10, 1, "guide", "新版手册", "2.0", "review"),
                    (11, 1, "guide", "旧版手册", "1.0", "published"),
                    (20, 2, "other", "乙方手册", "1.0", "review"),
                    (30, 1, "empty", "空手册", "1.0", "review"),
                ],
            )
            conn.executemany(
                "INSERT INTO document_chunks "
                "(document_id, chunk_index, page, content, embedding, embedding_model) "
                "VALUES (?, 0, 1, '测试内容', '[]', 'test')",
                [(10,), (20,)],
            )

        async def publish(application, document_id: int, api_key: str | None):
            path = f"/v1/documents/{document_id}/publish"
            headers = [(b"x-api-key", api_key.encode())] if api_key else []
            messages = []
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": path,
                "raw_path": path.encode(), "query_string": b"", "root_path": "",
                "headers": headers, "client": ("127.0.0.1", 1234),
                "server": ("127.0.0.1", 8000),
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await application(scope, receive, send)
            status = next(item["status"] for item in messages if item["type"] == "http.response.start")
            body = b"".join(
                item.get("body", b"") for item in messages if item["type"] == "http.response.body"
            )
            return status, json.loads(body)

        admin_key = "a" * 32
        engineer_key = "e" * 32
        other_org_key = "b" * 32
        keys = {
            admin_key: {"organization_id": 1, "role": "admin"},
            engineer_key: {"organization_id": 1, "role": "engineer"},
            other_org_key: {"organization_id": 2, "role": "admin"},
        }
        with patch.dict(os.environ, {"PILOT_API_KEYS": json.dumps(keys)}):
            for application in (main.app, create_app()):
                with self.subTest(application=application):
                    self.assertEqual(asyncio.run(publish(application, 10, None))[0], 401)
                    self.assertEqual(asyncio.run(publish(application, 10, "x" * 32))[0], 401)
                    status, body = asyncio.run(publish(application, 10, engineer_key))
                    self.assertEqual((status, body["detail"]), (403, "需要管理员权限"))
                    status, body = asyncio.run(publish(application, 99, admin_key))
                    self.assertEqual((status, body["detail"]), (404, "文档不存在"))
                    status, body = asyncio.run(publish(application, 20, admin_key))
                    self.assertEqual((status, body["detail"]), (404, "文档不存在"))
                    status, body = asyncio.run(publish(application, 30, admin_key))
                    self.assertEqual((status, body["detail"]), (409, "文档尚无可检索内容"))

                    status, body = asyncio.run(publish(application, 10, admin_key))
                    self.assertEqual(status, 200)
                    self.assertEqual((body["id"], body["status"], body["chunks"]), (10, "published", 1))
                    with database.get_connection() as conn:
                        statuses = dict(conn.execute(
                            "SELECT id, status FROM documents WHERE id IN (10, 11)"
                        ).fetchall())
                    self.assertEqual(statuses, {10: "published", 11: "superseded"})

                    status, body = asyncio.run(publish(application, 10, admin_key))
                    self.assertEqual((status, body["detail"]), (409, "只有待审核文档可以发布"))

                    with database.get_connection() as conn:
                        conn.execute("UPDATE documents SET status = 'review' WHERE id = 10")
                        conn.execute("UPDATE documents SET status = 'published' WHERE id = 11")

    def test_concurrent_publish_only_one_request_succeeds(self):
        database.init_db()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES (1, '企业甲')")
            conn.execute(
                "INSERT INTO documents "
                "(id, organization_id, family_key, title, document_type, version, status) "
                "VALUES (10, 1, 'guide', '设备手册', 'manual', '1.0', 'review')"
            )
            conn.execute(
                "INSERT INTO document_chunks "
                "(document_id, chunk_index, page, content, embedding, embedding_model) "
                "VALUES (10, 0, 1, '测试内容', '[]', 'test')"
            )

        async def publish(application):
            path = "/v1/documents/10/publish"
            messages = []
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": path,
                "raw_path": path.encode(), "query_string": b"", "root_path": "",
                "headers": [(b"x-api-key", b"a" * 32)],
                "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8000),
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await application(scope, receive, send)
            status = next(item["status"] for item in messages if item["type"] == "http.response.start")
            body = b"".join(
                item.get("body", b"") for item in messages if item["type"] == "http.response.body"
            )
            return status, json.loads(body)

        keys = {"a" * 32: {"organization_id": 1, "role": "admin"}}
        with patch.dict(os.environ, {"PILOT_API_KEYS": json.dumps(keys)}):
            for application in (main.app, create_app()):
                with self.subTest(application=application):
                    barrier = threading.Barrier(2, timeout=10)
                    original_get_connection = document_service.get_connection

                    @contextmanager
                    def synchronized_connection():
                        with original_get_connection() as connection:
                            class SynchronizedConnection:
                                def execute(self, statement, parameters=()):
                                    cursor = connection.execute(statement, parameters)
                                    if "SELECT family_key, status FROM documents" in statement:
                                        barrier.wait()
                                    return cursor

                            yield SynchronizedConnection()

                    with patch.object(
                        document_service, "get_connection", synchronized_connection
                    ):
                        async def publish_both():
                            return await asyncio.gather(
                                publish(application), publish(application)
                            )

                        results = asyncio.run(publish_both())
                    self.assertEqual(sorted(status for status, _ in results), [200, 409])
                    self.assertEqual(
                        next(body["detail"] for status, body in results if status == 409),
                        "只有待审核文档可以发布",
                    )
                    with database.get_connection() as conn:
                        status = conn.execute(
                            "SELECT status FROM documents WHERE id = 10"
                        ).fetchone()[0]
                    self.assertEqual(status, "published")
                    with database.get_connection() as conn:
                        conn.execute("UPDATE documents SET status = 'review' WHERE id = 10")

    def test_agent_without_pdf_does_not_call_embedding_api(self):
        database.init_db()
        with patch.object(pdf_rag_service, "get_embedding") as embedding:
            result = main.agent_chat(main.QuestionRequest(question="ESP32 无法连接 WiFi"))
        embedding.assert_not_called()
        self.assertEqual(result["device"], "ESP32")
        self.assertEqual(result["knowledge"]["references"], [])

    def test_http_ticket_validation_and_persistence(self):
        database.init_db()

        async def request(method, path, payload=None):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else b""
            messages = []
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": method, "scheme": "http", "path": path,
                "raw_path": path.encode(), "query_string": b"", "root_path": "",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8000),
            }

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                messages.append(message)

            await main.app(scope, receive, send)
            status = next(item["status"] for item in messages if item["type"] == "http.response.start")
            response_body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
            return status, json.loads(response_body)

        invalid_status, _ = asyncio.run(request("POST", "/tickets", {
            "customer_name": "  ", "device_name": "ESP32", "problem": "无网络",
        }))
        self.assertEqual(invalid_status, 422)
        status, ticket = asyncio.run(request("POST", "/tickets", {
            "customer_name": " 客户 ", "device_name": "ESP32", "problem": "无网络",
        }))
        self.assertEqual(status, 201)
        self.assertEqual(ticket["customer_name"], "客户")
        status, fetched = asyncio.run(request("GET", f"/tickets/{ticket['id']}"))
        self.assertEqual(status, 200)
        self.assertEqual(fetched["problem"], "无网络")


if __name__ == "__main__":
    unittest.main()
