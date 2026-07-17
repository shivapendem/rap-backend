# error_logger.py
import traceback
from database import AsyncSessionLocal
from models import ProcessingError


async def log_db_error(
    stage: str,
    error: Exception,
    source_type: str = None,
    source_id: str = None,
    raw_payload: str = None,
    raw_email_id: str = None,
    requirement_id: str = None,
    consultant_id: str = None,
    context: dict = None,
):
    """Persist a DB/query failure so it's visible in the admin dashboard (processing_errors table).

    Uses traceback.format_exception(type(error), error, error.__traceback__)
    instead of traceback.format_exc() — format_exc() relies on the *currently
    active* exception context, which is no longer valid after an `await`
    hands control back to the event loop (as happens when this async function
    is called from an except block). Rebuilding the trace directly from the
    passed-in exception object works regardless of that context switch.
    """
    try:
        async with AsyncSessionLocal() as session:
            session.add(ProcessingError(
                source_type=source_type,
                source_id=str(source_id) if source_id else None,
                error_stage=stage,
                error_message=str(error),
                stack_trace="".join(traceback.format_exception(type(error), error, error.__traceback__)),
                raw_payload=raw_payload,
                status="OPEN",
                raw_email_id=str(raw_email_id) if raw_email_id else None,
                requirement_id=str(requirement_id) if requirement_id else None,
                consultant_id=str(consultant_id) if consultant_id else None,
                additional_context=context or {},
            ))
            await session.commit()
    except Exception as log_err:
        print(f"[error_logger] FAILED to log error: {log_err}")