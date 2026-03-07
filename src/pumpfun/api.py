import asyncio
import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger("pumpfun")

PUMPFUN_BASE = "https://client-api-2-74b1891ee9f9.herokuapp.com"
PUMPFUN_API = "https://pump.fun/api"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class PumpFunAPI:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; PumpBot/1.0)",
                "Accept": "application/json",
            }
            self.session = aiohttp.ClientSession(
                headers=headers, timeout=REQUEST_TIMEOUT
            )
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_new_tokens(self, limit: int = 50) -> list[dict]:
        """Fetch recently created Pump.fun tokens."""
        try:
            session = await self._get_session()
            url = f"{PUMPFUN_BASE}/coins?offset=0&limit={limit}&sort=created_timestamp&order=DESC&includeNsfw=false"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, list) else []
                logger.warning(f"get_new_tokens HTTP {resp.status}")
                return []
        except Exception as e:
            logger.error(f"get_new_tokens error: {e}")
            return []

    async def get_trending_tokens(self, limit: int = 50) -> list[dict]:
        """Fetch trending / high-volume Pump.fun tokens."""
        try:
            session = await self._get_session()
            url = f"{PUMPFUN_BASE}/coins?offset=0&limit={limit}&sort=last_trade_timestamp&order=DESC&includeNsfw=false"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, list) else []
                logger.warning(f"get_trending_tokens HTTP {resp.status}")
                return []
        except Exception as e:
            logger.error(f"get_trending_tokens error: {e}")
            return []

    async def get_token_detail(self, mint: str) -> dict | None:
        """Fetch detailed info for a single token."""
        try:
            session = await self._get_session()
            url = f"{PUMPFUN_BASE}/coins/{mint}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            logger.error(f"get_token_detail {mint}: {e}")
            return None

    async def get_token_trades(self, mint: str, limit: int = 50) -> list[dict]:
        """Fetch recent trades for a token."""
        try:
            session = await self._get_session()
            url = f"{PUMPFUN_BASE}/trades/all/{mint}?limit={limit}&minimumSize=0"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, list) else []
                return []
        except Exception as e:
            logger.error(f"get_token_trades {mint}: {e}")
            return []

    async def get_dexscreener_data(self, mint: str) -> dict | None:
        """Fetch price/volume data from Dexscreener."""
        try:
            session = await self._get_session()
            url = f"{DEXSCREENER_API}/tokens/{mint}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    # Filter to pump.fun / raydium pairs only
                    pumpfun_pairs = [
                        p for p in pairs
                        if p.get("dexId") in ("pumpfun", "raydium")
                        and p.get("chainId") == "solana"
                    ]
                    if pumpfun_pairs:
                        return pumpfun_pairs[0]
                    return pairs[0] if pairs else None
                return None
        except Exception as e:
            logger.error(f"get_dexscreener_data {mint}: {e}")
            return None

    async def get_holder_data(self, mint: str) -> dict:
        """Fetch holder distribution data."""
        try:
            session = await self._get_session()
            url = f"{PUMPFUN_BASE}/coins/{mint}/holders"
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json() or {}
                return {}
        except Exception as e:
            logger.error(f"get_holder_data {mint}: {e}")
            return {}
