import asyncio
import logging
import time

from pumpfun.api import PumpPortalClient, get_sol_price
from rugcheck.rugcheck import RugChecker
from devstats.devstats import DevStatsModule
from filters.filters import TokenFilters
from scanner.scoring import RunnerScorer

logger = logging.getLogger("scanner")

ALERT_COOLDOWN = 3600
TRADE_WINDOW   = 4.0    # Seconds to watch trades on new tokens — keep fast
MAX_CONCURRENT = 50     # Max simultaneous deep-checks


class TokenScanner:
    def __init__(self):
        self.pump    = PumpPortalClient()
        self.rug     = RugChecker()
        self.dev     = DevStatsModule()
        self.scorer  = RunnerScorer()
        self.filters = TokenFilters()

        self._alerted: dict[str, float] = {}
        self._in_flight: set[str] = set()   # Mints currently being processed
        self._alert_callbacks: list = []
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)

        self.pump.on_new_token      = self._on_new_token
        self.pump.on_migration      = self._on_migration
        self.pump.on_momentum_spike = self._on_momentum_spike

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

    def _should_process(self, mint: str) -> bool:
        if self._already_alerted(mint):
            return False
        if mint in self._in_flight:
            return False
        return True

    def _fire(self, event_type: str, data: dict):
        """Spawn a concurrent task immediately — no queue."""
        mint = data.get("mint", "") or data.get("mint", "")
        if not mint or not self._should_process(mint):
            return
        self._in_flight.add(mint)
        asyncio.create_task(self._process(event_type, data))

    # ── Websocket callbacks ───────────────────────────────────────────────────

    async def _on_new_token(self, data: dict):
        data["_receipt_time"] = time.time()
        mc_sol = float(data.get("marketCapSol", 0) or 0)
        logger.info(f"📥 New: {data.get('name','?')} ({data.get('mint','')[:8]}...) mc_sol={mc_sol:.1f}")
        self._fire("new", data)

    async def _on_migration(self, data: dict):
        data["_receipt_time"] = time.time()
        logger.info(f"🔀 Migration: {data.get('name','?')} ({data.get('mint','')[:8]}...) ← PRIORITY")
        self._fire("migrate", data)

    async def _on_momentum_spike(self, data: dict):
        data["_receipt_time"] = time.time()
        self._fire("spike", data)

    # ── Start ─────────────────────────────────────────────────────────────────

    async def start(self):
        logger.info("Starting scanner — concurrent processing, no queue")
        logger.info("Alert priority: SOON_MIGRATE → MIGRATED → NEW_PAIR")
        await self.pump.start_stream()

    # ── Core processing (runs concurrently for every token) ───────────────────

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

        # Parse
        if event_type == "new":
            token = self.pump.parse_new_token_event(data)
        elif event_type == "migrate":
            token = self.pump.parse_migration_event(data)
        elif event_type == "spike":
            token = self.pump.parse_momentum_spike_event(data)
        else:
            return

        receipt_time = data.get("_receipt_time", time.time())
        token["receipt_age_minutes"] = (time.time() - receipt_time) / 60

        sol_price = get_sol_price()
        if token.get("market_cap_usd", 0) == 0 and token.get("market_cap_sol", 0) > 0:
            token["market_cap_usd"] = token["market_cap_sol"] * sol_price

        # Collect live trades
        if event_type in ("new", "migrate"):
            await self.pump.subscribe_token_trades(mint)
            await asyncio.sleep(TRADE_WINDOW)
            token = self.pump.enrich_with_trades(token)
            await self.pump.unsubscribe_token_trades(mint)
        else:
            token = self.pump.enrich_with_trades(token)

        token["receipt_age_minutes"] = (time.time() - receipt_time) / 60
        token["age_seconds"] = time.time() - token.get("created_at", time.time())

        # ── Filter — priority order: soon_migrate → migrated → new_pair ───────
        category = self.filters.classify(token)
        if category is None:
            return
        token["filter_category"] = category

        # ── On-chain rug + dev stats (parallel) ───────────────────────────────
        rug_report, dev_stats = await asyncio.gather(
            self.rug.check_token(mint),
            self.dev.get_dev_stats(token["dev_wallet"]),
        )

        token["rug_status"]        = rug_report.get("status", "Unknown")
        token["rug_score"]         = rug_report.get("score", 0)
        token["rug_mintable"]      = rug_report.get("mintable", False)
        token["rug_freezable"]     = rug_report.get("freezable", False)
        token["rug_risks"]         = rug_report.get("risks", [])
        token["top10_holders_pct"] = rug_report.get("top_holder_pct", 0.0)

        token["dev_deploy_count"]    = dev_stats.get("deploy_count", 0)
        token["dev_migration_count"] = dev_stats.get("migration_count", 0)
        token["dev_success_ratio"]   = dev_stats.get("success_ratio", 0.0)

        if token["rug_status"] == "Danger":
            return

        # ── Score + tier ──────────────────────────────────────────────────────
        passes, score, tier, reasons = self.scorer.passes(token)
        token["runner_score"]   = score
        token["runner_tier"]    = tier or ""
        token["runner_reasons"] = reasons

        if not passes:
            logger.debug(f"📉 {score}pts ({tier}): {token['name']}")
            return

        self._mark_alerted(mint)
        logger.info(
            f"🚨 {tier} | {token['name']} (${token['symbol']}) "
            f"score={score} cat={category} src={event_type} "
            f"mc=${token['market_cap_usd']:,.0f} "
            f"buys={token['tx_buys_5m']} sells={token['tx_sells_5m']}"
        )

        for cb in self._alert_callbacks:
            try:
                await cb(token)
            except Exception as e:
                logger.error(f"Alert callback error: {e}")
