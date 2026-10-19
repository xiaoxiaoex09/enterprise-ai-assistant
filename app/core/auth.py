"""Shared API-key identity for pilot organization endpoints."""

import json
import os
import secrets
from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, ValidationError


pilot_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class PilotIdentity(BaseModel):
    organization_id: int = Field(gt=0)
    role: Literal["engineer", "admin"]


def get_pilot_identity(
    api_key: Annotated[str | None, Depends(pilot_api_key_header)],
) -> PilotIdentity:
    """Resolve a pilot API key to its server-controlled organization."""
    raw_keys = os.getenv("PILOT_API_KEYS")
    if not raw_keys:
        raise HTTPException(status_code=503, detail="试点访问凭据尚未配置")
    try:
        entries = json.loads(raw_keys)
        if not isinstance(entries, dict) or not entries:
            raise ValueError("empty or invalid key map")
        identities = []
        for secret, value in entries.items():
            if not isinstance(secret, str) or len(secret) < 32:
                raise ValueError("API key must have at least 32 characters")
            identities.append((secret, PilotIdentity.model_validate(value)))
    except (ValueError, TypeError, ValidationError) as error:
        raise HTTPException(status_code=503, detail="试点访问凭据配置无效") from error

    if not api_key:
        raise HTTPException(status_code=401, detail="缺少API Key")
    identity = None
    for secret, candidate in identities:
        if secrets.compare_digest(api_key, secret):
            identity = candidate
    if identity is None:
        raise HTTPException(status_code=401, detail="API Key无效")
    return identity


def get_pilot_admin(
    identity: Annotated[PilotIdentity, Depends(get_pilot_identity)],
) -> PilotIdentity:
    if identity.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return identity
