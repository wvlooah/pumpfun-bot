import asyncio
import logging
import time

from pumpfun.api import PumpPortalClient, get_sol_price
from rugcheck.rugcheck import RugChecker
from devstats.devstats import DevStatsModule
from filters.filters import TokenFilters
from scanner.scoring import RunnerScorer

logger = logging.getLogger("scanner")

ALERT_COOLDOWN = 3600       # Don't re-alert same token for 1 hour
TRADE_WINDOW = 5.0          # Seconds to collect live trades before scoring
MAX_QUEUE_SIZE = 1000
REST_POLL_INTERVAL = 10     # Seconds between REST polls


# ── Stage 1: Fast pre-filter (no API calls, pure math) ───────────────────────

def _quick_filter(coin: dict) -> tuple[bool, str]:
    """
    Instant triage on raw REST data.
    Returns (passes, reason_if_failed).
    No API calls — just checks numbers already in the payload.
    """
    sol_price = get_sol_price()

    usd_mc = float(coin.get("usd_market_cap", 0) or 0)
    v_sol  = float(coin.get("virtual_sol_reserves", 0) or 0)
    usd_mc_est = usd_mc if usd_mc > 0 else v_sol * sol_price

    # Must have some market cap
    if usd_mc_est < 1000:
        return False, f"mc too low ${usd_mc_est:.0f}"

    # Must be pump.fun (not raydium-only token)
    # Raydium-migrated coins have raydium_pool set — still valid, keep them
    # Reject if no bonding curve data at all
    if not coin.get("bonding_curve") and not coin.get("raydium_pool"):
        return False, "no bonding curve or pool"

    # Must have had recent trading activity
    last_trade = coin.get("last_trade_timestamp", 0) or 0
    if last_trade > 0:
        last_trade_age = (time.time() * 1000 - last_trade) / 1000  # seconds
        if last_trade_age > 600:  # No trade in last 10 minutes = dead
            return False, f"no trades for {last_trade_age:.0f}s"

    # Reply count / trade count as momentum signal
    reply_count = int(coin.get("reply_count", 0) or 0)
    # Not a hard block but log it

    # Age check — must be within filter windows (max 180 min)
    created_ts = coin.get("created_timestamp", 0) or 0
    if created_ts > 0:
        age_min = (time.time() - created_ts / 1000) / 60
        if age_min > 180:
            return False, f"too old {age_min:.0f}m"

    return True, "ok"


