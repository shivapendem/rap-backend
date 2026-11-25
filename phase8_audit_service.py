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
    if not metadata or not isinstance(metadata, dict):
        return ""
    if "summary" in metadata:
        return str(metadata["summary"])
    if "description" in metadata:
        return str(metadata["description"])
    if "new_status" in metadata:
        return f"Status changed to {metadata.get('new_status')}"
    if "role" in metadata and "email" in metadata:
        return f"Role: {metadata.get('role')}, Email: {metadata.get('email')}"
    raw = json.dumps(metadata)
    return raw[:100] + "..." if len(raw) > 100 else raw

# ---------------------------------------------------------------------------
# Readable audit rows (admin dashboard "Recent activity" + Audit Logs page).
# Turns {"consultant_ids": [...]} / {"before":..,"after":..} etc. into
# "Ravikumar N assigned 8 consultants to Jashwanth M" with a Field /
# Before / After list. Names are looked up in ONE query per table for the
# whole page of rows. Never raises: any row it can't describe keeps the old
# metadata preview text.
# ---------------------------------------------------------------------------

_ROLE_LABEL = {"ADMIN": "Admin", "RECRUITER": "Recruiter", "CONSULTANT": "Consultant"}
_USER_FIELD_LABEL = {"full_name": "Name", "email": "Email", "role": "Role", "is_authorized": "Access"}
_ACTION_TAG = {
    "USER_CREATED": "User Created",
    "USER_UPDATED": "User Updated",
    "USER_DELETED": "User Deleted",
    "USER_STATUS_CHANGED": "Status Changed",
    "USER_LOGIN": "Login",
    "CONSULTANT_ASSIGNED": "Consultants Assigned",
    "REVIEW_APPROVED": "Review Approved",
    "REVIEW_REJECTED": "Review Rejected",
    "APPLICATION_SENT": "Profile Sent",
}


def _fmt_val(field: str, v) -> str:
    if v is None or v == "":
        return "—"
    if field == "role":
        return _ROLE_LABEL.get(str(v).upper(), str(v).title())
    if field == "is_authorized":
        return "Active" if v else "Inactive"
    return str(v)


def _names_list(names: list, shown: int = 2) -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    head = ", ".join(_short(n) for n in names[:shown])
    return head + (f", +{len(names) - shown}" if len(names) > shown else "")


def _short(name: str) -> str:
    # "Surya Prakash Reddy" -> "Surya P."
    parts = (name or "").split()
    return f"{parts[0]} {parts[1][0]}." if len(parts) >= 2 else (name or "")


