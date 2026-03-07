import asyncio
import logging
import time

from pumpfun.api import PumpPortalClient
from rugcheck.rugcheck import RugChecker
from devstats.devstats import DevStatsModule
from filters.filters import TokenFilters
from scanner.scoring import RunnerScorer

logger = logging.getLogger("scanner")

ALERT_COOLDOWN = 3600       # Don't re-alert same token for 1 hour
TRADE_WINDOW = 8.0          # Watch live trades for this many seconds before scoring
MAX_QUEUE_SIZE = 500


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

        self.pump.on_new_token = self._on_new_token
        self.pump.on_migration = self._on_migration

    def add_alert_callback(self, cb):
        self._alert_callbacks.append(cb)

    def _already_alerted(self, mint: str) -> bool:
        ts = self._alerted.get(mint)
        return bool(ts and (time.time() - ts) < ALERT_COOLDOWN)

    def _mark_alerted(self, mint: str):
        self._alerted[mint] = time.time()
        now = time.time()
        self._alerted = {k: v for k, v in self._alerted.items() if now - v < ALERT_COOLDOWN * 2}

    # ── Websocket callbacks ───────────────────────────────────────────────────

    async def _on_new_token(self, data: dict):
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint):
            return
        try:
            self._queue.put_nowait(("new", data))
            logger.debug(f"Queued new token: {data.get('name', '?')} ({mint[:8]}...)")
        except asyncio.QueueFull:
            logger.warning("Queue full, dropping token")

    async def _on_migration(self, data: dict):
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint):
            return
        try:
            self._queue.put_nowait(("migrate", data))
        except asyncio.QueueFull:
            logger.warning("Queue full, dropping migration")

    # ── Main loops ────────────────────────────────────────────────────────────

    async def start(self):
        logger.info("Starting TokenScanner (PumpPortal websocket + on-chain rug check)...")
        await asyncio.gather(
            self.pump.start_stream(),
            self._process_queue(),
        )

    async def _process_queue(self):
        logger.info("Token processing worker started.")
        # Semaphore to limit concurrent processing (avoid hammering RPC)
        sem = asyncio.Semaphore(5)
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

    async def _handle_event(self, event_type: str, data: dict):
        try:
            if event_type == "new":
                token = self.pump.parse_new_token_event(data)
            else:
                token = self.pump.parse_migration_event(data)

            mint = token["mint"]
            if not mint:
                return

            # Subscribe to live trades so we collect buy/sell data
            await self.pump.subscribe_token_trades(mint)

            # Let trades accumulate for a few seconds
            await asyncio.sleep(TRADE_WINDOW)

            # Enrich with live trade data
            token = self.pump.enrich_with_trades(token)
            token["age_seconds"] = time.time() - token["created_at"]

            # Unsubscribe to save memory
            await self.pump.unsubscribe_token_trades(mint)

            # Filter check
            category = self.filters.classify(token)
            if category is None:
                logger.debug(f"Filtered: {token['name']} ({mint[:8]}...)")
                return
            token["filter_category"] = category

            # On-chain rug check + dev stats in parallel
            rug_task = asyncio.create_task(self.rug.check_token(mint))
            dev_task = asyncio.create_task(self.dev.get_dev_stats(token["dev_wallet"]))
            rug_report, dev_stats = await asyncio.gather(rug_task, dev_task)

            token["rug_status"] = rug_report.get("status", "Unknown")
            token["rug_score"] = rug_report.get("score", 0)
            token["rug_mintable"] = rug_report.get("mintable", False)
            token["rug_freezable"] = rug_report.get("freezable", False)
            token["rug_risks"] = rug_report.get("risks", [])
            token["rug_top10_pct"] = rug_report.get("top_holder_pct", 0.0)
            token["top10_holders_pct"] = rug_report.get("top_holder_pct", 0.0)

            token["dev_deploy_count"] = dev_stats.get("deploy_count", 0)
            token["dev_migration_count"] = dev_stats.get("migration_count", 0)
            token["dev_success_ratio"] = dev_stats.get("success_ratio", 0.0)

            # Hard block: Danger rug
            if token["rug_status"] == "Danger":
                logger.debug(f"Blocked (Danger): {mint[:8]}...")
                return

            # Runner score
            passes, score, reasons = self.scorer.passes(token)
            token["runner_score"] = score
            token["runner_reasons"] = reasons

            if not passes:
                logger.debug(f"Score {score} too low: {token['name']}")
                return

            self._mark_alerted(mint)
            logger.info(
                f"🚨 ALERT: {token['name']} (${token['symbol']}) "
                f"score={score} cat={category} "
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
