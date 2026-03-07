import logging

import aiohttp

logger = logging.getLogger("devstats")

# PumpPortal has a REST endpoint for dev wallet history
PUMPPORTAL_API = "https://pumpportal.fun/api"
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
        Fetch token launch history for a dev wallet.
        Falls back gracefully if the endpoint isn't available.
        """
        if not dev_wallet:
            return self._default(dev_wallet)

        if dev_wallet in self._cache:
            return self._cache[dev_wallet]

        # Try PumpPortal account data endpoint
        result = await self._try_pumpportal(dev_wallet)
        self._cache[dev_wallet] = result
        return result

    async def _try_pumpportal(self, dev_wallet: str) -> dict:
        try:
            session = await self._get_session()
            # PumpPortal account trades endpoint
            url = f"{PUMPPORTAL_API}/account-trades?publicKey={dev_wallet}&limit=50"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        return self._parse_trades(dev_wallet, data)
        except Exception as e:
            logger.debug(f"DevStats pumpportal {dev_wallet[:8]}...: {e}")

        # Fallback: return minimal stats
        return self._default(dev_wallet)

    def _parse_trades(self, dev_wallet: str, trades: list) -> dict:
        """Parse trade history to estimate deploy/migration count."""
        # Count unique mints where dev was creator (txType=create)
        creates = [t for t in trades if t.get("txType") == "create"]
        migrations = [t for t in trades if t.get("txType") == "migrate"]

        deploy_count = len(creates)
        migration_count = len(migrations)
        success_ratio = (migration_count / deploy_count * 100) if deploy_count > 0 else 0.0

        return {
            "wallet": dev_wallet,
            "deploy_count": deploy_count,
            "migration_count": migration_count,
            "success_ratio": round(success_ratio, 1),
        }

    def _default(self, dev_wallet: str) -> dict:
        return {
            "wallet": dev_wallet,
            "deploy_count": 0,
            "migration_count": 0,
            "success_ratio": 0.0,
        }