def _momentum_score(coin: dict) -> float:
    """
    Quick momentum score 0-100 from raw REST data only.
    Used to prioritise which coins get deep-checked first.
    """
    score = 0.0
    sol_price = get_sol_price()

    usd_mc = float(coin.get("usd_market_cap", 0) or 0)
    v_sol  = float(coin.get("virtual_sol_reserves", 0) or 0)
    reply_count = int(coin.get("reply_count", 0) or 0)

    # Market cap size signal
    if usd_mc >= 50000:
        score += 30
    elif usd_mc >= 20000:
        score += 20
    elif usd_mc >= 8000:
        score += 12
    elif usd_mc >= 3000:
        score += 5

    # Bonding curve SOL — higher = more bought in
    if v_sol >= 50:
        score += 25
    elif v_sol >= 20:
        score += 15
    elif v_sol >= 5:
        score += 8

    # Community engagement
    if reply_count >= 50:
        score += 20
    elif reply_count >= 20:
        score += 12
    elif reply_count >= 5:
        score += 5

    # Recency of last trade
    last_trade = coin.get("last_trade_timestamp", 0) or 0
    if last_trade > 0:
        age_s = (time.time() * 1000 - last_trade) / 1000
        if age_s < 30:
            score += 25   # Traded in last 30s = hot
        elif age_s < 120:
            score += 15
        elif age_s < 300:
            score += 5

    return min(score, 100.0)


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
        data["_receipt_time"] = time.time()
        mc_sol = float(data.get("marketCapSol", 0) or 0)
        logger.info(f"📥 WS New: {data.get('name','?')} ({mint[:8]}...) mc_sol={mc_sol:.1f}")
        try:
            self._queue.put_nowait(("new", data))
        except asyncio.QueueFull:
            logger.warning("Queue full — dropping WS token")

    async def _on_migration(self, data: dict):
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint):
            return
        data["_receipt_time"] = time.time()
        logger.info(f"🔀 WS Migration: {data.get('name','?')} ({mint[:8]}...)")
        try:
            self._queue.put_nowait(("migrate", data))
        except asyncio.QueueFull:
            logger.warning("Queue full — dropping migration")

    # ── REST polling loop ─────────────────────────────────────────────────────

    async def _rest_poll_loop(self):
        logger.info(f"REST poll loop started — every {REST_POLL_INTERVAL}s...")
        await asyncio.sleep(5)  # Let websocket connect first

        while True:
            try:
                await self._poll_once()
            except Exception as e:
                logger.error(f"REST poll error: {e}")
            await asyncio.sleep(REST_POLL_INTERVAL)

    async def _poll_once(self):
        # Fetch 3 sorted views simultaneously
        by_trade, by_mcap, by_new = await asyncio.gather(
            self.pump.get_existing_coins(limit=100, sort="last_trade_timestamp"),
            self.pump.get_existing_coins(limit=100, sort="market_cap"),
            self.pump.get_existing_coins(limit=50,  sort="created_timestamp"),
        )

        # Deduplicate by mint
        seen: dict[str, dict] = {}
        for coin in by_trade + by_mcap + by_new:
            mint = coin.get("mint", "")
            if mint and mint not in seen:
                seen[mint] = coin

        total = len(seen)
        passed = 0
        dropped_filter = 0
        dropped_alerted = 0
        dropped_queue = 0

        # ── Stage 1: instant triage — no API calls ────────────────────────────
        candidates: list[tuple[float, dict]] = []
        for mint, coin in seen.items():
            if self._already_alerted(mint):
                dropped_alerted += 1
                continue

            ok, reason = _quick_filter(coin)
            if not ok:
                dropped_filter += 1
                logger.debug(f"⚡ Quick-drop: {coin.get('name','?')} — {reason}")
                continue

            momentum = _momentum_score(coin)
            candidates.append((momentum, coin))

        # Sort by momentum — highest first so best coins get processed first
        candidates.sort(key=lambda x: x[0], reverse=True)

        # ── Stage 2: queue survivors for deep processing ──────────────────────
        for momentum, coin in candidates:
            mint = coin.get("mint", "")
            token = self.pump.normalise_rest_coin(coin)
            token["_momentum_prescore"] = momentum
            try:
                self._queue.put_nowait(("existing", token))
                passed += 1
            except asyncio.QueueFull:
                dropped_queue += 1
                break

        logger.info(
            f"🔍 Poll: {total} coins → "
            f"{passed} queued (top momentum first) | "
            f"{dropped_filter} quick-dropped | "
            f"{dropped_alerted} already alerted | "
            f"{dropped_queue} queue-full"
        )

    # ── Main loops ────────────────────────────────────────────────────────────

    async def start(self):
        logger.info("Starting TokenScanner (WS + REST poll + 2-stage pipeline)...")
        await asyncio.gather(
            self.pump.start_stream(),
            self._rest_poll_loop(),
            self._process_queue(),
        )

    async def _process_queue(self):
        logger.info("Processing worker started.")
        sem = asyncio.Semaphore(8)  # Up to 8 concurrent deep-checks
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

    # ── Stage 2: deep processing ──────────────────────────────────────────────

    async def _handle_event(self, event_type: str, data: dict):
        try:
            # Already-normalised for REST coins; parse for WS events
            if event_type == "existing":
                token = data
            elif event_type == "new":
                token = self.pump.parse_new_token_event(data)
            else:
                token = self.pump.parse_migration_event(data)

            mint = token["mint"]
            if not mint or self._already_alerted(mint):
                return

            # Stamp age in minutes from receipt
            receipt_time = data.get("_receipt_time", token.get("created_at", time.time()))
            token["receipt_age_minutes"] = (time.time() - receipt_time) / 60

            # Ensure USD market cap
            sol_price = get_sol_price()
            if token.get("market_cap_usd", 0) == 0 and token.get("market_cap_sol", 0) > 0:
                token["market_cap_usd"] = token["market_cap_sol"] * sol_price

            # WS tokens: subscribe to trades briefly for buy/sell data
            if event_type in ("new", "migrate"):
                await self.pump.subscribe_token_trades(mint)
                await asyncio.sleep(TRADE_WINDOW)
                token = self.pump.enrich_with_trades(token)
                await self.pump.unsubscribe_token_trades(mint)

            token["receipt_age_minutes"] = (time.time() - receipt_time) / 60
            token["age_seconds"] = time.time() - token.get("created_at", time.time())

            # ── Filter check ──────────────────────────────────────────────────
            category = self.filters.classify(token)
            if category is None:
                return
            token["filter_category"] = category

            # ── On-chain rug check + dev stats (parallel) ─────────────────────
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

            # ── Runner score ──────────────────────────────────────────────────
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
                f"age={token['receipt_age_minutes']:.1f}m"
            )

            for cb in self._alert_callbacks:
                try:
                    await cb(token)
                except Exception as e:
                    logger.error(f"Alert callback error: {e}")

        except Exception as e:
            logger.error(f"handle_event error: {e}", exc_info=True)


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
        data["_receipt_time"] = time.time()
        mc_sol = float(data.get("marketCapSol", 0) or 0)
        sol = float(data.get("solAmount", 0) or 0)
        logger.info(f"📥 WS New: {data.get('name','?')} ({mint[:8]}...) mc_sol={mc_sol:.1f} sol={sol:.3f}")
        try:
            self._queue.put_nowait(("new", data))
        except asyncio.QueueFull:
            logger.warning("Queue full, dropping token")

    async def _on_migration(self, data: dict):
        mint = data.get("mint", "")
        if not mint or self._already_alerted(mint):
            return
        data["_receipt_time"] = time.time()
        logger.info(f"🔀 WS Migration: {data.get('name','?')} ({mint[:8]}...)")
        try:
            self._queue.put_nowait(("migrate", data))
        except asyncio.QueueFull:
            logger.warning("Queue full, dropping migration")

    # ── REST polling loop (catches existing/already-running coins) ────────────

    async def _rest_poll_loop(self):
        """
        Poll Pump.fun REST API every 60s for existing active coins.
        This catches coins that were already running before the bot started,
        or coins that slipped through the websocket.
        """
        logger.info("REST poll loop started — scanning existing coins every 60s...")
        await asyncio.sleep(10)  # Short delay to let websocket connect first

        while True:
            try:
                logger.info("🔍 REST poll: fetching existing coins...")

                # Fetch by last trade (most active), market cap, AND newest
                by_trade, by_mcap, by_new = await asyncio.gather(
                    self.pump.get_existing_coins(limit=100, sort="last_trade_timestamp"),
                    self.pump.get_existing_coins(limit=100, sort="market_cap"),
                    self.pump.get_existing_coins(limit=50, sort="created_timestamp"),
                )

                # Deduplicate
                seen: dict[str, dict] = {}
                for coin in by_trade + by_mcap + by_new:
                    mint = coin.get("mint", "")
                    if mint and mint not in seen:
                        seen[mint] = coin

                queued = 0
                skipped = 0
                for mint, coin in seen.items():
                    if self._already_alerted(mint):
                        skipped += 1
                        continue
                    token = self.pump.normalise_rest_coin(coin)
                    try:
                        self._queue.put_nowait(("existing", token))
                        queued += 1
                    except asyncio.QueueFull:
                        break

                logger.info(f"🔍 REST poll: {len(seen)} coins found, {queued} queued, {skipped} already alerted")

            except Exception as e:
                logger.error(f"REST poll error: {e}")

            await asyncio.sleep(REST_POLL_INTERVAL)

    # ── Main loops ────────────────────────────────────────────────────────────

    async def start(self):
        logger.info("Starting TokenScanner (websocket + REST poll + on-chain rug check)...")
        await asyncio.gather(
            self.pump.start_stream(),      # Real-time new tokens
            self._rest_poll_loop(),         # Existing coins every 60s
            self._process_queue(),          # Process all queued events
        )

    async def _process_queue(self):
        logger.info("Token processing worker started.")
        sem = asyncio.Semaphore(5)
        while True:
            try:
                item = await self._queue.get()
                event_type, data = item
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
            # "existing" events are already normalised by normalise_rest_coin()
            if event_type == "existing":
                token = data
            elif event_type == "new":
                token = self.pump.parse_new_token_event(data)
            else:
                token = self.pump.parse_migration_event(data)

            mint = token["mint"]
            if not mint:
                return

            # Double-check not already alerted (may have been queued before marking)
            if self._already_alerted(mint):
                return

            # Stamp receipt age in minutes
            receipt_time = data.get("_receipt_time", token.get("created_at", time.time()))
            token["receipt_age_minutes"] = (time.time() - receipt_time) / 60

            # Ensure USD market cap is set
            sol_price = get_sol_price()
            mc_sol = token.get("market_cap_sol", 0)
            if mc_sol > 0 and token.get("market_cap_usd", 0) == 0:
                token["market_cap_usd"] = mc_sol * sol_price

            # For websocket tokens: subscribe to live trades briefly
            if event_type in ("new", "migrate"):
                await self.pump.subscribe_token_trades(mint)
                await asyncio.sleep(TRADE_WINDOW)
                token = self.pump.enrich_with_trades(token)
                await self.pump.unsubscribe_token_trades(mint)

            # Update age after any trade window
            token["receipt_age_minutes"] = (time.time() - receipt_time) / 60
            token["age_seconds"] = time.time() - token.get("created_at", time.time())

            # Filter check
            category = self.filters.classify(token)
            if category is None:
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
            token["top10_holders_pct"] = rug_report.get("top_holder_pct", 0.0)

            token["dev_deploy_count"] = dev_stats.get("deploy_count", 0)
            token["dev_migration_count"] = dev_stats.get("migration_count", 0)
            token["dev_success_ratio"] = dev_stats.get("success_ratio", 0.0)

            # Hard block on Danger rug
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
                f"score={score} cat={category} src={event_type} "
                f"mc=${token['market_cap_usd']:,.0f} "
                f"age={token['receipt_age_minutes']:.1f}m"
            )

            for cb in self._alert_callbacks:
                try:
                    await cb(token)
                except Exception as e:
                    logger.error(f"Alert callback error: {e}")

        except Exception as e:
            logger.error(f"handle_event error: {e}", exc_info=True)
