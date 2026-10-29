"""Run the API and ingestion worker together on one Render disk-backed service."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    required = ("DATABASE_PATH", "DOCUMENT_STORAGE_PATH", "PILOT_API_KEYS", "SILICONFLOW_API_KEY", "DEEPSEEK_API_KEY")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
    if os.getenv("PDF_INGESTION_MODE") != "worker":
        raise RuntimeError("PDF_INGESTION_MODE must be worker")
    port = int(os.getenv("PORT", "10000"))
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT is out of range")

    database_path = Path(os.environ["DATABASE_PATH"])
    storage_path = Path(os.environ["DOCUMENT_STORAGE_PATH"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.mkdir(parents=True, exist_ok=True)

    # Import after validating the environment: database.py reads DATABASE_PATH at import time.
    from database import init_db
    init_db()

    stopping = False

    def request_stop(_signal: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    api = None
    worker = None
    try:
        api = subprocess.Popen([
            sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0",
            "--port", str(port), "--workers", "1",
        ])
        worker = subprocess.Popen([
            sys.executable, "pdf_rag_service.py", "work", "--poll-seconds", "3",
        ])
        while not stopping:
            if api.poll() is not None:
                raise RuntimeError(f"API exited with code {api.returncode}")
            if worker.poll() is not None:
                raise RuntimeError(f"Worker exited with code {worker.returncode}")
            time.sleep(1)
    finally:
        for process in (worker, api):
            if process is not None and process.poll() is None:
                process.terminate()
        for process in (worker, api):
            if process is None:
                continue
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
