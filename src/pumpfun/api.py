import asyncio
import json
import logging
import time
from typing import Callable

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger("pumpfun")

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class PumpPortalClient:
    """
    Websocket client for PumpPortal.
    Streams new token creations and migration events in real time.
    No API key required for bonding curve data.
    """

    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self._ws = None
        self._running = False
        self._reconnect_delay = 5
        self.on_new_token: Callable | None = None
        self.on_migration: Callable | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        return self.session

    async def close(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self.session and not self.session.closed:
            await self.session.close()

    # ── Websocket stream ──────────────────────────────────────────────────────

    async def start_stream(self):
        """Connect and stream events. Auto-reconnects with exponential backoff."""
        self._running = True
        while self._running:
            try:
                logger.info("Connecting to PumpPortal websocket...")
                async with websockets.connect(
                    PUMPPORTAL_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = 5  # Reset on successful connect

                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))

                    logger.info("PumpPortal connected. Streaming new tokens + migrations...")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                            await self._handle_message(data)
                        except Exception as e:
                            logger.error(f"Message handler error: {e}")

            except (ConnectionClosed, WebSocketException) as e:
                logger.warning(f"WS disconnected: {e}. Reconnecting in {self._reconnect_delay}s...")
            except Exception as e:
                logger.error(f"WS error: {e}. Reconnecting in {self._reconnect_delay}s...")

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _handle_message(self, data: dict):
        """Route messages to callbacks."""
        tx_type = data.get("txType", "")

        if tx_type == "create" or ("mint" in data and "name" in data):
            if self.on_new_token:
                await self.on_new_token(data)

        elif tx_type == "migrate" or "raydiumPool" in data:
            if self.on_migration:
                await self.on_migration(data)

    async def subscribe_token_trades(self, mint: str):
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))

    async def unsubscribe_token_trades(self, mint: str):
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))

    # ── Dexscreener ───────────────────────────────────────────────────────────

    async def get_dexscreener_data(self, mint: str) -> dict | None:
        try:
            session = await self._get_session()
            url = f"{DEXSCREENER_API}/tokens/{mint}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not isinstance(data, dict):
                    return None
                pairs = data.get("pairs") or []
                if not isinstance(pairs, list):
                    return None
                for dex_id in ("pumpfun", "raydium"):
                    for p in pairs:
                        if isinstance(p, dict) and p.get("dexId") == dex_id and p.get("chainId") == "solana":
                            return p
                return pairs[0] if pairs else None
        except Exception as e:
            logger.error(f"Dexscreener {mint}: {e}")
            return None

    # ── Token parsing ─────────────────────────────────────────────────────────

    def parse_new_token_event(self, data: dict) -> dict:
        """
        Normalise a PumpPortal 'create' websocket event into a standard token dict.
        PumpPortal fields: mint, name, symbol, description, image_uri,
        twitter, website, creator/traderPublicKey, initialBuy, solAmount,
        marketCapSol, vSolInBondingCurve, vTokensInBondingCurve
        """
        now = time.time()
        sol_price_usd = 150.0  # Fallback; enriched from Dexscreener

        mint = data.get("mint", "")
        sol_amount = float(data.get("solAmount", 0) or 0)
        market_cap_sol = float(data.get("marketCapSol", 0) or 0)

        return {
            "mint": mint,
            "name": data.get("name", "Unknown"),
            "symbol": data.get("symbol", "???"),
            "dev_wallet": data.get("traderPublicKey") or data.get("creator", ""),
            "image_uri": data.get("image_uri", ""),
            "twitter": data.get("twitter", ""),
            "telegram": data.get("telegram", ""),
            "website": data.get("website", ""),
            "description": data.get("description", ""),
            "age_seconds": 0,
            "created_at": now,
            "market_cap_usd": market_cap_sol * sol_price_usd,
            "market_cap_sol": market_cap_sol,
            "price_usd": 0.0,
            "price_change_5m": 0.0,
            "price_change_1h": 0.0,
            "price_change_24h": 0.0,
            "volume_5m_usd": sol_amount * sol_price_usd,
            "volume_1h_usd": 0.0,
            "volume_24h_usd": 0.0,
            "tx_buys_5m": 1 if float(data.get("initialBuy", 0) or 0) > 0 else 0,
            "tx_sells_5m": 0,
            "is_dex_listed": False,
            "is_migrated": False,
            "total_sol_fees": sol_amount,
            "top10_holders_pct": 0.0,
            "insider_pct": 0.0,
            "snipers_pct": 0.0,
            "bundles_pct": 0.0,
            "dev_holding_pct": 0.0,
            "pro_holders_count": 0,
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

    def parse_migration_event(self, data: dict) -> dict:
        """Normalise a migration event."""
        token = self.parse_new_token_event(data)
        token["is_migrated"] = True
        token["is_dex_listed"] = True
        return token

    def enrich_with_dexscreener(self, token: dict, dex: dict | None) -> dict:
        """Merge live Dexscreener price/volume data into token dict."""
        if not dex or not isinstance(dex, dict):
            return token
        pc = dex.get("priceChange") or {}
        vol = dex.get("volume") or {}
        txns = (dex.get("txns") or {}).get("m5") or {}

        token["price_usd"] = float(dex.get("priceUsd") or 0)
        token["price_change_5m"] = float(pc.get("m5") or 0)
        token["price_change_1h"] = float(pc.get("h1") or 0)
        token["price_change_24h"] = float(pc.get("h24") or 0)
        token["volume_5m_usd"] = float(vol.get("m5") or 0)
        token["volume_1h_usd"] = float(vol.get("h1") or 0)
        token["volume_24h_usd"] = float(vol.get("h24") or 0)
        token["tx_buys_5m"] = int(txns.get("buys") or 0)
        token["tx_sells_5m"] = int(txns.get("sells") or 0)
        token["is_dex_listed"] = True

        mc = float(dex.get("marketCap") or 0)
        if mc > 0:
            token["market_cap_usd"] = mc

        return token
