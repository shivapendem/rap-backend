# phase8_ai_usage_service.py
# ---------------------------------------------------------------------------
# Phase 8 — AI Usage / Cost Tracking Service
# ---------------------------------------------------------------------------

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models import AppSetting

DEFAULT_BUDGET_USD = 200.0

# Rough per-1K-token pricing — adjust to match your actual AI provider
MODEL_PRICING = {
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
}
DEFAULT_PRICING = {"input": 0.005, "output": 0.015}

async def set_budget_threshold(db: AsyncSession, budget_usd: float, updated_by: str | None = None) -> float:
    """Create or update the ai_budget_usd row in app_settings."""
    result = await db.execute(select(AppSetting).where(AppSetting.key == "ai_budget_usd"))
    setting = result.scalars().first()
    if setting:
        setting.value = str(budget_usd)
        setting.updated_by = updated_by
    else:
        db.add(AppSetting(key="ai_budget_usd", value=str(budget_usd), updated_by=updated_by))
    await db.commit()
    return budget_usd

async def get_budget_threshold(db: AsyncSession) -> float:
    """Fetch the AI budget threshold from app_settings, falling back to default."""
    result = await db.execute(select(AppSetting).where(AppSetting.key == "ai_budget_usd"))
    setting = result.scalars().first()
    if setting:
        try:
            return float(setting.value)
        except (TypeError, ValueError):
            pass
    return DEFAULT_BUDGET_USD

async def set_budget_threshold(db: AsyncSession, budget_usd: float, updated_by: str | None = None) -> float:
    """Create or update the ai_budget_usd row in app_settings."""
    result = await db.execute(select(AppSetting).where(AppSetting.key == "ai_budget_usd"))
    setting = result.scalars().first()
    if setting:
        setting.value = str(budget_usd)
        setting.updated_by = updated_by
    else:
        db.add(AppSetting(key="ai_budget_usd", value=str(budget_usd), updated_by=updated_by))
    await db.commit()
    return budget_usd

def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for an AI call based on model + token counts."""
    pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
    cost = (input_tokens / 1000.0) * pricing["input"] + (output_tokens / 1000.0) * pricing["output"]
    return round(cost, 6)