async def describe_audit_rows(db: AsyncSession, rows) -> dict:
    """{row.id: {actor_display, verb, target, extra, tag, changes, sentence}}"""
    from sqlalchemy import select, func
    from models import User, Consultant

    out: dict = {}
    if not rows:
        return out

    # ---- collect ids / emails to look up -------------------------------
    user_ids, cons_ids, emails = set(), set(), set()

    def _int(v):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None

    for r in rows:
        meta = r.meta if isinstance(r.meta, dict) else {}
        for v in (r.actor_user_id, r.actor_name):
            if v and "@" in str(v):
                emails.add(str(v).strip().lower())
            elif _int(v) is not None:
                user_ids.add(_int(v))
        et = (r.entity_type or "").lower()
        if et == "user" and _int(r.entity_id) is not None:
            user_ids.add(_int(r.entity_id))
        if et == "consultant" and _int(r.entity_id) is not None:
            cons_ids.add(_int(r.entity_id))
        if _int(meta.get("recruiter_id")) is not None:
            user_ids.add(_int(meta.get("recruiter_id")))
        for cid in meta.get("consultant_ids") or []:
            if _int(cid) is not None:
                cons_ids.add(_int(cid))

    users_by_id, users_by_email, cons_by_id = {}, {}, {}
    try:
        if user_ids or emails:
            conds = []
            if user_ids:
                conds.append(User.id.in_(user_ids))
            if emails:
                conds.append(func.lower(User.email).in_(emails))
            from sqlalchemy import or_
            for uid, name, email, role in (await db.execute(
                select(User.id, User.full_name, User.email, User.role).where(or_(*conds))
            )).all():
                rec = {"name": (name or "").strip() or (email or "").split("@")[0], "role": role}
                users_by_id[uid] = rec
                if email:
                    users_by_email[email.strip().lower()] = rec
        if cons_ids:
            for cid, cname, uname, uemail in (await db.execute(
                select(Consultant.id, Consultant.full_name, User.full_name, User.email)
                .outerjoin(User, User.id == Consultant.user_id)
                .where(Consultant.id.in_(cons_ids))
            )).all():
                cons_by_id[cid] = (cname or uname or (uemail or "").split("@")[0] or f"Consultant #{cid}").strip()
    except Exception as e:  # lookups are best-effort
        logger.warning("audit describe lookups failed: %s", e)

    def actor_of(r) -> str:
        for v in (r.actor_name, r.actor_user_id):
            if not v:
                continue
            s = str(v).strip()
            if "@" in s and s.lower() in users_by_email:
                return users_by_email[s.lower()]["name"]
            if _int(s) is not None and _int(s) in users_by_id:
                return users_by_id[_int(s)]["name"]
        name = str(r.actor_name or "System")
        return name.split("@")[0] if "@" in name else name

    def user_of(uid, meta=None):
        rec = users_by_id.get(_int(uid))
        if rec:
            return rec["name"], _ROLE_LABEL.get((rec["role"] or "").upper(), "")
        meta = meta or {}
        em = meta.get("email") or ""
        return (em.split("@")[0] if em else f"User #{uid}"), _ROLE_LABEL.get(str(meta.get("role") or "").upper(), "")

    for r in rows:
        try:
            meta = r.meta if isinstance(r.meta, dict) else {}
            a = (r.action or "").upper()
            et = (r.entity_type or "").lower()
            d = {"actor_display": actor_of(r), "verb": "", "target": "", "extra": "",
                 "tag": _ACTION_TAG.get(a, a.replace("_", " ").title()), "changes": []}

            if et == "consultant":
                tgt, role = cons_by_id.get(_int(r.entity_id), f"Consultant #{r.entity_id}"), "Consultant"
            elif et == "user":
                tgt, role = user_of(r.entity_id, meta)
            else:
                tgt, role = (f"#{r.entity_id}" if r.entity_id else ""), ""

            if a == "USER_STATUS_CHANGED":
                new, old = str(meta.get("new_status") or "").upper(), str(meta.get("old_status") or "").upper()
                d["verb"] = {"INACTIVE": "deactivated", "ACTIVE": "reactivated" if old == "INACTIVE" else "activated"}.get(
                    new, f"changed the status of")
                d["target"] = tgt
                d["extra"] = f" · {role}" if role and new in ("ACTIVE", "INACTIVE") else (f" to {new.title()}" if new else "")
                d["changes"] = [{"field": "Status", "before": old.title() or "—", "after": new.title() or "—"}]
            elif a == "USER_CREATED":
                d["verb"], d["target"] = "added", tgt
                r_label = _fmt_val("role", meta.get("role")) if meta.get("role") else role
                d["extra"] = f" · {r_label}" if r_label and r_label != "—" else ""
                d["changes"] = [{"field": "Role", "before": "—", "after": r_label or "—"},
                                {"field": "Email", "before": "—", "after": meta.get("email") or "—"}]
            elif a == "USER_DELETED":
                d["verb"], d["target"] = "deleted", (meta.get("email") or tgt)
                d["extra"] = f" · {_fmt_val('role', meta.get('role'))}" if meta.get("role") else ""
            elif a == "USER_UPDATED" and isinstance(meta.get("after"), dict):
                before, after = meta.get("before") or {}, meta["after"]
                for k, label in _USER_FIELD_LABEL.items():
                    if k in after and before.get(k) != after.get(k):
                        d["changes"].append({"field": label, "before": _fmt_val(k, before.get(k)), "after": _fmt_val(k, after.get(k))})
                d["verb"], d["target"] = "updated", tgt
                if len(d["changes"]) == 1:
                    c = d["changes"][0]
                    d["extra"] = f" · {c['field']}: {c['before']} → {c['after']}"
                elif d["changes"]:
                    d["extra"] = " · " + ", ".join(c["field"] for c in d["changes"])
                else:
                    d["extra"] = " · no field changes"
            elif a == "USER_UPDATED" and meta.get("type") == "consultant_profile_update":
                d["verb"], d["target"], d["tag"] = "updated the profile of", tgt, "Profile Updated"
            elif a == "USER_UPDATED" and meta.get("type") == "consultant_resume_rich_text_update":
                d["verb"], d["target"], d["tag"] = "updated the resume template of", tgt, "Profile Updated"
            elif a == "CONSULTANT_ASSIGNED" and meta.get("consultant_ids") is not None:
                names = [cons_by_id.get(_int(c), f"#{c}") for c in meta.get("consultant_ids") or []]
                n = len(names)
                d["verb"] = f"assigned {n} consultant{'s' if n != 1 else ''} to" if n else "removed all consultants from"
                d["target"] = tgt
                d["extra"] = f" · {_names_list(names)}" if names else ""
                d["changes"] = [{"field": "Consultants", "before": "—", "after": ", ".join(names) or "None"}]
            elif a == "CONSULTANT_ASSIGNED" and meta.get("recruiter_id"):
                rec_name, _ = user_of(meta.get("recruiter_id"))
                if meta.get("action") == "unassigned":
                    d["verb"], d["target"], d["extra"], d["tag"] = "unassigned", tgt, f" from {rec_name}", "Consultant Unassigned"
                    d["changes"] = [{"field": "Recruiter", "before": rec_name, "after": "—"}]
                else:
                    d["verb"], d["target"], d["extra"] = "assigned", tgt, f" to {rec_name}"
                    d["changes"] = [{"field": "Recruiter", "before": "—", "after": rec_name}]
            elif a == "USER_LOGIN":
                d["verb"] = "signed in"
            elif a in ("REVIEW_APPROVED", "REVIEW_REJECTED"):
                d["verb"] = "approved review" if a == "REVIEW_APPROVED" else "rejected review"
                d["target"] = f"#{r.entity_id}" if r.entity_id else ""
            else:
                d["verb"] = (a.replace("_", " ").lower() + (" for" if tgt else "")).strip()
                d["target"] = tgt
                prev = build_metadata_preview(meta)
                d["extra"] = f" · {prev}" if prev and not prev.startswith("{") else ""

            d["sentence"] = " ".join(p for p in (d["actor_display"], d["verb"], d["target"]) if p) + d["extra"]
            out[r.id] = d
        except Exception as e:
            logger.warning("audit describe failed for row %s: %s", getattr(r, "id", "?"), e)
    return out