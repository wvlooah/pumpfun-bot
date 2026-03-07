import logging
import time
from typing import Literal

logger = logging.getLogger("filters")

# ── Filter Definitions ────────────────────────────────────────────────────────

FILTER_NEW_PAIRS = {
    "HIDE_MAYHEM": True,
    "MARKET_CAP": {"from": 3000, "to": None},
    "TOKEN_AGE_SECONDS": {"from": None, "to": 6},
    "LAUNCHPADS": ["bags", "bonk", "pumpfun"],
    "TOTAL_SOL_FEES": {"from": 0.1, "to": None},
}

FILTER_SOON_MIGRATE = {
    "HAS_SOCIALS": True,
    "HIDE_MAYHEM": True,
    "TOP10_HOLDERS_PCT": {"from": None, "to": 40},
    "TOP_INSIDER_PCT": {"from": None, "to": 40},
    "SNIPERS_PCT": {"from": None, "to": 40},
    "MARKET_CAP": {"from": 8000, "to": None},
    "TOKEN_AGE_SECONDS": {"from": None, "to": 30},
    "BUNDLES_PCT": {"from": None, "to": 50},
    "DEV_HOLDING_PCT": {"from": None, "to": 40},
    "LAUNCHPADS": ["bags", "bonk", "pumpfun"],
    "PRO_HOLDERS_COUNT": {"from": 20, "to": None},
    "TOTAL_SOL_FEES": {"from": 0.5, "to": None},
}

FILTER_MIGRATED = {
    "HIDE_MAYHEM": True,
    "TOP10_HOLDERS_PCT": {"from": None, "to": 40},
    "TOP_INSIDER_PCT": {"from": None, "to": 40},
    "SNIPERS_PCT": {"from": None, "to": 40},
    "MARKET_CAP": {"from": 10000, "to": None},
    "TOKEN_AGE_SECONDS": {"from": None, "to": 180},
    "BUNDLES_PCT": {"from": None, "to": 40},
    "DEV_HOLDING_PCT": {"from": None, "to": 40},
    "LAUNCHPADS": ["bags", "bonk", "pumpfun"],
    "PRO_HOLDERS_COUNT": {"from": 100, "to": None},
    "TOTAL_SOL_FEES": {"from": 3.5, "to": None},
}


def _in_range(value, range_def: dict) -> bool:
    """Check if value falls within {from, to} range (None = unbounded)."""
    if value is None:
        return False
    lo = range_def.get("from")
    hi = range_def.get("to")
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


class TokenFilters:
    """Apply filter sets to a normalised token dict and return the category or None."""

    @staticmethod
    def classify(token: dict) -> Literal["new_pair", "soon_migrate", "migrated"] | None:
        """
        Returns which filter category the token passes, or None if it passes none.
        Priority: migrated > soon_migrate > new_pair
        """
        age_s = token.get("age_seconds", 9999)
        market_cap = token.get("market_cap_usd", 0)
        top10_pct = token.get("top10_holders_pct", 100)
        insider_pct = token.get("insider_pct", 100)
        snipers_pct = token.get("snipers_pct", 100)
        bundles_pct = token.get("bundles_pct", 100)
        dev_pct = token.get("dev_holding_pct", 100)
        pro_holders = token.get("pro_holders_count", 0)
        sol_fees = token.get("total_sol_fees", 0)
        has_socials = bool(token.get("twitter") or token.get("website"))
        is_migrated = token.get("is_migrated", False)
        launchpad = token.get("launchpad", "pumpfun").lower()

        # Must be pump.fun family
        allowed_lps = ["pumpfun", "bags", "bonk"]
        if launchpad not in allowed_lps:
            return None

        # ── Migrated ──────────────────────────────────────────────────────────
        if is_migrated:
            f = FILTER_MIGRATED
            if (
                _in_range(market_cap, f["MARKET_CAP"])
                and _in_range(age_s, f["TOKEN_AGE_SECONDS"])
                and _in_range(top10_pct, f["TOP10_HOLDERS_PCT"])
                and _in_range(insider_pct, f["TOP_INSIDER_PCT"])
                and _in_range(snipers_pct, f["SNIPERS_PCT"])
                and _in_range(bundles_pct, f["BUNDLES_PCT"])
                and _in_range(dev_pct, f["DEV_HOLDING_PCT"])
                and _in_range(sol_fees, f["TOTAL_SOL_FEES"])
                and pro_holders >= f["PRO_HOLDERS_COUNT"]["from"]
            ):
                return "migrated"

        # ── Soon to Migrate ───────────────────────────────────────────────────
        f = FILTER_SOON_MIGRATE
        if (
            _in_range(market_cap, f["MARKET_CAP"])
            and _in_range(age_s, f["TOKEN_AGE_SECONDS"])
            and _in_range(top10_pct, f["TOP10_HOLDERS_PCT"])
            and _in_range(insider_pct, f["TOP_INSIDER_PCT"])
            and _in_range(snipers_pct, f["SNIPERS_PCT"])
            and _in_range(bundles_pct, f["BUNDLES_PCT"])
            and _in_range(dev_pct, f["DEV_HOLDING_PCT"])
            and _in_range(sol_fees, f["TOTAL_SOL_FEES"])
            and pro_holders >= f["PRO_HOLDERS_COUNT"]["from"]
            and (not f["HAS_SOCIALS"] or has_socials)
        ):
            return "soon_migrate"

        # ── New Pair ──────────────────────────────────────────────────────────
        f = FILTER_NEW_PAIRS
        if (
            _in_range(market_cap, f["MARKET_CAP"])
            and _in_range(age_s, f["TOKEN_AGE_SECONDS"])
            and _in_range(sol_fees, f["TOTAL_SOL_FEES"])
        ):
            return "new_pair"

        return None
