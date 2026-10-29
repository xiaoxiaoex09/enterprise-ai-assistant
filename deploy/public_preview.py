"""Run the single-machine pilot and a temporary public Cloudflare URL."""

import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STORAGE = ROOT / "storage" / "public"
KEY_FILE = DATA / "public_access.json"
URL_FILE = DATA / "public_url.txt"
STOP_FILE = DATA / "public.stop"
TUNNEL = Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe")
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def access_keys() -> dict[str, str]:
    if KEY_FILE.exists():
        data = json.loads(KEY_FILE.read_text(encoding="utf-8"))
        if all(isinstance(data.get(role), str) and len(data[role]) >= 32 for role in ("admin", "engineer")):
            return data
        raise RuntimeError("现有访问密钥文件格式无效，请人工检查")
    keys = {"admin": secrets.token_urlsafe(32), "engineer": secrets.token_urlsafe(32)}
    with KEY_FILE.open("x", encoding="utf-8") as handle:
        json.dump(keys, handle, ensure_ascii=False, indent=2)
    os.chmod(KEY_FILE, 0o600)
    return keys


def spawn(command: list[str], environment: dict[str, str], log_name: str) -> subprocess.Popen[bytes]:
    log = (DATA / log_name).open("wb", buffering=0)
    try:
        return subprocess.Popen(
            command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        log.close()


def main() -> int:
    DATA.mkdir(exist_ok=True)
    STORAGE.mkdir(parents=True, exist_ok=True)
    if not TUNNEL.is_file():
        raise RuntimeError(f"未找到 cloudflared：{TUNNEL}")
    if not os.getenv("SILICONFLOW_API_KEY") or not os.getenv("DEEPSEEK_API_KEY"):
        raise RuntimeError("当前进程缺少模型 API Key")
    keys = access_keys()
    URL_FILE.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update({
        "PILOT_API_KEYS": json.dumps({
            keys["admin"]: {"organization_id": 1, "role": "admin"},
            keys["engineer"]: {"organization_id": 1, "role": "engineer"},
        }),
        "DATABASE_PATH": str(DATA / "public-pilot.db"),
        "DOCUMENT_STORAGE_PATH": str(STORAGE),
        "PDF_INGESTION_MODE": "worker",
    })
    python = sys.executable
    api = spawn([python, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000", "--workers", "1"], environment, "public-api.log")
    worker = spawn([python, "pdf_rag_service.py", "work", "--poll-seconds", "3"], environment, "public-worker.log")
    tunnel = spawn([str(TUNNEL), "tunnel", "--url", "http://127.0.0.1:8000"], environment, "public-tunnel.log")
    try:
        for _ in range(60):
            if any(process.poll() is not None for process in (api, worker, tunnel)):
                raise RuntimeError("API、Worker 或隧道启动失败，请查看 data/public-*.log")
            log_text = (DATA / "public-tunnel.log").read_text(encoding="utf-8", errors="replace")
            match = URL_PATTERN.search(log_text)
            if match:
                URL_FILE.write_text(match.group(0) + "/login\n", encoding="utf-8")
                break
            time.sleep(1)
        else:
            raise RuntimeError("一分钟内未获得公网地址，请查看 data/public-tunnel.log")
        while not STOP_FILE.exists():
            if any(process.poll() is not None for process in (api, worker, tunnel)):
                raise RuntimeError("API、Worker 或隧道已退出，请查看 data/public-*.log")
            time.sleep(3)
    finally:
        URL_FILE.unlink(missing_ok=True)
        for process in (tunnel, worker, api):
            if process.poll() is None:
                process.terminate()
        for process in (tunnel, worker, api):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        STOP_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
