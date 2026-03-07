import asyncio
import logging
import time

from pumpfun.api import PumpPortalClient, get_sol_price
from rugcheck.rugcheck import RugChecker
from devstats.devstats import DevStatsModule
from filters.filters import TokenFilters
from scanner.scoring import RunnerScorer

logger = logging.getLogger("scanner")

ALERT_COOLDOWN = 3600   # Don't re-alert same token for 1 hour
TRADE_WINDOW = 5.0      # Seconds to collect trades on new tokens before scoring
MAX_QUEUE_SIZE = 1000


class TokenScanner:
    def __init__(self):
        self.pump = PumpPortalClient()
        self.rug = RugChecker()
        self.dev = DevStatsModule()
        self.scorer = RunnerScorer()
        self.filters = TokenFilters()

        self._alerted: dict[str, float] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._alert_callbacks: list = []

        # Wire all callbacks
        self.pump.on_new_token = self._on_new_token
        self.pump.on_migration = self._on_migration
        self.pump.on_momentum_spike = self._on_momentum_spike

    def add_alert_callback(self, cb):
        self._alert_callbacks.append(cb)

    def _already_alerted(self, mint: str) -> bool:
        ts = self._alerted.get(mint)
        return bool(ts and (time.time() - ts) < ALERT_COOLDOWN)

    def _mark_alerted(self, mint: str):
        self._alerted[mint] = time.time()
        now = time.time()
        self._alerted = {k: v for k, v in self._alerted.items() if now - v < ALERT_COOLDOWN * 2}

    # ── Websocket event handlers ──────────────────────────────────────────────

    async def _on_new_token(self, data: dict):
        """Brand new token just launched."""
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint):
            return
        data["_receipt_time"] = time.time()
        mc_sol = float(data.get("marketCapSol", 0) or 0)
        logger.info(f"📥 New: {data.get('name','?')} ({mint[:8]}...) mc_sol={mc_sol:.1f}")
        try:
            self._queue.put_nowait(("new", data))
        except asyncio.QueueFull:
            logger.warning("Queue full — dropping new token")

    async def _on_migration(self, data: dict):
        """Token just graduated to Raydium."""
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint):
            return
        data["_receipt_time"] = time.time()
        logger.info(f"🔀 Migration: {data.get('name','?')} ({mint[:8]}...)")
        try:
            self._queue.put_nowait(("migrate", data))
        except asyncio.QueueFull:
            logger.warning("Queue full — dropping migration")

    async def _on_momentum_spike(self, data: dict):
        """
        Existing coin showing strong buy pressure / volume spike.
        Fired by PumpPortalClient whenever it detects a hot trade window.
        This is how we catch already-running coins without any REST poll.
        """
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint):
            return
        try:
            self._queue.put_nowait(("spike", data))
        except asyncio.QueueFull:
            pass  # Spike events are high-frequency, OK to drop some

    # ── Main loops ────────────────────────────────────────────────────────────

    async def start(self):
        logger.info("Starting TokenScanner (WS new tokens + momentum spike detection)...")
        await asyncio.gather(
            self.pump.start_stream(),
            self._process_queue(),
        )

    async def _process_queue(self):
        logger.info("Processing worker started.")
        sem = asyncio.Semaphore(10)
        while True:
            try:
                event_type, data = await self._queue.get()
                asyncio.create_task(self._guarded_handle(sem, event_type, data))
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Queue error: {e}")

    async def _guarded_handle(self, sem: asyncio.Semaphore, event_type: str, data: dict):
        async with sem:
            await self._handle_event(event_type, data)

    # ── Deep processing pipeline ──────────────────────────────────────────────

    async def _handle_event(self, event_type: str, data: dict):
        try:
            # Parse into standard token dict
            if event_type == "new":
                token = self.pump.parse_new_token_event(data)
            elif event_type == "migrate":
                token = self.pump.parse_migration_event(data)
            elif event_type == "spike":
                token = self.pump.parse_momentum_spike_event(data)
            else:
                return

            mint = token["mint"]
            if not mint or self._already_alerted(mint):
                return

            # Age from receipt stamp
            receipt_time = data.get("_receipt_time", time.time())
            token["receipt_age_minutes"] = (time.time() - receipt_time) / 60

            # Ensure market cap in USD
            sol_price = get_sol_price()
            if token.get("market_cap_usd", 0) == 0 and token.get("market_cap_sol", 0) > 0:
                token["market_cap_usd"] = token["market_cap_sol"] * sol_price

            # For new WS tokens: subscribe briefly to collect trade data
            if event_type in ("new", "migrate"):
                await self.pump.subscribe_token_trades(mint)
                await asyncio.sleep(TRADE_WINDOW)
                token = self.pump.enrich_with_trades(token)
                await self.pump.unsubscribe_token_trades(mint)
            elif event_type == "spike":
                # Spike tokens already have trade data embedded
                token = self.pump.enrich_with_trades(token)

            token["receipt_age_minutes"] = (time.time() - receipt_time) / 60
            token["age_seconds"] = time.time() - token.get("created_at", time.time())

            # ── Stage 1: filter check (free, no API) ──────────────────────────
            category = self.filters.classify(token)
            if category is None:
                return
            token["filter_category"] = category

            # ── Stage 2: on-chain rug + dev stats ────────────────────────────
            rug_report, dev_stats = await asyncio.gather(
                self.rug.check_token(mint),
                self.dev.get_dev_stats(token["dev_wallet"]),
            )

            token["rug_status"]    = rug_report.get("status", "Unknown")
            token["rug_score"]     = rug_report.get("score", 0)
            token["rug_mintable"]  = rug_report.get("mintable", False)
            token["rug_freezable"] = rug_report.get("freezable", False)
            token["rug_risks"]     = rug_report.get("risks", [])
            token["top10_holders_pct"] = rug_report.get("top_holder_pct", 0.0)

            token["dev_deploy_count"]    = dev_stats.get("deploy_count", 0)
            token["dev_migration_count"] = dev_stats.get("migration_count", 0)
            token["dev_success_ratio"]   = dev_stats.get("success_ratio", 0.0)

            if token["rug_status"] == "Danger":
                logger.debug(f"🚫 Rug Danger: {token['name']} ({mint[:8]}...)")
                return

            # ── Stage 3: runner score ─────────────────────────────────────────
            passes, score, reasons = self.scorer.passes(token)
            token["runner_score"]   = score
            token["runner_reasons"] = reasons

            if not passes:
                logger.debug(f"📉 Score {score}: {token['name']}")
                return

            self._mark_alerted(mint)
            logger.info(
                f"🚨 ALERT: {token['name']} (${token['symbol']}) "
                f"score={score} cat={category} src={event_type} "
                f"mc=${token['market_cap_usd']:,.0f} "
                f"buys={token['tx_buys_5m']} sells={token['tx_sells_5m']}"
            )

            for cb in self._alert_callbacks:
                try:
                    await cb(token)
                except Exception as e:
                    logger.error(f"Alert callback error: {e}")

        except Exception as e:
            logger.error(f"handle_event error: {e}", exc_info=True)
