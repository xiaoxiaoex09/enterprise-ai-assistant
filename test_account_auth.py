"""Password login and tenant isolation through the real ASGI routes."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
from test_demo_flow import DEMO_PDF, request


class AccountAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db_patch = patch.object(database, "DATABASE_NAME", Path(temporary.name) / "pilot.db")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        self.admin_a, self.admin_b = "a" * 32, "b" * 32
        env_patch = patch.dict(os.environ, {
            "PILOT_API_KEYS": json.dumps({
                self.admin_a: {"organization_id": 1, "role": "admin"},
                self.admin_b: {"organization_id": 2, "role": "admin"},
            }),
            "DOCUMENT_STORAGE_PATH": str(Path(temporary.name) / "uploads"),
            "PDF_INGESTION_MODE": "worker",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        database.init_db()

    def test_password_sessions_keep_documents_inside_their_organization(self) -> None:
        password_a, password_b = "A-long-secret-123", "B-long-secret-456"
        for key, organization, password in (
            (self.admin_a, "企业甲", password_a),
            (self.admin_b, "企业乙", password_b),
        ):
            status, _ = request("POST", "/v1/auth/bootstrap", key=key, payload={
                "organization": organization, "username": "manager", "password": password,
            })
            self.assertEqual(status, 201)
        self.assertEqual(request("POST", "/v1/auth/bootstrap", key=self.admin_a, payload={
            "organization": "企业甲", "username": "other", "password": password_a,
        })[0], 409)
        self.assertEqual(request("POST", "/v1/auth/login", payload={
            "organization": "企业甲", "username": "manager", "password": password_b,
        })[0], 401)

        token_a = request("POST", "/v1/auth/login", payload={
            "organization": "企业甲", "username": "manager", "password": password_a,
        })[1]["token"]
        token_b = request("POST", "/v1/auth/login", payload={
            "organization": "企业乙", "username": "manager", "password": password_b,
        })[1]["token"]
        auth_a, auth_b = f"Bearer {token_a}", f"Bearer {token_b}"
        self.assertEqual(request("GET", "/v1/auth/me", authorization=auth_a)[1]["organization"], "企业甲")
        self.assertEqual(request("GET", "/v1/auth/me", authorization=auth_b)[1]["organization"], "企业乙")

        status, model = request("POST", "/v1/device-models", authorization=auth_a,
                                payload={"model_code": "X100", "name": "设备甲"})
        self.assertEqual(status, 201)
        status, upload = request(
            "POST", "/v1/documents", authorization=auth_a,
            body=DEMO_PDF.read_bytes(), content_type="application/pdf",
            query={"family_key": "guide", "version": "1.0", "title": "甲企业手册",
                   "device_model_ids": [model["id"]]},
        )
        self.assertEqual(status, 202)
        document_id = upload["document_id"]
        self.assertEqual([item["id"] for item in request("GET", "/v1/documents", authorization=auth_a)[1]], [document_id])
        self.assertEqual(request("GET", "/v1/documents", authorization=auth_b), (200, []))
        self.assertEqual(request("GET", f"/v1/documents/{document_id}", authorization=auth_b)[0], 404)
        self.assertEqual(request("GET", f"/v1/ingestion-jobs/{upload['job_id']}", authorization=auth_b)[0], 404)

        status, _ = request("POST", "/v1/auth/users", authorization=auth_a,
                            payload={"username": "engineer", "password": "engineer-pass-123", "role": "engineer"})
        self.assertEqual(status, 201)
        engineer_token = request("POST", "/v1/auth/login", payload={
            "organization": "企业甲", "username": "engineer", "password": "engineer-pass-123",
        })[1]["token"]
        self.assertEqual(request("POST", "/v1/auth/users", authorization=f"Bearer {engineer_token}",
                                 payload={"username": "other", "password": "another-pass-123"})[0], 403)
        self.assertEqual(request("POST", "/v1/auth/logout", authorization=auth_a)[0], 204)
        self.assertEqual(request("GET", "/v1/documents", authorization=auth_a)[0], 401)


if __name__ == "__main__":
    unittest.main()
