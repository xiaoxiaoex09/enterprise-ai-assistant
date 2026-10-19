"""Prepare a versioned PDF upload and its durable ingestion job."""

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from database import get_connection


MAX_PDF_BYTES = 50 * 1024 * 1024


def _check_storage_path(path: Path, storage_root: Path) -> None:
    """Reject an upload target that resolves outside the configured storage root."""
    if not path.resolve().is_relative_to(storage_root):
        raise ValueError("文档存储路径无效")


def _is_duplicate_version(error: sqlite3.IntegrityError) -> bool:
    return (
        getattr(error, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_UNIQUE"
        and str(error) == (
            "UNIQUE constraint failed: "
            "documents.organization_id, documents.family_key, documents.version"
        )
    )


def create_ingestion_job(
    pdf_bytes: bytes,
    *,
    organization_id: int,
    device_model_ids: Sequence[int],
    family_key: str,
    version: str,
    title: str,
    storage_dir: str | Path,
) -> dict[str, int | str]:
    """Save a manual and create a durable ingestion job without calling the model."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("上传内容不是有效的PDF")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError("PDF不能超过50MB")
    if not isinstance(organization_id, int) or organization_id <= 0:
        raise ValueError("企业ID无效")

    model_ids = tuple(dict.fromkeys(device_model_ids))
    if not model_ids or any(not isinstance(value, int) or value <= 0 for value in model_ids):
        raise ValueError("至少选择一个有效的设备型号")
    family_key = family_key.strip()
    version = version.strip()
    title = title.strip()
    if not all((family_key, version, title)):
        raise ValueError("文档标识、版本和标题不能为空")

    placeholders = ", ".join("?" for _ in model_ids)
    with get_connection() as conn:
        organization = conn.execute(
            "SELECT 1 FROM organizations WHERE id = ?", (organization_id,)
        ).fetchone()
        if organization is None:
            raise ValueError("企业不存在")
        matched = conn.execute(
            f"SELECT COUNT(*) FROM device_models "
            f"WHERE organization_id = ? AND id IN ({placeholders})",
            (organization_id, *model_ids),
        ).fetchone()[0]
        if matched != len(model_ids):
            raise ValueError("设备型号不属于该企业")

    storage_root = Path(storage_dir).resolve()
    storage_key = f"{organization_id}/{uuid4().hex}.pdf"
    file_path = storage_root / storage_key
    temp_path = file_path.with_suffix(".pdf.tmp")
    file_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    temp_created = False
    published_file = False
    committed = False
    try:
        _check_storage_path(file_path, storage_root)
        _check_storage_path(temp_path, storage_root)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        _check_storage_path(file_path, storage_root)
        _check_storage_path(temp_path, storage_root)
        with temp_path.open("xb") as temporary_file:
            temp_created = True
            temporary_file.write(pdf_bytes)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        _check_storage_path(file_path, storage_root)
        _check_storage_path(temp_path, storage_root)
        if file_path.exists() or file_path.is_symlink():
            raise FileExistsError("文档存储目标已存在")
        os.replace(temp_path, file_path)
        temp_created = False
        published_file = True

        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO documents
                    (organization_id, family_key, title, document_type,
                     version, status, storage_key, sha256)
                VALUES (?, ?, ?, 'manual', ?, 'processing', ?, ?)
                """,
                (organization_id, family_key, title, version, storage_key, file_sha256),
            )
            document_id = cursor.lastrowid
            conn.executemany(
                """
                INSERT INTO document_models
                    (organization_id, document_id, device_model_id)
                VALUES (?, ?, ?)
                """,
                ((organization_id, document_id, model_id) for model_id in model_ids),
            )
            job_id = conn.execute(
                "INSERT INTO ingestion_jobs (document_id) VALUES (?)",
                (document_id,),
            ).lastrowid
        committed = True
    except sqlite3.IntegrityError as error:
        if _is_duplicate_version(error):
            raise ValueError("该企业的同一文档版本已存在") from error
        raise
    finally:
        if temp_created:
            temp_path.unlink(missing_ok=True)
        if published_file and not committed:
            file_path.unlink(missing_ok=True)

    return {"document_id": document_id, "job_id": job_id, "status": "queued"}
