import logging
from typing import Literal

logger = logging.getLogger("filters")

# ── Filter Definitions ────────────────────────────────────────────────────────
# All ages in MINUTES. Market cap in USD. % values are percentages.

FILTER_NEW_PAIRS = {
    # From original: {"MARKET CAP":{"from":3000},"TOKEN AGE IN SECONDS":{"to":6},"TOTAL_SOL_FEES":{"from":0.1}}
    # "TOKEN AGE IN SECONDS" label is misleading — confirmed to mean MINUTES
    "MARKET_CAP_USD": {"from": 3000, "to": None},
    "TOKEN_AGE_MINUTES": {"from": None, "to": 6},
    "TOTAL_SOL_FEES": {"from": 0.1, "to": None},
    "LAUNCHPADS": ["bags", "bonk", "pumpfun"],
}

FILTER_SOON_MIGRATE = {
    # From original: mc≥8000, age≤30min, socials required,
    # top10≤40%, insiders≤40%, snipers≤40%, bundles≤50%, dev≤40%, pro_holders≥20, sol≥0.5
    "HAS_SOCIALS": True,
    "MARKET_CAP_USD": {"from": 8000, "to": None},
    "TOKEN_AGE_MINUTES": {"from": None, "to": 30},
    "TOP10_HOLDERS_PCT": {"from": None, "to": 40},
    "INSIDER_PCT": {"from": None, "to": 40},
    "SNIPERS_PCT": {"from": None, "to": 40},
    "BUNDLES_PCT": {"from": None, "to": 50},
    "DEV_HOLDING_PCT": {"from": None, "to": 40},
    "PRO_HOLDERS_COUNT": {"from": 20, "to": None},
    "TOTAL_SOL_FEES": {"from": 0.5, "to": None},
    "LAUNCHPADS": ["bags", "bonk", "pumpfun"],
}

FILTER_MIGRATED = {
    # From original: mc≥10000, age≤180min, top10≤40%, insiders≤40%,
    # snipers≤40%, bundles≤40%, dev≤40%, pro_holders≥100, sol≥3.5
    "MARKET_CAP_USD": {"from": 10000, "to": None},
    "TOKEN_AGE_MINUTES": {"from": None, "to": 180},
    "TOP10_HOLDERS_PCT": {"from": None, "to": 40},
    "INSIDER_PCT": {"from": None, "to": 40},
    "SNIPERS_PCT": {"from": None, "to": 40},
    "BUNDLES_PCT": {"from": None, "to": 40},
    "DEV_HOLDING_PCT": {"from": None, "to": 40},
    "PRO_HOLDERS_COUNT": {"from": 100, "to": None},
    "TOTAL_SOL_FEES": {"from": 3.5, "to": None},
    "LAUNCHPADS": ["bags", "bonk", "pumpfun"],
}


def _in_range(value, range_def: dict) -> bool:
    """Check value against {from, to} bounds. None = unbounded."""
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

    @staticmethod
    def classify(token: dict) -> Literal["new_pair", "soon_migrate", "migrated"] | None:
        """
        Returns the first filter category the token passes, or None.
        Priority: migrated > soon_migrate > new_pair
        Age is stamped at receipt time so processing delay doesn't disqualify tokens.
        """
        # Age in MINUTES — use receipt time stamp set the moment PumpPortal fires
        receipt_age_min = token.get("receipt_age_minutes", token.get("age_seconds", 99999) / 60)

        market_cap_usd = token.get("market_cap_usd", 0)
        top10_pct      = token.get("top10_holders_pct", 0)
        insider_pct    = token.get("insider_pct", 0)
        snipers_pct    = token.get("snipers_pct", 0)
        bundles_pct    = token.get("bundles_pct", 0)
        dev_pct        = token.get("dev_holding_pct", 0)
        pro_holders    = token.get("pro_holders_count", 0)
        sol_fees       = token.get("total_sol_fees", 0)
        has_socials    = bool(token.get("twitter") or token.get("website"))
        is_migrated    = token.get("is_migrated", False)
        launchpad      = token.get("launchpad", "pumpfun").lower()

        if launchpad not in ["pumpfun", "bags", "bonk"]:
            logger.debug(f"❌ Wrong launchpad: {launchpad}")
            return None

        # ── Soon to Migrate (FIRST PRIORITY) ─────────────────────────────────
        f = FILTER_SOON_MIGRATE
        if (
            _in_range(market_cap_usd, f["MARKET_CAP_USD"])
            and _in_range(receipt_age_min, f["TOKEN_AGE_MINUTES"])
            and _in_range(top10_pct, f["TOP10_HOLDERS_PCT"])
            and _in_range(insider_pct, f["INSIDER_PCT"])
            and _in_range(snipers_pct, f["SNIPERS_PCT"])
            and _in_range(bundles_pct, f["BUNDLES_PCT"])
            and _in_range(dev_pct, f["DEV_HOLDING_PCT"])
            and _in_range(sol_fees, f["TOTAL_SOL_FEES"])
            and pro_holders >= f["PRO_HOLDERS_COUNT"]["from"]
            and (not f["HAS_SOCIALS"] or has_socials)
        ):
            logger.info(f"✅ SOON_MIGRATE: {token.get('name')} mc=${market_cap_usd:,.0f} age={receipt_age_min:.1f}m")
            return "soon_migrate"

        # ── Migrated (SECOND PRIORITY) ────────────────────────────────────────
        if is_migrated:
            f = FILTER_MIGRATED
            if (
                _in_range(market_cap_usd, f["MARKET_CAP_USD"])
                and _in_range(receipt_age_min, f["TOKEN_AGE_MINUTES"])
                and _in_range(top10_pct, f["TOP10_HOLDERS_PCT"])
                and _in_range(insider_pct, f["INSIDER_PCT"])
                and _in_range(snipers_pct, f["SNIPERS_PCT"])
                and _in_range(bundles_pct, f["BUNDLES_PCT"])
                and _in_range(dev_pct, f["DEV_HOLDING_PCT"])
                and _in_range(sol_fees, f["TOTAL_SOL_FEES"])
                and pro_holders >= f["PRO_HOLDERS_COUNT"]["from"]
            ):
                logger.info(f"✅ MIGRATED: {token.get('name')} mc=${market_cap_usd:,.0f} age={receipt_age_min:.1f}m")
                return "migrated"

        # ── New Pair (THIRD PRIORITY) ─────────────────────────────────────────
        f = FILTER_NEW_PAIRS
        if (
            _in_range(market_cap_usd, f["MARKET_CAP_USD"])
            and _in_range(receipt_age_min, f["TOKEN_AGE_MINUTES"])
            and _in_range(sol_fees, f["TOTAL_SOL_FEES"])
        ):
            logger.info(f"✅ NEW_PAIR: {token.get('name')} mc=${market_cap_usd:,.0f} age={receipt_age_min:.1f}m sol={sol_fees:.3f}")
            return "new_pair"

        logger.debug(
            f"❌ No match: {token.get('name','?')} "
            f"mc=${market_cap_usd:,.0f} age={receipt_age_min:.1f}m "
            f"sol={sol_fees:.3f} migrated={is_migrated}"
        )
        return None
