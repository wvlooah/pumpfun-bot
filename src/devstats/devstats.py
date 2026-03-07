import logging
from typing import Any

import aiohttp

logger = logging.getLogger("devstats")

PUMPFUN_BASE = "https://client-api-2-74b1891ee9f9.herokuapp.com"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class DevStatsModule:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self._cache: dict[str, dict] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_dev_stats(self, dev_wallet: str) -> dict:
        """
        Fetch all tokens deployed by this developer wallet.
        Returns:
        {
            wallet: str,
            deploy_count: int,
            migration_count: int,
            success_ratio: float,
            fees_paid_usd: float,
            tokens: list[dict],
        }
        """
        if dev_wallet in self._cache:
            return self._cache[dev_wallet]

        default = {
            "wallet": dev_wallet,
            "deploy_count": 0,
            "migration_count": 0,
            "success_ratio": 0.0,
            "fees_paid_usd": 0.0,
            "tokens": [],
        }

        try:
            session = await self._get_session()
            url = f"{PUMPFUN_BASE}/coins/user-created-coins/{dev_wallet}?offset=0&limit=50"
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"DevStats {dev_wallet}: HTTP {resp.status}")
                    return default
                tokens = await resp.json()

            if not isinstance(tokens, list):
                return default

            deploy_count = len(tokens)
            migration_count = sum(1 for t in tokens if t.get("raydium_pool") is not None)
            fees_paid = sum(float(t.get("total_supply", 0)) * 0 for t in tokens)  # placeholder
            success_ratio = (migration_count / deploy_count * 100) if deploy_count > 0 else 0.0

            result = {
                "wallet": dev_wallet,
                "deploy_count": deploy_count,
                "migration_count": migration_count,
                "success_ratio": round(success_ratio, 1),
                "fees_paid_usd": fees_paid,
                "tokens": tokens[:10],  # Keep top 10 for memory
            }

            self._cache[dev_wallet] = result
            return result

        except Exception as e:
            logger.error(f"DevStats error {dev_wallet}: {e}")
            return default
