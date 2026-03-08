"""
Filters — soon_migrate and migrated only. New pairs completely removed.

Pre-rugcheck: MC, age, SOL fees, socials, is_migrated
Post-rugcheck: holder limits applied after rug data arrives
"""
import logging
from typing import Literal

logger = logging.getLogger("filters")

FILTER_SOON_MIGRATE = {
    "MARKET_CAP_USD":    {"from": 8_000,  "to": None},
    "TOKEN_AGE_MINUTES": {"from": None,   "to": 30},
    "TOTAL_SOL_FEES":    {"from": 0.5,    "to": None},
    "HAS_SOCIALS":       True,
}

FILTER_MIGRATED = {
    "MARKET_CAP_USD":    {"from": 10_000, "to": None},
    "TOKEN_AGE_MINUTES": {"from": None,   "to": 180},
    "TOTAL_SOL_FEES":    {"from": 3.5,    "to": None},
}

HOLDER_LIMITS = {
    "soon_migrate": {"top10": 40, "insider": 40, "dev": 40},
    "migrated":     {"top10": 40, "insider": 40, "dev": 40},
}


def _in_range(value, rng: dict) -> bool:
    if value is None: return False
    lo = rng.get("from")
    hi = rng.get("to")
    if lo is not None and value < lo: return False
    if hi is not None and value > hi: return False
    return True


class TokenFilters:

    @staticmethod
    def classify(token: dict) -> Literal["soon_migrate", "migrated"] | None:
        age_min     = token.get("receipt_age_minutes") or (token.get("age_seconds", 0) / 60)
        mc_usd      = token.get("market_cap_usd", 0) or 0
        sol_fees    = token.get("total_sol_fees", 0) or 0
        has_socials = bool(token.get("twitter") or token.get("website"))
        is_migrated = token.get("is_migrated", False)

        # ── 1. Soon to Migrate ────────────────────────────────────────────────
        f = FILTER_SOON_MIGRATE
        if (
            _in_range(mc_usd, f["MARKET_CAP_USD"])
            and _in_range(age_min, f["TOKEN_AGE_MINUTES"])
            and _in_range(sol_fees, f["TOTAL_SOL_FEES"])
            and has_socials
        ):
            logger.info(f"✅ SOON_MIGRATE: {token.get('name')} mc=${mc_usd:,.0f} age={age_min:.1f}m sol={sol_fees:.2f}")
            return "soon_migrate"

        # ── 2. Migrated ───────────────────────────────────────────────────────
        f = FILTER_MIGRATED
        if (
            is_migrated
            and _in_range(mc_usd, f["MARKET_CAP_USD"])
            and _in_range(age_min, f["TOKEN_AGE_MINUTES"])
            and _in_range(sol_fees, f["TOTAL_SOL_FEES"])
        ):
            logger.info(f"✅ MIGRATED: {token.get('name')} mc=${mc_usd:,.0f} age={age_min:.1f}m sol={sol_fees:.2f}")
            return "migrated"

        logger.debug(
            f"❌ No match: {token.get('name','?')} "
            f"mc=${mc_usd:,.0f} age={age_min:.1f}m sol={sol_fees:.3f} migrated={is_migrated}"
        )
        return None

    @staticmethod
    def passes_holder_limits(token: dict, category: str) -> tuple[bool, str]:
        limits  = HOLDER_LIMITS.get(category, HOLDER_LIMITS["soon_migrate"])
        top10   = token.get("top10_holders_pct", 0) or 0
        insider = token.get("insider_pct", 0) or 0
        dev     = token.get("dev_holding_pct", 0) or 0
        if top10   > 0 and top10   > limits["top10"]:   return False, f"Top10 {top10:.0f}% > {limits['top10']}%"
        if insider > 0 and insider > limits["insider"]: return False, f"Insiders {insider:.0f}% > {limits['insider']}%"
        if dev     > 0 and dev     > limits["dev"]:     return False, f"Dev {dev:.0f}% > {limits['dev']}%"
        return True, "ok"
