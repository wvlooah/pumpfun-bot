"""
Mobula Pulse V2 WebSocket client.
Streams real-time pumpfun token data including:
  - new      -> new_pair     (not bonded yet)
  - bonding  -> soon_migrate (final stretch, about to graduate)
  - bonded   -> migrated     (graduated to Raydium)

TokenDataSchema fields we use:
  address, symbol, name, logo, creatorAddress
  latest_price, market_cap, liquidity
  price_change_1h/4h/24h
  volume_1h/24h, organic_volume_1h
  trades_1h/24h, organic_trades_1h
  holdersCount, top10HoldingsPercentage
  devHoldingsPercentage, insidersHoldingsPercentage
  bundlersHoldingsPercentage, snipersHoldingsPercentage
  insidersCount, bundlersCount, snipersCount
  freshTradersCount, proTradersCount, smartTradersCount
  proTradersBuys, smartTradersBuys
  created_at, website, twitter
"""

import asyncio
import json
import logging
import os
import time
from typing import Callable

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger("mobula")

MOBULA_WS  = "wss://pulse-v2-api.mobula.io"
MOBULA_KEY = os.getenv("MOBULA_API_KEY", "756f2b77-7ec7-4e43-a944-98a4a70e4e4b")

VIEW_CATEGORY = {
    "bonding": "soon_migrate",
    "bonded":  "migrated",
    "new":     "new_pair",
}


