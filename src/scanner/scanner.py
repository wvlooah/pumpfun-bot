import asyncio
import logging
import time

from pumpfun.api import PumpPortalClient
from rugcheck.rugcheck import RugCheckAPI
from devstats.devstats import DevStatsModule
from filters.filters import TokenFilters
from scanner.scoring import RunnerScorer

logger = logging.getLogger("scanner")

ALERT_COOLDOWN = 3600       # Don't re-alert same token for 1 hour
DEX_FETCH_DELAY = 3.0       # Wait a few seconds before hitting Dexscreener (token needs to index)
MAX_QUEUE_SIZE = 200


class TokenScanner:
    def __init__(self):
        self.pump = PumpPortalClient()
        self.rug = RugCheckAPI()
        self.dev = DevStatsModule()
        self.scorer = RunnerScorer()
        self.filters = TokenFilters()

        self._alerted: dict[str, float] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._alert_callbacks: list = []

        # Wire up PumpPortal callbacks
        self.pump.on_new_token = self._on_new_token
        self.pump.on_migration = self._on_migration

    def add_alert_callback(self, cb):
        """Register a callback that receives a token dict when an alert fires."""
        self._alert_callbacks.append(cb)

    def _already_alerted(self, mint: str) -> bool:
        ts = self._alerted.get(mint)
        if ts and (time.time() - ts) < ALERT_COOLDOWN:
            return True
        return False

    def _mark_alerted(self, mint: str):
        self._alerted[mint] = time.time()
        # Prune old entries
        now = time.time()
        self._alerted = {k: v for k, v in self._alerted.items() if now - v < ALERT_COOLDOWN * 2}

    # ── Websocket event handlers ──────────────────────────────────────────────

    async def _on_new_token(self, data: dict):
        """Called instantly when PumpPortal fires a new token event."""
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint):
            return
        try:
            self._queue.put_nowait(("new", data))
        except asyncio.QueueFull:
            logger.warning("Token queue full, dropping event")

    async def _on_migration(self, data: dict):
        """Called when a token migrates to Raydium."""
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint):
            return
        try:
            self._queue.put_nowait(("migrate", data))
        except asyncio.QueueFull:
            logger.warning("Migration queue full, dropping event")

    # ── Main loops ────────────────────────────────────────────────────────────

    async def start(self):
        """
        Start both the websocket stream and the processing worker concurrently.
        This runs forever (until cancelled).
        """
        logger.info("Starting TokenScanner (PumpPortal websocket mode)...")
        await asyncio.gather(
            self.pump.start_stream(),
            self._process_queue(),
        )

    async def _process_queue(self):
        """Worker that processes queued token events one at a time."""
        logger.info("Token processing worker started.")
        while True:
            try:
                event_type, data = await self._queue.get()
                await self._handle_event(event_type, data)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Queue processing error: {e}", exc_info=True)

    async def _handle_event(self, event_type: str, data: dict):
        """Process a single token event from the queue."""
        try:
            if event_type == "new":
                token = self.pump.parse_new_token_event(data)
            else:
                token = self.pump.parse_migration_event(data)

            mint = token["mint"]
            if not mint:
                return

            # Small delay so Dexscreener has time to index the token
            await asyncio.sleep(DEX_FETCH_DELAY)

            # Enrich with Dexscreener price/volume
            dex = await self.pump.get_dexscreener_data(mint)
            if dex:
                token = self.pump.enrich_with_dexscreener(token, dex)

            # Update age
            token["age_seconds"] = time.time() - token["created_at"]

            # Filter check
            category = self.filters.classify(token)
            if category is None:
                logger.debug(f"Filtered out: {token['name']} ({mint[:8]}...)")
                return
            token["filter_category"] = category

            # Safety + dev stats in parallel
            rug_task = asyncio.create_task(self.rug.check_token(mint))
            dev_task = asyncio.create_task(self.dev.get_dev_stats(token["dev_wallet"]))
            rug_report, dev_stats = await asyncio.gather(rug_task, dev_task)

            token["rug_status"] = rug_report.get("status", "Unknown")
            token["rug_score"] = rug_report.get("score", 0)
            token["rug_mintable"] = rug_report.get("mintable", True)
            token["rug_freezable"] = rug_report.get("freezable", True)
            token["rug_risks"] = rug_report.get("risks", [])

            token["dev_deploy_count"] = dev_stats.get("deploy_count", 0)
            token["dev_migration_count"] = dev_stats.get("migration_count", 0)
            token["dev_success_ratio"] = dev_stats.get("success_ratio", 0.0)

            # Hard block on Danger rug
            if token["rug_status"] == "Danger":
                logger.debug(f"Blocked (rug Danger): {mint[:8]}...")
                return

            # Runner score
            passes, score, reasons = self.scorer.passes(token)
            token["runner_score"] = score
            token["runner_reasons"] = reasons

            if not passes:
                logger.debug(f"Score {score} too low: {token['name']} ({mint[:8]}...)")
                return

            self._mark_alerted(mint)
            logger.info(
                f"🚨 ALERT: {token['name']} (${token['symbol']}) "
                f"score={score} cat={category} mc=${token['market_cap_usd']:,.0f}"
            )

            # Fire all registered callbacks
            for cb in self._alert_callbacks:
                try:
                    await cb(token)
                except Exception as e:
                    logger.error(f"Alert callback error: {e}")

        except Exception as e:
            logger.error(f"_handle_event error: {e}", exc_info=True)
