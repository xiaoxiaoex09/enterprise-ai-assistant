"""Read and publish organization-scoped documents."""

from typing import Any

from database import get_connection


class DocumentNotFoundError(Exception):
    """The requested document does not belong to the organization."""


class DocumentPublishConflictError(Exception):
    """The document cannot be published in its current state."""


def get_document(document_id: int, organization_id: int) -> dict[str, Any]:
    """Return one document with its device models and chunk count."""
    with get_connection() as conn:
        document = conn.execute(
            """
            SELECT id, family_key, title, version, status, document_type
            FROM documents WHERE id = ? AND organization_id = ?
            """,
            (document_id, organization_id),
        ).fetchone()
        if document is None:
            raise DocumentNotFoundError("文档不存在")
        model_rows = conn.execute(
            """
            SELECT device_model_id FROM document_models
            WHERE document_id = ? AND organization_id = ?
            ORDER BY device_model_id
            """,
            (document_id, organization_id),
        ).fetchall()
        chunk_count = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]
    return {
        **dict(document),
        "device_model_ids": [row[0] for row in model_rows],
        "chunks": chunk_count,
    }


def publish_document(document_id: int, organization_id: int) -> dict[str, Any]:
    """Publish a reviewed document and supersede its prior published version."""
    with get_connection() as conn:
        document = conn.execute(
            """
            SELECT family_key, status FROM documents
            WHERE id = ? AND organization_id = ?
            """,
            (document_id, organization_id),
        ).fetchone()
        if document is None:
            raise DocumentNotFoundError("文档不存在")
        if document["status"] != "review":
            raise DocumentPublishConflictError("只有待审核文档可以发布")
        chunk_count = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]
        if chunk_count == 0:
            raise DocumentPublishConflictError("文档尚无可检索内容")
        conn.execute(
            """
            UPDATE documents SET status = 'superseded'
            WHERE organization_id = ? AND family_key = ? AND status = 'published'
            """,
            (organization_id, document["family_key"]),
        )
        updated = conn.execute(
            """
            UPDATE documents SET status = 'published', published_at = CURRENT_TIMESTAMP
            WHERE id = ? AND organization_id = ? AND status = 'review'
            """,
            (document_id, organization_id),
        )
        if updated.rowcount != 1:
            still_exists = conn.execute(
                "SELECT 1 FROM documents WHERE id = ? AND organization_id = ?",
                (document_id, organization_id),
            ).fetchone()
            if still_exists is None:
                raise DocumentNotFoundError("文档不存在")
            raise DocumentPublishConflictError("只有待审核文档可以发布")
    return get_document(document_id, organization_id)
