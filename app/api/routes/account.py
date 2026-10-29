"""Tenant-scoped password login and user provisioning for the pilot UI."""

import hashlib
import secrets
import sqlite3
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from app.core.auth import PilotIdentity, get_pilot_admin, get_pilot_identity
from database import get_connection


router = APIRouter(prefix="/v1/auth", tags=["pilot-auth"])
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=50)]
Password = Annotated[str, Field(min_length=12, max_length=128)]
ROUNDS = 600_000
SESSION_SECONDS = 12 * 60 * 60


class LoginRequest(BaseModel):
    organization: Name
    username: Username
    password: Password


class BootstrapRequest(BaseModel):
    organization: Name
    username: Username
    password: Password


class UserCreate(BaseModel):
    username: Username
    password: Password
    role: Literal["admin", "engineer"] = "engineer"


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ROUNDS)
    return f"pbkdf2_sha256${ROUNDS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256" or int(rounds) != ROUNDS:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), ROUNDS)
        return secrets.compare_digest(actual, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


@router.post("/bootstrap", status_code=201)
def bootstrap_admin(
    payload: BootstrapRequest,
    identity: Annotated[PilotIdentity, Depends(get_pilot_admin)],
) -> dict[str, int | str]:
    """Use the existing admin API key once to create the first password account."""
    encoded = _password_hash(payload.password)
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            organization = conn.execute(
                "SELECT name FROM organizations WHERE id = ?", (identity.organization_id,)
            ).fetchone()
            if organization is None:
                conn.execute(
                    "INSERT INTO organizations (id, name) VALUES (?, ?)",
                    (identity.organization_id, payload.organization),
                )
            elif organization["name"] != payload.organization:
                raise HTTPException(status_code=409, detail="企业名称与已初始化的企业不一致")
            existing = conn.execute(
                "SELECT 1 FROM pilot_users WHERE organization_id = ? LIMIT 1",
                (identity.organization_id,),
            ).fetchone()
            if existing:
                raise HTTPException(status_code=409, detail="企业已开通账号")
            cursor = conn.execute(
                "INSERT INTO pilot_users (organization_id, username, role, password_hash) VALUES (?, ?, 'admin', ?)",
                (identity.organization_id, payload.username, encoded),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="企业名称或用户名已存在") from error
    return {"organization_id": identity.organization_id, "user_id": cursor.lastrowid, "username": payload.username}


@router.post("/users", status_code=201)
def create_user(
    payload: UserCreate,
    identity: Annotated[PilotIdentity, Depends(get_pilot_admin)],
) -> dict[str, int | str]:
    encoded = _password_hash(payload.password)
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO pilot_users (organization_id, username, role, password_hash) VALUES (?, ?, ?, ?)",
                (identity.organization_id, payload.username, payload.role, encoded),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="用户名已存在") from error
    return {"id": cursor.lastrowid, "username": payload.username, "role": payload.role}


@router.get("/users")
def list_users(
    identity: Annotated[PilotIdentity, Depends(get_pilot_admin)],
) -> list[dict[str, int | str]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, username, role FROM pilot_users WHERE organization_id = ? ORDER BY id",
            (identity.organization_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@router.post("/login")
def login(payload: LoginRequest) -> dict[str, str | int]:
    organization = payload.organization.strip()
    username = payload.username.strip()
    failure_key = (organization.casefold(), username.casefold())
    now = int(time.time())
    with get_connection() as conn:
        failure = conn.execute(
            "SELECT attempts, locked_until FROM pilot_login_failures WHERE organization_name = ? AND username = ?",
            failure_key,
        ).fetchone()
        if failure and failure["locked_until"] > now:
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
        row = conn.execute(
            """
            SELECT u.id, u.organization_id, u.username, u.role, u.password_hash, o.name AS organization
            FROM pilot_users u JOIN organizations o ON o.id = u.organization_id
            WHERE o.name = ? AND u.username = ? COLLATE NOCASE
            """,
            (organization, username),
        ).fetchone()
        # Use the same expensive check when the account does not exist.
        stored = row["password_hash"] if row else _DUMMY_HASH
        valid = _verify_password(payload.password, stored)
        if row is None or not valid:
            attempts = (failure["attempts"] if failure else 0) + 1
            locked_until = now + 15 * 60 if attempts >= 5 else 0
            conn.execute(
                """
                INSERT INTO pilot_login_failures (organization_name, username, attempts, locked_until)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (organization_name, username) DO UPDATE SET
                    attempts = excluded.attempts, locked_until = excluded.locked_until
                """,
                (*failure_key, attempts, locked_until),
            )
            conn.commit()
            raise HTTPException(status_code=401, detail="企业、用户名或密码错误")
        conn.execute(
            "DELETE FROM pilot_login_failures WHERE organization_name = ? AND username = ?",
            failure_key,
        )
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        conn.execute(
            "INSERT INTO pilot_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, row["id"], now + SESSION_SECONDS),
        )
    return {"token": token, "expires_in": SESSION_SECONDS, "organization": row["organization"], "username": row["username"], "role": row["role"]}


@router.get("/me")
def me(identity: Annotated[PilotIdentity, Depends(get_pilot_identity)]) -> dict[str, int | str | None]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT name FROM organizations WHERE id = ?", (identity.organization_id,)
        ).fetchone()
        user = conn.execute(
            "SELECT username FROM pilot_users WHERE id = ?", (identity.user_id,)
        ).fetchone() if identity.user_id else None
    return {"organization_id": identity.organization_id, "organization": row["name"] if row else None, "username": user["username"] if user else None, "role": identity.role}


@router.post("/logout", status_code=204)
def logout(
    identity: Annotated[PilotIdentity, Depends(get_pilot_identity)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if identity.user_id and authorization:
        _, _, token = authorization.partition(" ")
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM pilot_sessions WHERE token_hash = ? AND user_id = ?",
                (hashlib.sha256(token.encode()).hexdigest(), identity.user_id),
            )


_DUMMY_HASH = _password_hash("no-such-user-password")
