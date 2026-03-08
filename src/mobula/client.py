"""
Mobula REST client (free plan).

Endpoint: GET https://production-api.mobula.io/api/1/market/data
  ?asset=<mint>&blockchain=solana

Free plan response fields confirmed:
  price               float  USD price
  market_cap          float  USD market cap
  liquidity           float  USD liquidity
  volume              float  24h on-chain volume USD
  off_chain_volume    float  24h off-chain volume USD
  volume_change_24h   float  % change in 24h volume
  price_change_1h     float  % price change 1h
  price_change_24h    float  % price change 24h
  price_change_7d     float  % price change 7d

NOTE: insider %, sniper %, holder counts, organic vol are Pulse V2 WS only
(Growth plan). We get those from RugCheck instead.

We compute:
  organic_ratio  = on-chain vol / (on-chain + off-chain)  — proxy for bot activity
  vol_accel      = bool, True if vol_change_24h > 50%
"""

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

logger = logging.getLogger("mobula")

MOBULA_BASE = "https://production-api.mobula.io/api/1"
MOBULA_KEY  = os.getenv("MOBULA_API_KEY", "756f2b77-7ec7-4e43-a944-98a4a70e4e4b")
TIMEOUT     = aiohttp.ClientTimeout(total=15)
CACHE_TTL   = 30  # seconds


class MobulaClient:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, tuple[dict, float]] = {}

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": MOBULA_KEY,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=TIMEOUT,
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def enrich_token(self, mint: str) -> dict:
        """Fetch Mobula market data for a mint. Returns {} on failure."""
        if not mint:
            return {}

        cached = self._cache.get(mint)
        if cached and time.time() - cached[1] < CACHE_TTL:
            return cached[0]

        result = await self._fetch(mint)
        self._cache[mint] = (result, time.time())

        if len(self._cache) > 5000:
            now = time.time()
            self._cache = {k: v for k, v in self._cache.items()
                           if now - v[1] < CACHE_TTL * 20}
        return result

    async def _fetch(self, mint: str) -> dict:
        sess = await self._sess()
        try:
            async with sess.get(
                f"{MOBULA_BASE}/market/data",
                params={"asset": mint, "blockchain": "solana"},
            ) as resp:
                if resp.status == 200:
                    body = await resp.json(content_type=None)
                    raw  = body.get("data") or {}
                    if not raw:
                        logger.debug(f"Mobula empty data for {mint[:8]}")
                        return {}
                    result = self._normalise(raw)
                    logger.debug(
                        f"Mobula {mint[:8]}: mc=${result.get('market_cap_usd',0):,.0f} "
                        f"liq=${result.get('liquidity_usd',0):,.0f} "
                        f"1h={result.get('price_change_1h',0):+.1f}% "
                        f"vol24h=${result.get('volume_24h_usd',0):,.0f} "
                        f"organic={result.get('mobula_organic_ratio',0):.0%}"
                    )
                    return result
                elif resp.status == 404:
                    logger.debug(f"Mobula: {mint[:8]} not indexed yet")
                elif resp.status == 429:
                    logger.warning(f"Mobula rate limited")
                    await asyncio.sleep(2)
                else:
                    logger.debug(f"Mobula HTTP {resp.status} for {mint[:8]}")
        except asyncio.TimeoutError:
            logger.debug(f"Mobula timeout {mint[:8]}")
        except Exception as e:
            logger.debug(f"Mobula error {mint[:8]}: {e}")
        return {}

    def _normalise(self, raw: dict) -> dict:
        def f(k, d=0.0):
            v = raw.get(k)
            try: return float(v) if v is not None else d
            except: return d

        price       = f("price")
        mc          = f("market_cap")
        liquidity   = f("liquidity")
        vol_24h     = f("volume")            # on-chain 24h
        off_chain   = f("off_chain_volume")  # off-chain 24h
        vol_change  = f("volume_change_24h") # % vol change vs prior day
        pc_1h       = f("price_change_1h")
        pc_24h      = f("price_change_24h")
        pc_7d       = f("price_change_7d")
        liq_change  = f("liquidity_change_24h")

        total_vol = vol_24h + off_chain
        organic_ratio = vol_24h / total_vol if total_vol > 0 else 1.0

        result = {
            # Direct overrides for token fields (only if non-zero)
            "market_cap_usd":    mc       if mc > 0       else None,
            "price_usd":         price    if price > 0    else None,
            "liquidity_usd":     liquidity if liquidity > 0 else None,
            "volume_24h_usd":    vol_24h  if vol_24h > 0  else None,
            "price_change_1h":   pc_1h    if pc_1h != 0   else None,
            "price_change_24h":  pc_24h   if pc_24h != 0  else None,

            # Mobula-specific enrichment fields used in scoring
            "mobula_vol_change_24h":  vol_change,
            "mobula_liq_change_24h":  liq_change,
            "mobula_organic_ratio":   round(organic_ratio, 3),
            "mobula_vol_accel":       vol_change > 50,   # vol spiking vs yesterday
            "mobula_price_change_7d": pc_7d,
            "mobula_enriched":        True,
        }
        # Strip Nones so we don't overwrite good data with None
        return {k: v for k, v in result.items() if v is not None}
