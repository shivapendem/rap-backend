# phase8_audit_service.py
# ---------------------------------------------------------------------------
# Phase 8 — Audit Logging Service
# Logs: USER_LOGIN, USER_LOGOUT, RESUME_GENERATED, APPLICATION_CREATED,
# APPLICATION_SENT, GMAIL_CONNECTED, PERMISSION_DENIED, ADMIN_ACTION,
# ERROR_RETRY, REVIEW_APPROVED, REVIEW_REJECTED + general actions
# ---------------------------------------------------------------------------

import json
import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from models import AuditLog

logger = logging.getLogger("rap.audit")

VALID_ACTIONS = {
    "USER_LOGIN", "USER_LOGOUT", "RESUME_GENERATED", "APPLICATION_CREATED",
    "APPLICATION_SENT", "GMAIL_CONNECTED", "PERMISSION_DENIED", "ADMIN_ACTION",
    "ERROR_RETRY", "REVIEW_APPROVED", "REVIEW_REJECTED",
    "LOGIN", "LOGOUT", "GENERATE", "SEND", "CONNECT", "DISCONNECT",
    "MATCH", "ERROR", "PARSE", "ARCHIVE", "BUDGET_ALERT",
}


async def log_action(
    db: AsyncSession,
    action: str,
    *,
    actor_user_id: Optional[str] = None,
    actor_name: str = "System",
    actor_role: str = "System",
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> AuditLog:
    entry = AuditLog(
        actor_user_id=str(actor_user_id) if actor_user_id else None,
        actor_name=actor_name,
        actor_role=actor_role,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else None,
        meta=metadata or {},
        ip_address=ip_address,
        user_agent=user_agent[:500] if user_agent else None,
        request_id=request_id,
    )
    db.add(entry)
    await db.flush()

    # Also broadcast via WebSocket (best-effort, never blocks audit write)
    try:
        from phase8_websocket import broadcast_event
        await broadcast_event("AUDIT_CREATED", {
            "action": action,
            "actor": actor_name,
            "entity_type": entity_type,
            "entity_id": entity_id,
        })
    except Exception:
        pass

    return entry


def build_metadata_preview(metadata: dict) -> str:
    raw = json.dumps(metadata or {})
    return raw[:60] + "..." if len(raw) > 60 else raw