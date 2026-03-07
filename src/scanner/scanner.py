"""
Scanner — orchestrates Mobula stream + RugCheck + scoring.

Flow:
  MobulaClient streams tokens categorised as:
    soon_migrate (bonding) → PRIORITY 1
    migrated     (bonded)  → PRIORITY 2
    new_pair     (new)     → PRIORITY 3

  Each token fires concurrently (no queue).
  After Mobula data arrives → RugCheck enrichment → scoring → Discord alert.
"""

import asyncio
import logging
import time

from mobula.client import MobulaClient
from rugcheck.rugcheck import RugChecker
from filters.filters import TokenFilters
from scanner.scoring import RunnerScorer

logger = logging.getLogger("scanner")

ALERT_COOLDOWN = 3600   # 1 hour between re-alerts on same token
MAX_CONCURRENT = 50     # simultaneous deep-checks


class TokenScanner:
    def __init__(self):
        self.mobula  = MobulaClient()
        self.rug     = RugChecker()
        self.filters = TokenFilters()
        self.scorer  = RunnerScorer()

        self._alerted: dict[str, float] = {}
        self._in_flight: set[str] = set()
        self._alert_callbacks: list = []
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)

        # Wire callback
        self.mobula.on_token = self._on_token

    def add_alert_callback(self, cb):
        self._alert_callbacks.append(cb)

    def _already_alerted(self, mint: str) -> bool:
        ts = self._alerted.get(mint)
        return bool(ts and (time.time() - ts) < ALERT_COOLDOWN)

    def _mark_alerted(self, mint: str):
        self._alerted[mint] = time.time()
        now = time.time()
        self._alerted = {k: v for k, v in self._alerted.items()
                         if now - v < ALERT_COOLDOWN * 2}

    async def _on_token(self, token: dict, category: str):
        """Called by MobulaClient for every token update. Fire & forget."""
        mint = token.get("mint", "")
        if not mint or self._already_alerted(mint) or mint in self._in_flight:
            return
        self._in_flight.add(mint)
        asyncio.create_task(self._process(token, category))

    async def _process(self, token: dict, category: str):
        mint = token.get("mint", "")
        try:
            async with self._sem:
                await self._handle(token, category)
        except Exception as e:
            logger.error(f"Process error {mint[:8]}: {e}", exc_info=True)
        finally:
            self._in_flight.discard(mint)

    async def _handle(self, token: dict, category: str):
        mint = token.get("mint", "")
        if not mint or self._already_alerted(mint):
            return

        # ── 1. Filter check (pure data, no API) ───────────────────────────────
        classified = self.filters.classify(token)
        if classified is None:
            return
        token["filter_category"] = classified

        # ── 2. RugCheck (async, cached) ───────────────────────────────────────
        rug = await self.rug.check_token(mint)
        token["rug_status"]      = rug.get("status", "Unknown")
        token["rug_score"]       = rug.get("score", 0)
        token["rug_mintable"]    = rug.get("mintable", False)
        token["rug_freezable"]   = rug.get("freezable", False)
        token["rug_risks"]       = rug.get("risks", [])
        token["lp_locked"]       = rug.get("lp_locked", False)

        # RugCheck may provide better holder data than Mobula — merge if better
        rc_top10 = rug.get("top_holder_pct", 0.0)
        rc_dev   = rug.get("dev_holding_pct", 0.0)
        rc_ins   = rug.get("insider_pct", 0.0)
        if rc_top10 > 0 and token.get("top10_holders_pct", 0) == 0:
            token["top10_holders_pct"] = rc_top10
        if rc_dev > 0 and token.get("dev_holding_pct", 0) == 0:
            token["dev_holding_pct"] = rc_dev
        if rc_ins > 0 and token.get("insider_pct", 0) == 0:
            token["insider_pct"] = rc_ins

        # Hard block — danger rug
        if token["rug_status"] == "Danger":
            logger.debug(f"🚫 Danger rug: {token.get('name')} {mint[:8]}")
            return

        # ── 3. Post-rugcheck holder limits ────────────────────────────────────
        ok, reason = self.filters.passes_holder_limits(token, classified)
        if not ok:
            logger.debug(f"🚫 Holder limit: {token.get('name')} — {reason}")
            return

        # ── 4. Runner score ───────────────────────────────────────────────────
        passes, score, tier, reasons = self.scorer.passes(token)
        token["runner_score"]   = score
        token["runner_tier"]    = tier or ""
        token["runner_reasons"] = reasons

        if not passes:
            logger.debug(f"📉 Score {score}: {token.get('name')}")
            return

        # ── 5. Alert ──────────────────────────────────────────────────────────
        self._mark_alerted(mint)
        logger.info(
            f"🚨 {tier} | {token.get('name')} (${token.get('symbol')}) "
            f"score={score} cat={classified} "
            f"mc=${token.get('market_cap_usd',0):,.0f} "
            f"liq=${token.get('liquidity_usd',0):,.0f} "
            f"1h={token.get('price_change_1h',0):+.0f}% "
            f"holders={token.get('holders_count',0)}"
        )

        for cb in self._alert_callbacks:
            try:
                await cb(token)
            except Exception as e:
                logger.error(f"Alert callback error: {e}")

    async def start(self):
        logger.info("Starting scanner — Mobula Pulse V2 + RugCheck")
        logger.info("Priority: SOON_MIGRATE → MIGRATED → NEW_PAIR (all concurrent)")
        await self.mobula.start_stream()
