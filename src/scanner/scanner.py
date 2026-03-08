"""
Scanner — full pipeline with persistent momentum watching.

Tokens are NEVER discarded. If they don't pass on first scan,
they stay in _watching and re-trigger a full deep scan whenever
the momentum spike detector fires for that mint.

Pipeline per token:
  1. PumpPortal event (migration or momentum spike)
  2. Subscribe trades + Mobula + RugCheck (concurrent)
  3. Merge all data
  4. Filter (soon_migrate | migrated)
  5. Holder limits check
  6. Score
  7. Alert (with rule-based narrative)
"""

import asyncio
import logging
import time

from pumpfun.api import PumpPortalClient, get_sol_price
from mobula.client import MobulaClient
from rugcheck.rugcheck import RugChecker
from filters.filters import TokenFilters
from scanner.scoring import RunnerScorer
from scanner.narrative import build_narrative

logger = logging.getLogger("scanner")

ALERT_COOLDOWN = 3600
TRADE_WINDOW   = 5.0
MAX_CONCURRENT = 40


class TokenScanner:
    def __init__(self):
        self.pump    = PumpPortalClient()
        self.mobula  = MobulaClient()
        self.rug     = RugChecker()
        self.filters = TokenFilters()
        self.scorer  = RunnerScorer()

        self._alerted: dict[str, float] = {}
        self._in_flight: set[str] = set()
        self._alert_callbacks: list = []
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)

        # All tokens ever seen — never discarded
        # mint -> base token dict (refreshed on each re-scan)
        self._watching: dict[str, dict] = {}

        self.pump.on_migration      = self._on_migration
        self.pump.on_momentum_spike = self._on_spike

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

    def _fire(self, event_type: str, data: dict):
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint) or mint in self._in_flight:
            return
        self._in_flight.add(mint)
        asyncio.create_task(self._process(event_type, data))

    async def _on_migration(self, data: dict):
        data["_receipt_time"] = time.time()
        logger.info(f"🔀 Migration: {data.get('name','?')} ({data.get('mint','')[:8]}...)")
        self._fire("migrate", data)

    async def _on_spike(self, data: dict):
        mint = data.get("mint", "")
        # If we've seen this token before, merge stored data with spike data
        if mint in self._watching:
            merged = dict(self._watching[mint])
            merged.update({k: v for k, v in data.items() if v})
            data = merged
        logger.info(f"⚡ Re-scan spike: {data.get('name','?')} ({mint[:8]}...)")
        self._fire("spike", data)

    async def _process(self, event_type: str, data: dict):
        mint = data.get("mint", "")
        try:
            async with self._sem:
                await self._handle(event_type, data)
        except Exception as e:
            logger.error(f"Process error {mint[:8]}: {e}", exc_info=True)
        finally:
            self._in_flight.discard(mint)

    async def _handle(self, event_type: str, data: dict):
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint):
            return

        receipt_time = data.get("_receipt_time", time.time())

        # ── 1. Build base token ────────────────────────────────────────────────
        if event_type == "migrate":
            token = self.pump.parse_migration(data)
        else:
            token = self.pump.parse_spike(data)

        # Store in watching (never discard)
        self._watching[mint] = token

        # ── 2. Concurrent enrichment ──────────────────────────────────────────
        trade_task  = asyncio.create_task(self._collect_trades(mint, event_type))
        mobula_task = asyncio.create_task(self.mobula.enrich_token(mint))
        rug_task    = asyncio.create_task(self.rug.check_token(mint))

        snap, mobula_data, rug = await asyncio.gather(
            trade_task, mobula_task, rug_task,
            return_exceptions=True,
        )

        # ── 3. Merge trade data ────────────────────────────────────────────────
        if isinstance(snap, dict) and snap:
            if snap.get("tx_buys_5m", 0) + snap.get("tx_sells_5m", 0) > 0:
                token["tx_buys_5m"]       = snap["tx_buys_5m"]
                token["tx_sells_5m"]      = snap["tx_sells_5m"]
                token["volume_5m_usd"]    = snap["volume_5m_usd"]
            if snap.get("market_cap_usd", 0) > token.get("market_cap_usd", 0):
                token["market_cap_usd"]   = snap["market_cap_usd"]
                token["market_cap_sol"]   = snap["market_cap_sol"]
            token["repeat_buy_signal"]    = snap.get("repeat_buy_signal", False)
            token["price_floor_held"]     = snap.get("price_floor_held", False)
            token["bonding_fill_rate"]    = snap.get("bonding_fill_rate", 0.0)

        # ── 4. Merge Mobula data ──────────────────────────────────────────────
        if isinstance(mobula_data, dict) and mobula_data:
            token.update(mobula_data)

        # ── 5. Merge RugCheck data ────────────────────────────────────────────
        if isinstance(rug, dict):
            token["rug_status"]         = rug["status"]
            token["rug_score"]          = rug["score"]
            token["rug_mintable"]       = rug["mintable"]
            token["rug_freezable"]      = rug["freezable"]
            token["rug_risks"]          = rug["risks"]
            token["lp_locked"]          = rug["lp_locked"]
            token["lp_lock_days"]       = rug["lp_lock_days"]
            token["metadata_mutable"]   = rug["metadata_mutable"]
            token["transfer_fee_pct"]   = rug["transfer_fee_pct"]
            token["verified"]           = rug["verified"]
            token["top10_holders_pct"]  = rug["top10_holders_pct"]
            token["dev_holding_pct"]    = rug["dev_holding_pct"]
            token["insider_pct"]        = rug["insider_pct"]
            if rug.get("total_liquidity_usd", 0) > 0 and not token.get("liquidity_usd"):
                token["liquidity_usd"]  = rug["total_liquidity_usd"]
        else:
            token["rug_status"] = "Unknown"
            token["lp_locked"]  = False
            token["lp_lock_days"] = 0

        # Update age
        token["age_seconds"]         = time.time() - token.get("created_at", time.time())
        token["receipt_age_minutes"] = token["age_seconds"] / 60

        # Update stored state
        self._watching[mint] = dict(token)

        # Hard block — confirmed danger
        if token.get("rug_status") == "Danger":
            logger.debug(f"🚫 Danger rug: {token.get('name')} {mint[:8]}")
            return

        # ── 6. Filter ─────────────────────────────────────────────────────────
        category = self.filters.classify(token)
        if category is None:
            logger.debug(f"📋 Watching (no match yet): {token.get('name')} {mint[:8]}")
            return
        token["filter_category"] = category

        # ── 7. Holder limits ──────────────────────────────────────────────────
        ok, reason = self.filters.passes_holder_limits(token, category)
        if not ok:
            logger.debug(f"🚫 Holder limit: {token.get('name')} — {reason}")
            return

        # ── 8. Score ──────────────────────────────────────────────────────────
        passes, score, tier, reasons = self.scorer.passes(token)
        token["runner_score"]   = score
        token["runner_tier"]    = tier or ""
        token["runner_reasons"] = reasons

        # ── 9. Narrative ──────────────────────────────────────────────────────
        token["narrative"] = build_narrative(token)

        if not passes:
            logger.debug(f"📉 Score {score}: {token.get('name')} | {' · '.join(reasons[:3])}")
            return

        # ── 10. Alert ─────────────────────────────────────────────────────────
        self._mark_alerted(mint)
        logger.info(
            f"🚨 {tier} | {token.get('name')} (${token.get('symbol')}) "
            f"score={score} cat={category} src={event_type} "
            f"mc=${token.get('market_cap_usd',0):,.0f} "
            f"liq=${token.get('liquidity_usd',0):,.0f} "
            f"1h={token.get('price_change_1h',0):+.1f}% "
            f"top10={token.get('top10_holders_pct',0):.1f}% "
            f"dev={token.get('dev_holding_pct',0):.1f}% "
            f"rug={token['rug_status']} lp={token['lp_locked']}({token['lp_lock_days']}d) "
            f"mutable={token['metadata_mutable']} fee={token['transfer_fee_pct']:.1f}%"
        )

        for cb in self._alert_callbacks:
            try:
                await cb(token)
            except Exception as e:
                logger.error(f"Alert callback error: {e}")

    async def _collect_trades(self, mint: str, event_type: str) -> dict:
        if event_type == "spike":
            # For spikes, trade data already collected by momentum detector
            return self.pump.get_trade_snapshot(mint)
        await self.pump.subscribe_trades(mint)
        await asyncio.sleep(TRADE_WINDOW)
        snap = self.pump.get_trade_snapshot(mint)
        await self.pump.unsubscribe_trades(mint)
        return snap

    async def start(self):
        logger.info("Starting scanner — PumpPortal WS + Mobula REST + RugCheck")
        logger.info("Scanning: SOON_MIGRATE + MIGRATED (persistent momentum watching)")
        await self.pump.start_stream()
