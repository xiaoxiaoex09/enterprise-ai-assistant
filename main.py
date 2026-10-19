
import sqlite3
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, StringConstraints

from app.api.routes.documents import (
    get_pilot_document,
    publish_pilot_document,
    upload_pilot_document,
)
from app.api.routes.health import health_check
from app.api.schemas.document import PilotDocumentQueueResponse, PilotDocumentResponse
from app.core.auth import (
    PilotIdentity,
    get_pilot_admin,
    get_pilot_identity,
    pilot_api_key_header,
)
from app.core.factory import close_legacy_routes_in_pilot_mode
from app.main import app, lifespan
from database import get_connection, init_db
from ai_service import analyze_ticket_with_ai
from agent_service import run_agent
from rag_ai_service import ask_company_ai
from pdf_rag_service import (
    ingest_pdf,
)
from pdf_chat_service import ask_pdf, ask_versioned_pdf


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PilotQuestion = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
class TicketCreate(BaseModel):
    customer_name: NonEmptyText
    device_name: NonEmptyText
    problem: NonEmptyText


class QuestionRequest(BaseModel):
    question: NonEmptyText


class PilotAnswerRequest(BaseModel):
    device_model_id: int = Field(gt=0)
    question: PilotQuestion
    fault_code: str | None = Field(default=None, max_length=64)


class PilotAnswerReference(BaseModel):
    label: str
    chunk_id: int
    document_id: int
    title: str
    version: str
    page: int
    content: str
    score: float


class PilotAnswerResponse(BaseModel):
    status: Literal["answered", "insufficient_evidence", "needs_review"]
    answer: str
    references: list[PilotAnswerReference]


class PilotOrganizationCreate(BaseModel):
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)
    ]


class PilotOrganizationResponse(BaseModel):
    id: int
    name: str


class PilotDeviceModelCreate(BaseModel):
    model_code: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ]
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]


class PilotDeviceModelResponse(BaseModel):
    id: int
    model_code: str
    name: str


class PilotIngestionJobResponse(BaseModel):
    id: int
    document_id: int
    status: Literal["queued", "running", "succeeded", "failed"]
    error_message: str | None


@app.post(
    "/v1/organization",
    status_code=201,
    response_model=PilotOrganizationResponse,
    tags=["pilot"],
)
def create_pilot_organization(
    payload: PilotOrganizationCreate,
    identity: Annotated[PilotIdentity, Depends(get_pilot_admin)],
):
    """Provision only the organization assigned to this admin's API key."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM organizations WHERE id = ?",
            (identity.organization_id,),
        ).fetchone()
        if existing is not None:
            raise HTTPException(status_code=409, detail="企业已初始化")
        try:
            conn.execute(
                "INSERT INTO organizations (id, name) VALUES (?, ?)",
                (identity.organization_id, payload.name),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="企业名称已使用") from error
    return {"id": identity.organization_id, "name": payload.name}


@app.get(
    "/v1/organization",
    response_model=PilotOrganizationResponse,
    tags=["pilot"],
)
def get_pilot_organization(
    identity: Annotated[PilotIdentity, Depends(get_pilot_identity)],
):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, name FROM organizations WHERE id = ?",
            (identity.organization_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="企业尚未初始化")
    return dict(row)


@app.post(
    "/v1/device-models",
    status_code=201,
    response_model=PilotDeviceModelResponse,
    tags=["pilot"],
)
def create_pilot_device_model(
    payload: PilotDeviceModelCreate,
    identity: Annotated[PilotIdentity, Depends(get_pilot_admin)],
):
    model_code = payload.model_code.upper()
    with get_connection() as conn:
        organization = conn.execute(
            "SELECT 1 FROM organizations WHERE id = ?",
            (identity.organization_id,),
        ).fetchone()
        if organization is None:
            raise HTTPException(status_code=404, detail="企业尚未初始化")
        try:
            cursor = conn.execute(
                """
                INSERT INTO device_models (organization_id, model_code, name)
                VALUES (?, ?, ?)
                """,
                (identity.organization_id, model_code, payload.name),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="设备型号编码已存在") from error
    return {"id": cursor.lastrowid, "model_code": model_code, "name": payload.name}


@app.get(
    "/v1/device-models",
    response_model=list[PilotDeviceModelResponse],
    tags=["pilot"],
)
def list_pilot_device_models(
    identity: Annotated[PilotIdentity, Depends(get_pilot_identity)],
):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, model_code, name FROM device_models
            WHERE organization_id = ? ORDER BY model_code
            """,
            (identity.organization_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get(
    "/v1/ingestion-jobs/{job_id}",
    response_model=PilotIngestionJobResponse,
    tags=["pilot"],
)
def get_pilot_ingestion_job(
    job_id: int,
    identity: Annotated[PilotIdentity, Depends(get_pilot_admin)],
):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT j.id, j.document_id, j.status, j.error_message
            FROM ingestion_jobs AS j
            JOIN documents AS d ON d.id = j.document_id
            WHERE j.id = ? AND d.organization_id = ?
            """,
            (job_id, identity.organization_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="入库任务不存在")
    return dict(row)


@app.post("/v1/answers", response_model=PilotAnswerResponse, tags=["pilot"])
def create_pilot_answer(
    payload: PilotAnswerRequest,
    identity: Annotated[PilotIdentity, Depends(get_pilot_identity)],
):
    with get_connection() as conn:
        model = conn.execute(
            """
            SELECT 1 FROM device_models
            WHERE id = ? AND organization_id = ?
            """,
            (payload.device_model_id, identity.organization_id),
        ).fetchone()
    if model is None:
        raise HTTPException(status_code=404, detail="设备型号不存在")

    question = payload.question
    if payload.fault_code and payload.fault_code.strip():
        question = f"故障代码：{payload.fault_code.strip()}。{question}"
    try:
        return ask_versioned_pdf(
            question,
            organization_id=identity.organization_id,
            device_model_id=payload.device_model_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error



@app.post("/tickets", status_code=201)
def create_ticket(ticket: TicketCreate):
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tickets
            (customer_name, device_name, problem, status)
            VALUES (?, ?, ?, ?)
            """,
            (
                ticket.customer_name,
                ticket.device_name,
                ticket.problem,
                "待处理"
            )
        )

        ticket_id = cursor.lastrowid
        conn.commit()

        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?",
            (ticket_id,)
        ).fetchone()

        return dict(row)

