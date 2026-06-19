# phase8_websocket.py
# ---------------------------------------------------------------------------
# Phase 8 — Admin Live WebSocket
# Endpoint: ws://server/api/v1/admin/live
# Events: NEW_APPLICATION, NEW_ERROR, AI_USAGE_UPDATED, AUDIT_CREATED, REVIEW_UPDATED
#
# Rewired to use your existing auth.py decode_access_token() instead of a
# separate Phase 8 JWT decoder — same SECRET_KEY, same token payload shape.
# ---------------------------------------------------------------------------

import json
import logging
import asyncio
from typing import Set
from datetime import datetime, timezone
from fastapi import WebSocket, WebSocketDisconnect, APIRouter

from auth import decode_access_token

logger = logging.getLogger("rap.websocket")

router = APIRouter()

_connections: Set[WebSocket] = set()


async def broadcast_event(event_type: str, payload: dict):
    """Called from services to push live events to all connected admin clients."""
    if not _connections:
        return
    message = json.dumps({
        "event": event_type,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    dead = set()
    for ws in list(_connections):
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _connections.discard(ws)


@router.websocket("/api/v1/admin/live")
async def admin_live(websocket: WebSocket):
    """
    WebSocket endpoint for admin dashboard.
    Client sends: {"token": "<JWT>"}
    Server sends events: NEW_APPLICATION, NEW_ERROR, AI_USAGE_UPDATED, AUDIT_CREATED, REVIEW_UPDATED
    """
    await websocket.accept()

    try:
        auth_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        data = json.loads(auth_msg)
        token = data.get("token", "")
        user = decode_access_token(token)
        if user.get("role") != "ADMIN":
            await websocket.send_text(json.dumps({"error": "Admin role required"}))
            await websocket.close(code=4003)
            return
    except Exception as e:
        await websocket.send_text(json.dumps({"error": f"Auth failed: {str(e)}"}))
        await websocket.close(code=4001)
        return

    _connections.add(websocket)
    logger.info(f"Admin WS connected: {user.get('sub')} — total={len(_connections)}")

    await websocket.send_text(json.dumps({
        "event": "CONNECTED",
        "payload": {"user": user.get("sub"), "connections": len(_connections)},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }))

    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if msg == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({
                    "event": "HEARTBEAT",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }))
    except WebSocketDisconnect:
        logger.info(f"Admin WS disconnected: {user.get('sub')}")
    finally:
        _connections.discard(websocket)