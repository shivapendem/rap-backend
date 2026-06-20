# phase8_retry_service.py
# ---------------------------------------------------------------------------
# Phase 8 — Error Retry Service
# ---------------------------------------------------------------------------

from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone

from models import ProcessingError


async def attempt_retry(db: AsyncSession, error: ProcessingError) -> dict:
    """
    Attempt to retry a failed processing step.
    This is a stub — wire in real retry logic (re-parse email, re-generate
    resume, re-send application, etc.) based on error.error_stage.
    """
    error.retry_count = (error.retry_count or 0) + 1
    error.last_retry_at = datetime.now(timezone.utc)

    # Placeholder retry outcome — replace with real stage-specific retry logic
    success = False
    message = f"Retry #{error.retry_count} attempted for stage '{error.error_stage}'. Manual intervention may be required."

    if success:
        error.status = "RESOLVED"
        error.resolved_at = datetime.now(timezone.utc)

    await db.flush()

    return {"success": success, "message": message, "retry_count": error.retry_count}