@app.get("/")
def home():
    return {
        "message": "Enterprise AI Assistant is running!",
        "status": "ok"
    }


@app.get("/tickets")
def get_tickets():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY id DESC"
        ).fetchall()

        return [dict(row) for row in rows]


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return dict(row)

@app.post("/tickets/{ticket_id}/analyze")
def analyze_ticket(ticket_id: int):

    with get_connection() as conn:

        row = conn.execute(
            "SELECT * FROM tickets WHERE id=?",
            (ticket_id,)
        ).fetchone()


    if row is None:
        raise HTTPException(
            status_code=404,
            detail="工单不存在"
        )


    ticket = dict(row)


    try:
        analysis = analyze_ticket_with_ai(ticket)

    except RuntimeError as e:

        raise HTTPException(
            status_code=502,
            detail=str(e)
        )

    with get_connection() as conn:

        conn.execute(
            """
            UPDATE tickets
            SET ai_analysis=?,
                status=?
            WHERE id = ?
            """,
            (
                analysis,
                "AI已分析",
                ticket_id
            )
        )

        conn.commit()

    return {
        "ticket_id": ticket_id,
        "status": "AI分析完成",
        "analysis": analysis
    }


@app.post("/knowledge/chat")
def knowledge_chat(question: str = Query(min_length=1)):

    try:
        result = ask_company_ai(question)

    except RuntimeError as error:
        raise HTTPException(
            status_code=502,
            detail=str(error)
        ) from error

    return {
        "question": question,
        "answer": result["answer"],
        "references": result["references"]
    }


# 上传 PDF，建立向量知识库
@app.post("/knowledge/upload-pdf")
async def upload_pdf(
    request: Request,
    filename: str,
    doc_type: str = "unknown"
):
    safe_name = Path(filename.replace(
        "\\", "/"
    )).name

    if (
        not safe_name
        or not safe_name.lower().endswith(".pdf")
    ):
        raise HTTPException(
            status_code=400,
            detail="文件名必须以.pdf结尾"
        )

    pdf_bytes = await request.body()

    # PDF大小限制50MB
    if len(pdf_bytes) > 50 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="PDF不能超过50MB"
        )

    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=400,
            detail="上传内容不是有效的PDF"
        )

    try:
        result = await run_in_threadpool(
            ingest_pdf, pdf_bytes, safe_name, doc_type
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
        ) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=502,
            detail=str(error)
        ) from error

    return {
        "message": "PDF知识库建立成功",
        **result
    }


# 基于PDF进行向量检索与AI问答
@app.post("/knowledge/pdf-chat")
def pdf_chat(question: str = Query(min_length=1)):
    try:
        return ask_pdf(question)
    except RuntimeError as error:
        raise HTTPException(
            status_code=502,
            detail=str(error)
        ) from error


@app.post("/agent/chat")
def agent_chat(request: QuestionRequest):
    try:
        return run_agent(request.question)
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
