import asyncio
import logging
import time
from collections import deque

from pumpfun.api import PumpFunAPI
from rugcheck.rugcheck import RugCheckAPI
from devstats.devstats import DevStatsModule
from filters.filters import TokenFilters
from scanner.scoring import RunnerScorer

logger = logging.getLogger("scanner")

# How long to keep a token in the "already alerted" cache (seconds)
ALERT_COOLDOWN = 3600  # 1 hour


class TokenScanner:
    def __init__(self):
        self.pump = PumpFunAPI()
        self.rug = RugCheckAPI()
        self.dev = DevStatsModule()
        self.scorer = RunnerScorer()
        self.filters = TokenFilters()

        # mint -> timestamp of last alert
        self._alerted: dict[str, float] = {}

    def _already_alerted(self, mint: str) -> bool:
        ts = self._alerted.get(mint)
        if ts and (time.time() - ts) < ALERT_COOLDOWN:
            return True
        return False

    def _mark_alerted(self, mint: str):
        self._alerted[mint] = time.time()
        # Prune old entries
        now = time.time()
        self._alerted = {
            k: v for k, v in self._alerted.items()
            if now - v < ALERT_COOLDOWN * 2
        }

    async def _normalise_token(self, raw: dict) -> dict:
        """Convert raw Pump.fun API response to a standardised dict."""
        now = time.time()
        created = raw.get("created_timestamp", now * 1000) / 1000
        age_s = now - created

        mint = raw.get("mint", "")
        market_cap = float(raw.get("usd_market_cap", 0) or 0)
        name = raw.get("name", "Unknown")
        symbol = raw.get("symbol", "???")
        dev_wallet = raw.get("creator", "")
        image_uri = raw.get("image_uri", "")
        twitter = raw.get("twitter", "")
        website = raw.get("website", "")
        description = raw.get("description", "")
        is_migrated = raw.get("raydium_pool") is not None

        # Fetch Dexscreener for price/volume
        dex = await self.pump.get_dexscreener_data(mint)
        price_change_5m = 0.0
        price_change_1h = 0.0
        price_change_24h = 0.0
        vol_5m = 0.0
        vol_1h = 0.0
        vol_24h = 0.0
        tx_buys_5m = 0
        tx_sells_5m = 0
        is_dex_listed = False
        price_usd = 0.0

        if dex:
            is_dex_listed = True
            pc = dex.get("priceChange", {})
            price_change_5m = float(pc.get("m5", 0) or 0)
            price_change_1h = float(pc.get("h1", 0) or 0)
            price_change_24h = float(pc.get("h24", 0) or 0)
            vol = dex.get("volume", {})
            vol_5m = float(vol.get("m5", 0) or 0)
            vol_1h = float(vol.get("h1", 0) or 0)
            vol_24h = float(vol.get("h24", 0) or 0)
            txns = dex.get("txns", {}).get("m5", {})
            tx_buys_5m = int(txns.get("buys", 0) or 0)
            tx_sells_5m = int(txns.get("sells", 0) or 0)
            price_usd = float(dex.get("priceUsd", 0) or 0)
            market_cap = float(dex.get("marketCap", market_cap) or market_cap)

        # Holder data (best-effort)
        top10_pct = float(raw.get("top_holders_pct", 0) or 0)
        insider_pct = float(raw.get("insider_pct", 0) or 0)
        snipers_pct = float(raw.get("snipers_pct", 0) or 0)
        bundles_pct = float(raw.get("bundles_pct", 0) or 0)
        dev_holding_pct = float(raw.get("dev_holding_pct", 0) or 0)
        pro_holders = int(raw.get("pro_holders_count", 0) or 0)
        total_sol_fees = float(raw.get("total_sol_fees", raw.get("virtual_sol_reserves", 0)) or 0)

        return {
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "dev_wallet": dev_wallet,
            "image_uri": image_uri,
            "twitter": twitter,
            "website": website,
            "description": description,
            "age_seconds": age_s,
            "market_cap_usd": market_cap,
            "price_usd": price_usd,
            "price_change_5m": price_change_5m,
            "price_change_1h": price_change_1h,
            "price_change_24h": price_change_24h,
            "volume_5m_usd": vol_5m,
            "volume_1h_usd": vol_1h,
            "volume_24h_usd": vol_24h,
            "tx_buys_5m": tx_buys_5m,
            "tx_sells_5m": tx_sells_5m,
            "is_dex_listed": is_dex_listed,
            "is_migrated": is_migrated,
            "top10_holders_pct": top10_pct,
            "insider_pct": insider_pct,
            "snipers_pct": snipers_pct,
            "bundles_pct": bundles_pct,
            "dev_holding_pct": dev_holding_pct,
            "pro_holders_count": pro_holders,
            "total_sol_fees": total_sol_fees,
            "launchpad": "pumpfun",
            "rug_status": "Unknown",
            "rug_score": 0,
            "rug_mintable": True,
            "rug_freezable": True,
            "rug_risks": [],
            "dev_deploy_count": 0,
            "dev_migration_count": 0,
            "dev_success_ratio": 0.0,
            "runner_score": 0,
            "runner_reasons": [],
            "filter_category": None,
        }

    async def _enrich_token(self, token: dict) -> dict:
        """Add rugcheck + dev stats to a normalised token."""
        mint = token["mint"]
        dev_wallet = token["dev_wallet"]

        # Rugcheck + dev in parallel
        rug_task = asyncio.create_task(self.rug.check_token(mint))
        dev_task = asyncio.create_task(self.dev.get_dev_stats(dev_wallet))

        rug_report, dev_stats = await asyncio.gather(rug_task, dev_task)

        token["rug_status"] = rug_report.get("status", "Unknown")
        token["rug_score"] = rug_report.get("score", 0)
        token["rug_mintable"] = rug_report.get("mintable", True)
        token["rug_freezable"] = rug_report.get("freezable", True)
        token["rug_risks"] = rug_report.get("risks", [])

        token["dev_deploy_count"] = dev_stats.get("deploy_count", 0)
        token["dev_migration_count"] = dev_stats.get("migration_count", 0)
        token["dev_success_ratio"] = dev_stats.get("success_ratio", 0.0)

        return token

    async def scan(self) -> list[dict]:
        """
        Full scan cycle: fetch tokens, filter, enrich, score, return alerts.
        """
        alerts: list[dict] = []

        # Fetch both new and trending
        new_tokens, trending = await asyncio.gather(
            self.pump.get_new_tokens(50),
            self.pump.get_trending_tokens(50),
        )

        # Deduplicate by mint
        seen: dict[str, dict] = {}
        for raw in new_tokens + trending:
            mint = raw.get("mint", "")
            if mint and mint not in seen:
                seen[mint] = raw

        logger.info(f"Fetched {len(seen)} unique tokens")

        # Process in batches to avoid hammering APIs
        batch_size = 5
        mints = list(seen.keys())

        for i in range(0, len(mints), batch_size):
            batch = mints[i: i + batch_size]
            tasks = [self._process_token(seen[m]) for m in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, dict):
                    alerts.append(res)
            await asyncio.sleep(0.5)

        logger.info(f"Scan complete. {len(alerts)} alert(s) generated.")
        return alerts

    async def _process_token(self, raw: dict) -> dict | None:
        """Normalise, filter, enrich, score a single token."""
        try:
            mint = raw.get("mint", "")
            if not mint:
                return None
            if self._already_alerted(mint):
                return None

            token = await self._normalise_token(raw)

            # Filter check
            category = self.filters.classify(token)
            if category is None:
                return None
            token["filter_category"] = category

            # Enrich with rugcheck + dev stats
            token = await self._enrich_token(token)

            # Safety gate: don't alert on Danger rug status
            if token["rug_status"] == "Danger":
                logger.debug(f"Skipping {mint}: rug Danger")
                return None

            # Runner score
            passes, score, reasons = self.scorer.passes(token)
            token["runner_score"] = score
            token["runner_reasons"] = reasons

            if not passes:
                logger.debug(f"Skipping {mint}: score {score} below threshold")
                return None

            self._mark_alerted(mint)
            logger.info(f"ALERT: {token['name']} ({token['symbol']}) score={score} cat={category}")
            return token

        except Exception as e:
            logger.error(f"_process_token error: {e}", exc_info=True)
            return None
