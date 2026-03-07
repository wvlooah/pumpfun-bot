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

# Pump.fun REST API for existing coins
PUMPFUN_REST = "https://frontend-api.pump.fun"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Approximate SOL price — updated periodically
_SOL_PRICE_USD = 150.0


def update_sol_price(price: float):
    global _SOL_PRICE_USD
    if price > 0:
        _SOL_PRICE_USD = price


def get_sol_price() -> float:
    return _SOL_PRICE_USD


class PumpPortalClient:
    """
    Combined PumpPortal websocket + Pump.fun REST polling client.
    - Websocket: streams brand new token creations + migrations instantly
    - REST poll: catches existing/already-running coins every 60s
    """

    def __init__(self):
        self._ws = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._reconnect_delay = 5

        # Callbacks
        self.on_new_token: Callable | None = None
        self.on_migration: Callable | None = None

        # Per-token trade tracking
        self._trade_data: dict[str, dict] = {}
        self._subscribed_trades: set[str] = set()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        return self._session

    async def close(self):
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    # ── REST: fetch existing coins ────────────────────────────────────────────

    async def get_existing_coins(self, limit: int = 50, sort: str = "last_trade_timestamp") -> list[dict]:
        """
        Fetch currently active coins from Pump.fun REST API.
        sort options: last_trade_timestamp, created_timestamp, market_cap
        """
        try:
            session = await self._get_session()
            url = f"{PUMPFUN_REST}/coins?offset=0&limit={limit}&sort={sort}&order=DESC&includeNsfw=false"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, list) else []
                logger.warning(f"REST coins HTTP {resp.status}")
                return []
        except Exception as e:
            logger.error(f"get_existing_coins error: {e}")
            return []

    def normalise_rest_coin(self, coin: dict) -> dict:
        """Convert a Pump.fun REST coin response into the same format as a websocket event."""
        now = time.time()
        created_ts = coin.get("created_timestamp", now * 1000)
        created_at = created_ts / 1000 if created_ts > 1e10 else created_ts
        age_minutes = (now - created_at) / 60

        sol_price = get_sol_price()
        usd_mc = float(coin.get("usd_market_cap", 0) or 0)
        v_sol = float(coin.get("virtual_sol_reserves", 0) or 0)

        return {
            "mint": coin.get("mint", ""),
            "name": coin.get("name", "Unknown"),
            "symbol": coin.get("symbol", "???"),
            "dev_wallet": coin.get("creator", ""),
            "image_uri": coin.get("image_uri", ""),
            "twitter": coin.get("twitter", ""),
            "telegram": coin.get("telegram", ""),
            "website": coin.get("website", ""),
            "description": coin.get("description", ""),
            "age_seconds": (now - created_at),
            "created_at": created_at,
            "receipt_age_minutes": age_minutes,
            "market_cap_usd": usd_mc,
            "market_cap_sol": usd_mc / sol_price if sol_price > 0 else 0,
            "price_usd": float(coin.get("price", 0) or 0),
            "price_change_5m": 0.0,
            "price_change_1h": 0.0,
            "price_change_24h": 0.0,
            "volume_5m_usd": 0.0,
            "volume_1h_usd": 0.0,
            "volume_24h_usd": 0.0,
            "tx_buys_5m": 0,
            "tx_sells_5m": 0,
            "is_dex_listed": coin.get("raydium_pool") is not None,
            "is_migrated": coin.get("raydium_pool") is not None,
            "total_sol_fees": v_sol,
            "top10_holders_pct": 0.0,
            "insider_pct": 0.0,
            "snipers_pct": 0.0,
            "bundles_pct": 0.0,
            "dev_holding_pct": 0.0,
            "pro_holders_count": 0,
            "launchpad": "pumpfun",
            "rug_status": "Unknown",
            "rug_score": 0,
            "rug_mintable": False,
            "rug_freezable": False,
            "rug_risks": [],
            "dev_deploy_count": 0,
            "dev_migration_count": 0,
            "dev_success_ratio": 0.0,
            "runner_score": 0,
            "runner_reasons": [],
            "filter_category": None,
            "_receipt_time": now,
            "_source": "rest_poll",
        }

    # ── Websocket stream ──────────────────────────────────────────────────────

    async def start_stream(self):
        """Connect and stream forever with auto-reconnect."""
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
                    self._reconnect_delay = 5

                    # Subscribe to new token launches
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    # Subscribe to migrations
                    await ws.send(json.dumps({"method": "subscribeMigration"}))

                    logger.info("PumpPortal connected ✓  Streaming new tokens + migrations...")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                            await self._handle_message(data)
                        except Exception as e:
                            logger.error(f"Message error: {e}")

            except (ConnectionClosed, WebSocketException) as e:
                logger.warning(f"WS disconnected: {e}. Reconnecting in {self._reconnect_delay}s...")
            except Exception as e:
                logger.error(f"WS error: {e}. Reconnecting in {self._reconnect_delay}s...")

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _handle_message(self, data: dict):
        tx_type = data.get("txType", "")

        # New token creation
        if tx_type == "create" or ("mint" in data and "name" in data and "symbol" in data):
            if self.on_new_token:
                await self.on_new_token(data)

        # Migration to Raydium
        elif tx_type == "migrate" or "raydiumPool" in data:
            if self.on_migration:
                await self.on_migration(data)

        # Live trade event for a subscribed token
        elif tx_type in ("buy", "sell"):
            mint = data.get("mint", "")
            if mint in self._subscribed_trades:
                self._record_trade(mint, data)

    def _record_trade(self, mint: str, data: dict):
        """Update per-token trade stats from a live trade event."""
        if mint not in self._trade_data:
            self._trade_data[mint] = {
                "buys": 0, "sells": 0,
                "volume_sol": 0.0, "last_price_sol": 0.0,
                "market_cap_sol": 0.0,
            }
        td = self._trade_data[mint]
        sol_amount = float(data.get("solAmount", 0) or 0)
        is_buy = data.get("txType") == "buy"

        if is_buy:
            td["buys"] += 1
        else:
            td["sells"] += 1
        td["volume_sol"] += sol_amount
        td["last_price_sol"] = float(data.get("tokenPrice", 0) or 0)
        td["market_cap_sol"] = float(data.get("marketCapSol", 0) or 0)

    async def subscribe_token_trades(self, mint: str):
        """Start tracking live trades for a specific token."""
        if mint in self._subscribed_trades:
            return
        self._subscribed_trades.add(mint)
        self._trade_data[mint] = {
            "buys": 0, "sells": 0,
            "volume_sol": 0.0, "last_price_sol": 0.0,
            "market_cap_sol": 0.0,
        }
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({
                "method": "subscribeTokenTrade",
                "keys": [mint],
            }))

    async def unsubscribe_token_trades(self, mint: str):
        self._subscribed_trades.discard(mint)
        self._trade_data.pop(mint, None)
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({
                "method": "unsubscribeTokenTrade",
                "keys": [mint],
            }))

    def get_trade_data(self, mint: str) -> dict:
        return self._trade_data.get(mint, {
            "buys": 0, "sells": 0,
            "volume_sol": 0.0, "last_price_sol": 0.0,
            "market_cap_sol": 0.0,
        })

    # ── Token parsing ─────────────────────────────────────────────────────────

    def parse_new_token_event(self, data: dict) -> dict:
        """
        Normalise a PumpPortal create event.
        Fields from PumpPortal: mint, name, symbol, description, image_uri,
        twitter, telegram, website, traderPublicKey, initialBuy,
        solAmount, marketCapSol, vSolInBondingCurve, vTokensInBondingCurve
        """
        now = time.time()
        sol_price = get_sol_price()
        sol_amount = float(data.get("solAmount", 0) or 0)
        market_cap_sol = float(data.get("marketCapSol", 0) or 0)
        initial_buy = float(data.get("initialBuy", 0) or 0)

        return {
            "mint": data.get("mint", ""),
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
            # Market data from bonding curve
            "market_cap_usd": market_cap_sol * sol_price,
            "market_cap_sol": market_cap_sol,
            "price_usd": float(data.get("tokenPrice", 0) or 0) * sol_price,
            "price_change_5m": 0.0,
            "price_change_1h": 0.0,
            "price_change_24h": 0.0,
            "volume_5m_usd": sol_amount * sol_price,
            "volume_1h_usd": 0.0,
            "volume_24h_usd": 0.0,
            "tx_buys_5m": 1 if initial_buy > 0 else 0,
            "tx_sells_5m": 0,
            "is_dex_listed": False,
            "is_migrated": False,
            "total_sol_fees": sol_amount,
            # Holder data (filled by on-chain rugcheck)
            "top10_holders_pct": 0.0,
            "insider_pct": 0.0,
            "snipers_pct": 0.0,
            "bundles_pct": 0.0,
            "dev_holding_pct": 0.0,
            "pro_holders_count": 0,
            "launchpad": "pumpfun",
            # Filled later
            "rug_status": "Unknown",
            "rug_score": 0,
            "rug_mintable": False,
            "rug_freezable": False,
            "rug_risks": [],
            "dev_deploy_count": 0,
            "dev_migration_count": 0,
            "dev_success_ratio": 0.0,
            "runner_score": 0,
            "runner_reasons": [],
            "filter_category": None,
        }

    def parse_migration_event(self, data: dict) -> dict:
        token = self.parse_new_token_event(data)
        token["is_migrated"] = True
        token["is_dex_listed"] = True
        return token

    def enrich_with_trades(self, token: dict) -> dict:
        """Update token with live trade data collected since launch."""
        mint = token["mint"]
        td = self.get_trade_data(mint)
        sol_price = get_sol_price()

        if td["buys"] + td["sells"] > 0:
            token["tx_buys_5m"] = td["buys"]
            token["tx_sells_5m"] = td["sells"]

        if td["volume_sol"] > 0:
            token["volume_5m_usd"] = td["volume_sol"] * sol_price

        if td["market_cap_sol"] > 0:
            token["market_cap_usd"] = td["market_cap_sol"] * sol_price
            token["market_cap_sol"] = td["market_cap_sol"]

        if td["last_price_sol"] > 0:
            token["price_usd"] = td["last_price_sol"] * sol_price

        # Top10 from rugcheck on-chain data
        if token.get("rug_top10_pct"):
            token["top10_holders_pct"] = token["rug_top10_pct"]

        return token
