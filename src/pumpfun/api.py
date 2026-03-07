import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Callable

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger("pumpfun")

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"

_SOL_PRICE_USD = 150.0


def update_sol_price(price: float):
    global _SOL_PRICE_USD
    if price > 0:
        _SOL_PRICE_USD = price


def get_sol_price() -> float:
    return _SOL_PRICE_USD


class PumpPortalClient:
    """
    Pure PumpPortal websocket client.

    Subscribes to:
    - subscribeNewToken       → every new pump.fun token instantly
    - subscribeMigration      → every graduation to Raydium
    - subscribeTokenTrade     → per-token live trades (for buy/sell tracking)

    For existing coins: we track ALL incoming trades. Any token that shows
    a volume spike / high buy activity gets flagged via on_momentum_spike callback.
    """

    def __init__(self):
        self._ws = None
        self._running = False
        self._reconnect_delay = 5

        # Callbacks
        self.on_new_token: Callable | None = None
        self.on_migration: Callable | None = None
        self.on_momentum_spike: Callable | None = None  # fires for hot existing coins

        # Per-token rolling trade window (last 60s)
        # mint -> list of {time, sol_amount, is_buy, market_cap_sol}
        self._trade_window: dict[str, list] = defaultdict(list)
        self._subscribed_trades: set[str] = set()

        # Tokens we've seen (to detect existing coins from trade stream)
        self._seen_tokens: dict[str, dict] = {}   # mint -> last trade data
        self._last_spike_check: dict[str, float] = {}  # mint -> last spike alert time

    async def close(self):
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ── Websocket stream ──────────────────────────────────────────────────────

    async def start_stream(self):
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

                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))

                    logger.info("PumpPortal connected ✓  Streaming all token events...")

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

        if tx_type == "create" or ("mint" in data and "name" in data and "symbol" in data and tx_type == ""):
            if self.on_new_token:
                await self.on_new_token(data)

        elif tx_type == "migrate" or "raydiumPool" in data:
            if self.on_migration:
                await self.on_migration(data)

        elif tx_type in ("buy", "sell"):
            mint = data.get("mint", "")
            if mint:
                await self._record_trade(mint, data)

    async def _record_trade(self, mint: str, data: dict):
        """
        Record every trade we see. For tokens NOT currently being actively
        tracked as new, check if they're showing momentum spikes.
        """
        now = time.time()
        sol_amount = float(data.get("solAmount", 0) or 0)
        is_buy = data.get("txType") == "buy"
        mc_sol = float(data.get("marketCapSol", 0) or 0)

        # Update SOL price estimate from market data
        token_price = float(data.get("tokenPrice", 0) or 0)

        # Add to rolling window
        window = self._trade_window[mint]
        window.append({
            "time": now,
            "sol": sol_amount,
            "buy": is_buy,
            "mc_sol": mc_sol,
        })

        # Keep only last 60 seconds
        cutoff = now - 60
        self._trade_window[mint] = [t for t in window if t["time"] > cutoff]

        # Store latest token info for momentum checks
        self._seen_tokens[mint] = {
            "mint": mint,
            "name": data.get("name", ""),
            "symbol": data.get("symbol", ""),
            "traderPublicKey": data.get("traderPublicKey", ""),
            "marketCapSol": mc_sol,
            "solAmount": sol_amount,
            "txType": data.get("txType"),
            "tokenPrice": token_price,
            "_last_trade_time": now,
        }

        # Check for momentum spike on this token
        # Only fire if not seen recently to avoid spam
        last_spike = self._last_spike_check.get(mint, 0)
        if now - last_spike > 30:  # Max one spike check per token per 30s
            await self._check_momentum(mint, now)

    async def _check_momentum(self, mint: str, now: float):
        """
        Analyse the rolling 60s trade window for this token.
        If it looks hot, fire on_momentum_spike.
        """
        window = self._trade_window.get(mint, [])
        if len(window) < 3:  # Need at least 3 trades to care
            return

        buys = sum(1 for t in window if t["buy"])
        sells = sum(1 for t in window if not t["buy"])
        total = buys + sells
        volume_sol = sum(t["sol"] for t in window)
        buy_ratio = buys / total if total > 0 else 0

        # Latest market cap
        mc_sol = self._seen_tokens[mint].get("marketCapSol", 0)
        sol_price = get_sol_price()
        mc_usd = mc_sol * sol_price

        # Momentum criteria — any of these signal a hot token:
        is_hot = (
            (total >= 5 and buy_ratio >= 0.65 and volume_sol >= 0.5)   # Strong buy pressure
            or (total >= 10 and volume_sol >= 1.0)                       # High tx volume
            or (buys >= 8 and volume_sol >= 2.0)                         # Heavy buying
        )

        if is_hot and self.on_momentum_spike:
            self._last_spike_check[mint] = now
            spike_data = dict(self._seen_tokens[mint])
            spike_data["_window_buys"] = buys
            spike_data["_window_sells"] = sells
            spike_data["_window_volume_sol"] = volume_sol
            spike_data["_window_total"] = total
            spike_data["_buy_ratio"] = buy_ratio
            spike_data["_receipt_time"] = now
            logger.info(
                f"⚡ Momentum spike: {spike_data.get('name','?')} ({mint[:8]}...) "
                f"buys={buys} sells={sells} vol={volume_sol:.2f}SOL buy%={buy_ratio*100:.0f}%"
            )
            await self.on_momentum_spike(spike_data)

    # ── Per-token trade subscription (for new tokens) ─────────────────────────

    async def subscribe_token_trades(self, mint: str):
        if mint in self._subscribed_trades:
            return
        self._subscribed_trades.add(mint)
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({
                "method": "subscribeTokenTrade",
                "keys": [mint],
            }))

    async def unsubscribe_token_trades(self, mint: str):
        self._subscribed_trades.discard(mint)
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({
                "method": "unsubscribeTokenTrade",
                "keys": [mint],
            }))

    def get_trade_data(self, mint: str) -> dict:
        window = self._trade_window.get(mint, [])
        sol_price = get_sol_price()
        buys = sum(1 for t in window if t["buy"])
        sells = sum(1 for t in window if not t["buy"])
        volume_sol = sum(t["sol"] for t in window)
        last_info = self._seen_tokens.get(mint, {})
        return {
            "buys": buys,
            "sells": sells,
            "volume_sol": volume_sol,
            "last_price_sol": last_info.get("tokenPrice", 0),
            "market_cap_sol": last_info.get("marketCapSol", 0),
        }

    # ── Token parsing ─────────────────────────────────────────────────────────

    def parse_new_token_event(self, data: dict) -> dict:
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
        }

    def parse_momentum_spike_event(self, data: dict) -> dict:
        """Convert a momentum spike event into a standard token dict."""
        now = time.time()
        sol_price = get_sol_price()
        mc_sol = float(data.get("marketCapSol", 0) or 0)
        vol_sol = float(data.get("_window_volume_sol", 0) or 0)

        return {
            "mint": data.get("mint", ""),
            "name": data.get("name", "Unknown"),
            "symbol": data.get("symbol", "???"),
            "dev_wallet": data.get("traderPublicKey", ""),
            "image_uri": "",
            "twitter": "",
            "telegram": "",
            "website": "",
            "description": "",
            "age_seconds": 0,
            "created_at": now,
            "receipt_age_minutes": 0,
            "market_cap_usd": mc_sol * sol_price,
            "market_cap_sol": mc_sol,
            "price_usd": float(data.get("tokenPrice", 0) or 0) * sol_price,
            "price_change_5m": 0.0,
            "price_change_1h": 0.0,
            "price_change_24h": 0.0,
            "volume_5m_usd": vol_sol * sol_price,
            "volume_1h_usd": 0.0,
            "volume_24h_usd": 0.0,
            "tx_buys_5m": int(data.get("_window_buys", 0)),
            "tx_sells_5m": int(data.get("_window_sells", 0)),
            "is_dex_listed": False,
            "is_migrated": False,
            "total_sol_fees": vol_sol,
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
        }

    def parse_migration_event(self, data: dict) -> dict:
        token = self.parse_new_token_event(data)
        token["is_migrated"] = True
        token["is_dex_listed"] = True
        return token

    def enrich_with_trades(self, token: dict) -> dict:
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
        return token