class MobulaClient:
    def __init__(self):
        self._ws = None
        self._running = False
        self._reconnect_delay = 5
        self.on_token: Callable | None = None
        self._last_seen: dict[str, float] = {}
        self._DEDUP_WINDOW = 20  # seconds

    async def start_stream(self):
        self._running = True
        while self._running:
            try:
                await self._connect()
            except (ConnectionClosed, WebSocketException) as e:
                logger.warning(f"Mobula WS disconnected: {e}. Reconnecting in {self._reconnect_delay}s...")
            except Exception as e:
                logger.error(f"Mobula WS error: {e}. Reconnecting in {self._reconnect_delay}s...")
            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, 60)

    async def _connect(self):
        logger.info("Connecting to Mobula Pulse V2 (wss://pulse-v2-api.mobula.io)...")
        async with websockets.connect(
            MOBULA_WS,
            ping_interval=30,
            ping_timeout=15,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._reconnect_delay = 5

            await ws.send(json.dumps({
                "type": "pulse-v2",
                "authorization": MOBULA_KEY,
                "payload": {
                    "model": "default",
                    "assetMode": True,
                    "chainId": ["solana:solana"],
                    "poolTypes": ["pumpfun"],
                    "compressed": False,
                }
            }))
            logger.info("Mobula Pulse V2 ✓  Streaming new / bonding / bonded pumpfun tokens...")

            async for raw in ws:
                if not self._running:
                    break
                try:
                    await self._handle_message(json.loads(raw))
                except Exception as e:
                    logger.error(f"Mobula message error: {e}")

    async def _handle_message(self, msg: dict):
        # Mobula sends: {"view": "bonding", "data": [...]}
        view_name = msg.get("view") or msg.get("name") or ""
        data = msg.get("data") or msg.get("tokens") or []

        # Handle nested payload format
        if not view_name and "payload" in msg:
            inner = msg["payload"]
            view_name = inner.get("view") or inner.get("name") or ""
            data = inner.get("data") or inner.get("tokens") or []

        if not view_name or not isinstance(data, list):
            return

        category = VIEW_CATEGORY.get(view_name)
        if not category:
            return

        now = time.time()
        for raw_token in data:
            mint = raw_token.get("address") or raw_token.get("mint") or ""
            if not mint:
                continue
            if now - self._last_seen.get(mint, 0) < self._DEDUP_WINDOW:
                continue
            self._last_seen[mint] = now

            token = self._normalise(raw_token, category)
            if self.on_token:
                try:
                    await self.on_token(token, category)
                except Exception as e:
                    logger.error(f"on_token error: {e}")

        # Trim dedup cache
        if len(self._last_seen) > 10000:
            cutoff = now - self._DEDUP_WINDOW * 10
            self._last_seen = {k: v for k, v in self._last_seen.items() if v > cutoff}

    def _normalise(self, r: dict, category: str) -> dict:
        now = time.time()

        def flt(k, d=0.0):
            v = r.get(k)
            try: return float(v) if v is not None else d
            except: return d

        def num(k, d=0):
            v = r.get(k)
            try: return int(v) if v is not None else d
            except: return d

        def txt(k, d=""):
            v = r.get(k)
            return str(v).strip() if v else d

        created_raw = r.get("created_at") or r.get("createdAt") or 0
        created_at  = (created_raw / 1000) if created_raw > 1e10 else (float(created_raw) if created_raw else now)
        age_s       = now - created_at

        liquidity   = flt("liquidity")
        sol_fees_est = liquidity / 150 if liquidity > 0 else 0.0

        return {
            # Identity
            "mint":        txt("address") or txt("mint"),
            "name":        txt("name", "Unknown"),
            "symbol":      txt("symbol", "???"),
            "dev_wallet":  txt("creatorAddress") or txt("creator"),
            "image_uri":   txt("logo") or txt("image"),
            "twitter":     txt("twitter") or txt("twitterUrl"),
            "website":     txt("website") or txt("websiteUrl"),
            "description": txt("description"),
            "launchpad":   "pumpfun",

            # Time
            "created_at":          created_at,
            "age_seconds":         age_s,
            "receipt_age_minutes": age_s / 60,
            "_receipt_time":       now,

            # Market
            "market_cap_usd": flt("market_cap"),
            "market_cap_sol": 0.0,
            "price_usd":      flt("latest_price"),
            "liquidity_usd":  liquidity,

            # Price changes
            "price_change_5m":  flt("price_change_5m"),
            "price_change_1h":  flt("price_change_1h"),
            "price_change_4h":  flt("price_change_4h"),
            "price_change_24h": flt("price_change_24h"),

            # Volume
            "volume_5m_usd":      flt("volume_5m"),
            "volume_1h_usd":      flt("volume_1h"),
            "volume_24h_usd":     flt("volume_24h"),
            "organic_volume_1h":  flt("organic_volume_1h"),
            "organic_trades_1h":  flt("organic_trades_1h"),

            # Trades
            "tx_buys_5m":  num("buy_trades_5m") or num("buyTrades5m"),
            "tx_sells_5m": num("sell_trades_5m") or num("sellTrades5m"),
            "trades_1h":   num("trades_1h"),
            "trades_24h":  num("trades_24h"),

            # ── Holder health (the data we never had before) ──
            "holders_count":     num("holdersCount"),
            "top10_holders_pct": flt("top10HoldingsPercentage"),
            "dev_holding_pct":   flt("devHoldingsPercentage"),
            "insider_pct":       flt("insidersHoldingsPercentage"),
            "bundles_pct":       flt("bundlersHoldingsPercentage"),
            "snipers_pct":       flt("snipersHoldingsPercentage"),

            # Trader counts
            "insiders_count":    num("insidersCount"),
            "bundlers_count":    num("bundlersCount"),
            "snipers_count":     num("snipersCount"),
            "fresh_traders":     num("freshTradersCount"),
            "pro_holders_count": num("proTradersCount"),
            "smart_traders":     num("smartTradersCount"),

            # Smart money activity
            "pro_traders_buys":   num("proTradersBuys"),
            "smart_traders_buys": num("smartTradersBuys"),

            # SOL fees estimate from liquidity
            "total_sol_fees": flt("totalSolFees") or flt("solFees") or sol_fees_est,

            # Status
            "is_migrated":   category == "migrated",
            "is_soon":       category == "soon_migrate",
            "is_dex_listed": category == "migrated",

            # Filled by rugcheck
            "rug_status":    "Unknown",
            "rug_score":     0,
            "rug_mintable":  False,
            "rug_freezable": False,
            "rug_risks":     [],

            # Filled by scorer
            "runner_score":    0,
            "runner_tier":     "",
            "runner_reasons":  [],
            "filter_category": category,
        }
