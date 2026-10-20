"""Opt-in, bounded real-model smoke test using only the public demo PDF.

Without --live this module performs a local preflight and sends no API requests.
It is deliberately not named test_*, so unittest discovery stays offline.
"""

import argparse
import asyncio
import json
import math
import os
import re
import secrets
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import database
import main
import pdf_rag_service
from deepseek_client import chat_completion
from embedding_service import EMBEDDING_MODEL, get_embedding
from pdf_service import extract_pdf_text
from test_demo_flow import request as asgi_request


DEMO_PDF = Path(__file__).resolve().parent / "demo" / "assets" / "asteredge_x100_guide.pdf"
EXPECTED_BGE_M3_DIMENSIONS = 1024
MAX_DEMO_CHUNKS = 2


def require(condition: bool, description: str) -> None:
    if not condition:
        raise RuntimeError(description)


def failure_category(error: Exception) -> str:
    """Keep a useful error class without printing response bodies or credentials."""
    message = str(error)
    if "not configured" in message or "未配置" in message:
        return "configuration"
    if "HTTP 401" in message or "HTTP 403" in message:
        return "authentication"
    if "HTTP 429" in message:
        return "rate_limit"
    if re.search(r"HTTP 5\d\d", message):
        return "provider_server"
    if "网络连接" in message or "超时" in message:
        return "network_or_timeout"
    if "无效数据" in message or "不完整" in message:
        return "response_parsing"
    return "validation_or_other"


def check_response(method: str, path: str, expected_status: int, **kwargs: object) -> dict:
    status, payload = asgi_request(method, path, **kwargs)
    require(status == expected_status, f"{method} {path}: expected {expected_status}, got {status}")
    return payload


def collect_demo_evidence(answer: dict, pdf_bytes: bytes, document_id: int,
                          question: str) -> dict:
    """Record public-source evidence; factual support still needs human review."""
    require(answer.get("status") == "answered" and bool(answer.get("references")),
            "RAG answer was not cited")
    pages = {page["page"]: re.sub(r"\s+", " ", page["text"]).strip()
             for page in extract_pdf_text(pdf_bytes)}
    references = []
    for reference in answer["references"]:
        page = reference["page"]
        require(reference["document_id"] == document_id and page in pages,
                "citation refers to another document or page")
        quote = re.sub(r"\s+", " ", reference["content"]).strip()
        require(bool(quote) and quote in pages[page],
                "citation is not on the cited PDF page")
        references.append({"label": reference["label"], "document_id": document_id,
                           "page": page, "content": reference["content"]})
    return {"validated_at_utc": datetime.now(timezone.utc).isoformat(),
            "question": question, "answer": answer["answer"],
            "references": references, "semantic_review": "pending_manual_review"}


def run_rag(pdf_bytes: bytes, expected_chunks: int) -> dict:
    """Use actual ASGI routes and Worker logic with an isolated SQLite database."""
    with tempfile.TemporaryDirectory(prefix="ea-live-rag-") as temporary:
        storage = Path(temporary) / "uploads"
        keys = [secrets.token_urlsafe(32) for _ in range(3)]
        identities = {
            keys[0]: {"organization_id": 1, "role": "admin"},
            keys[1]: {"organization_id": 1, "role": "engineer"},
            keys[2]: {"organization_id": 2, "role": "admin"},
        }
        environment = {
            "PILOT_API_KEYS": json.dumps(identities),
            "DOCUMENT_STORAGE_PATH": str(storage),
            "PDF_INGESTION_MODE": "worker",
        }
        with patch.object(database, "DATABASE_NAME", Path(temporary) / "pilot.db"), \
                patch.dict(os.environ, environment):
            async def startup() -> None:
                async with main.lifespan(main.app):
                    pass

            asyncio.run(startup())
            check_response("GET", "/health", 200)
            check_response("POST", "/v1/organization", 201, key=keys[0],
                           payload={"name": "Public Demo Enterprise A"})
            check_response("POST", "/v1/organization", 201, key=keys[2],
                           payload={"name": "Public Demo Enterprise B"})
            model_a = check_response("POST", "/v1/device-models", 201, key=keys[0],
                                     payload={"model_code": "X100", "name": "Demo sensor"})
            model_b = check_response("POST", "/v1/device-models", 201, key=keys[2],
                                     payload={"model_code": "X100", "name": "Other sensor"})
            uploaded = check_response(
                "POST", "/v1/documents", 202, key=keys[0], body=pdf_bytes,
                content_type="application/pdf",
                query={"family_key": "asteredge-x100-live", "version": "1.0",
                       "title": "AsterEdge X100 Public Demo",
                       "device_model_ids": [model_a["id"]]},
            )
            document_id, job_id = uploaded["document_id"], uploaded["job_id"]
            question = {"device_model_id": model_a["id"],
                        "question": "What should I check for fault code E-17?"}
            before = check_response("POST", "/v1/answers", 200, key=keys[1],
                                    payload=question)
            require(before["status"] == "insufficient_evidence" and not before["references"],
                    "unpublished document was used in an answer")
            require(pdf_rag_service.process_next_queued_job(storage_dir=storage) == job_id,
                    "Worker did not process the queued job")
            job = check_response("GET", f"/v1/ingestion-jobs/{job_id}", 200, key=keys[0])
            detail = check_response("GET", f"/v1/documents/{document_id}", 200, key=keys[1])
            require(job["status"] == "succeeded" and detail["status"] == "review",
                    "Worker did not finish with document in review")
            require(detail["chunks"] == expected_chunks, "unexpected chunk count")
            in_review = check_response("POST", "/v1/answers", 200, key=keys[1],
                                       payload=question)
            require(in_review["status"] == "insufficient_evidence" and not in_review["references"],
                    "review document was used in an answer")
            check_response("GET", f"/v1/documents/{document_id}", 404, key=keys[2])
            check_response("GET", f"/v1/ingestion-jobs/{job_id}", 404, key=keys[2])
            check_response("POST", f"/v1/documents/{document_id}/publish", 403, key=keys[1])
            check_response("POST", f"/v1/documents/{document_id}/publish", 404, key=keys[2])
            published = check_response("POST", f"/v1/documents/{document_id}/publish", 200,
                                       key=keys[0])
            require(published["status"] == "published", "document was not published")
            answer = check_response("POST", "/v1/answers", 200, key=keys[1],
                                    payload=question)
            require(answer["status"] == "answered" and answer["references"],
                    f"RAG answer was not cited (status={answer['status']})")
            require("E-17" in answer["answer"] or "12 V" in answer["answer"],
                    "model answer does not appear related to the demo fault")
            evidence = collect_demo_evidence(
                answer, pdf_bytes, document_id, question["question"],
            )
            check_response("POST", "/v1/answers", 404, key=keys[2], payload=question)
            other = check_response("POST", "/v1/answers", 200, key=keys[2],
                                   payload={"device_model_id": model_b["id"],
                                            "question": question["question"]})
            require(other["status"] == "insufficient_evidence" and not other["references"],
                    "cross-tenant answer included another tenant's knowledge")
            return {"job_status": job["status"], "document_status": published["status"],
                    "chunks": detail["chunks"], "answer_status": answer["status"],
                    "citations": len(answer["references"]), "tenant_isolation": "passed",
                    "evidence": evidence}


