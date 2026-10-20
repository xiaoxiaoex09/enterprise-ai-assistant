"""The opt-in live runner must never make network requests by default."""

import contextlib
import io
import os
import sys
import unittest
import urllib.request
from unittest.mock import patch

import live_model_smoke
import pdf_chat_service
import pdf_rag_service


class LiveModelSafetyTests(unittest.TestCase):
    def test_default_preflight_has_no_model_requests(self) -> None:
        with patch.object(sys, "argv", ["live_model_smoke.py"]), \
                patch.object(urllib.request, "urlopen", side_effect=AssertionError("network used")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live_model_smoke.main_cli(), 0)

    def test_live_requires_keys_before_network(self) -> None:
        with patch.object(sys, "argv", ["live_model_smoke.py", "--live", "--e2e"]), \
                patch.dict(os.environ, {"SILICONFLOW_API_KEY": "", "DEEPSEEK_API_KEY": ""}), \
                patch.object(urllib.request, "urlopen", side_effect=AssertionError("network used")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "SILICONFLOW_API_KEY"):
                live_model_smoke.main_cli()

    def test_e2e_harness_uses_only_isolated_test_data(self) -> None:
        pdf_bytes = live_model_smoke.DEMO_PDF.read_bytes()
        with patch.object(pdf_rag_service, "get_embedding", return_value=[1.0, 0.0, 0.0]), \
                patch("pdf_chat_service.chat_completion",
                      return_value="For fault E-17, check the 12 V supply [资料1]."):
            result = live_model_smoke.run_rag(pdf_bytes, 2)
        self.assertEqual(result["job_status"], "succeeded")
        self.assertEqual(result["answer_status"], "answered")
        self.assertEqual(result["tenant_isolation"], "passed")

    def test_provider_error_categories_are_distinct(self) -> None:
        cases = {
            "HTTP 401": "authentication",
            "HTTP 429": "rate_limit",
            "HTTP 503": "provider_server",
            "网络连接或响应超时": "network_or_timeout",
            "返回了无效数据": "response_parsing",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(live_model_smoke.failure_category(RuntimeError(message)), expected)

    def test_invalid_citation_number_is_not_accepted(self) -> None:
        reference = {"chunk_id": 1, "document_id": 1, "title": "Demo",
                     "version": "1.0", "page": 1, "content": "12 V DC", "score": 0.9}
        with patch.object(pdf_chat_service, "search_versioned_knowledge",
                          return_value=[reference]), \
                patch.object(pdf_chat_service, "chat_completion",
                             return_value="Use a 48 V battery [资料2]."):
            result = pdf_chat_service.ask_versioned_pdf(
                "What is the supply?", organization_id=1, device_model_id=1,
            )
        self.assertEqual((result["status"], result["references"]), ("needs_review", []))

    def test_wrong_page_is_rejected_by_demo_evidence(self) -> None:
        pdf_bytes = live_model_smoke.DEMO_PDF.read_bytes()
        answer = {"status": "answered", "answer": "Check the 12 V supply [资料1].",
                  "references": [{"label": "资料1", "document_id": 1,
                                  "page": 2, "content": "12 V DC"}]}
        with self.assertRaisesRegex(RuntimeError, "another document or page"):
            live_model_smoke.collect_demo_evidence(answer, pdf_bytes, 1, "E-17?")

    def test_real_quote_does_not_claim_semantic_support(self) -> None:
        pdf_bytes = live_model_smoke.DEMO_PDF.read_bytes()
        answer = {"status": "answered",
                  "answer": "Replace the battery with a 48 V unit [资料1].",
                  "references": [{"label": "资料1", "document_id": 1,
                                  "page": 1,
                                  "content": "Meaning: the sensor is not receiving a stable 12 V DC supply."}]}
        evidence = live_model_smoke.collect_demo_evidence(
            answer, pdf_bytes, 1, "What should I do?",
        )
        self.assertEqual(evidence["semantic_review"], "pending_manual_review")
        self.assertEqual(evidence["answer"], answer["answer"])

    def test_no_source_does_not_call_chat_or_create_evidence(self) -> None:
        with patch.object(pdf_chat_service, "search_versioned_knowledge",
                          return_value=[]), \
                patch.object(pdf_chat_service, "chat_completion") as chat:
            result = pdf_chat_service.ask_versioned_pdf(
                "Unknown specification?", organization_id=1, device_model_id=1,
            )
        self.assertEqual((result["status"], result["references"]),
                         ("insufficient_evidence", []))
        chat.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "not cited"):
            live_model_smoke.collect_demo_evidence(
                result, live_model_smoke.DEMO_PDF.read_bytes(), 1, "Unknown specification?",
            )


if __name__ == "__main__":
    unittest.main()