def main_cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="allow real paid model requests")
    parser.add_argument("--e2e", action="store_true", help="also run one isolated real PDF RAG flow")
    args = parser.parse_args()
    require(not args.e2e or args.live, "--e2e also requires --live")
    require(DEMO_PDF.is_file(), "public demo PDF is missing")
    pdf_bytes = DEMO_PDF.read_bytes()
    chunks = [chunk for page in extract_pdf_text(pdf_bytes)
              for chunk in pdf_rag_service.split_text(page["text"])]
    require(0 < len(chunks) <= MAX_DEMO_CHUNKS, "demo PDF exceeds the request budget")
    print(json.dumps({"mode": "live" if args.live else "preflight-only",
                      "demo_pdf_bytes": len(pdf_bytes), "demo_chunks": len(chunks),
                      "siliconflow_configured": bool(os.getenv("SILICONFLOW_API_KEY")),
                      "deepseek_configured": bool(os.getenv("DEEPSEEK_API_KEY")),
                      "planned_embedding_requests_if_live": 1 + (len(chunks) + 1 if args.e2e else 0),
                      "planned_chat_requests_if_live": 1 + (1 if args.e2e else 0)}, ensure_ascii=False))
    if not args.live:
        return 0
    require(bool(os.getenv("SILICONFLOW_API_KEY")), "SILICONFLOW_API_KEY is not configured")
    require(bool(os.getenv("DEEPSEEK_API_KEY")), "DEEPSEEK_API_KEY is not configured")

    original_urlopen = urllib.request.urlopen
    responses: list[tuple[str, int]] = []
    attempts = {"embedding": 0, "chat": 0}
    limits = {"embedding": 1 + (len(chunks) + 1 if args.e2e else 0),
              "chat": 1 + (1 if args.e2e else 0)}

    def recorded_urlopen(request: urllib.request.Request, *a: object, **kw: object):
        if request.full_url == "https://api.siliconflow.cn/v1/embeddings":
            endpoint = "embedding"
        elif request.full_url == "https://api.deepseek.com/chat/completions":
            endpoint = "chat"
        else:
            raise RuntimeError("unapproved live API endpoint")
        require(attempts[endpoint] < limits[endpoint], f"{endpoint} request budget exceeded")
        attempts[endpoint] += 1
        response = original_urlopen(request, *a, **kw)
        responses.append((endpoint, response.status))
        return response

    with patch("urllib.request.urlopen", side_effect=recorded_urlopen):
        started = time.perf_counter()
        vector = get_embedding("AsterEdge X100 demo sensor: fault E-17 checks the 12 V DC supply.")
        require(len(vector) == EXPECTED_BGE_M3_DIMENSIONS, "unexpected embedding dimension")
        require(all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) for value in vector), "invalid embedding values")
        print(json.dumps({"embedding": "SUCCESS", "model": EMBEDDING_MODEL,
                          "dimensions": len(vector), "http_status": responses[-1][1],
                          "seconds": round(time.perf_counter() - started, 3)}))
        started = time.perf_counter()
        text = chat_completion([{"role": "user", "content": "Reply with exactly OK."}])
        require(bool(text.strip()), "DeepSeek returned no text")
        print(json.dumps({"deepseek": "SUCCESS", "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
                          "http_status": responses[-1][1],
                          "seconds": round(time.perf_counter() - started, 3)}))
        if args.e2e:
            started = time.perf_counter()
            summary = run_rag(pdf_bytes, len(chunks))
            summary["seconds"] = round(time.perf_counter() - started, 3)
            summary["embedding_http_calls"] = sum(kind == "embedding" for kind, _ in responses)
            summary["chat_http_calls"] = sum(kind == "chat" for kind, _ in responses)
            require(attempts["embedding"] == len(chunks) + 2,
                    "unexpected number of embedding requests")
            require(attempts["chat"] == 2, "unexpected number of chat requests")
            print(json.dumps({"e2e": "SUCCESS", **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_cli())
    except (RuntimeError, ValueError) as error:
        # Production adapters already sanitize HTTP/network errors; do not print headers.
        print(json.dumps({"result": "FAILED_OR_BLOCKED", "category": failure_category(error),
                          "reason": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None
