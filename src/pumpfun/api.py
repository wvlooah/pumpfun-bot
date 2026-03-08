"""
PumpPortal WebSocket — token discovery + live trade streaming.
Only fires soon_migrate and migrated events (new pairs ignored).
Tracks ALL seen tokens indefinitely for momentum re-scan.
"""
import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger("pumpfun")
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"

_SOL_PRICE = 150.0
def update_sol_price(p): global _SOL_PRICE; _SOL_PRICE = p if p > 0 else _SOL_PRICE
def get_sol_price(): return _SOL_PRICE


class PumpPortalClient:
    def __init__(self):
        self._ws = None
        self._running = False
        self._reconnect_delay = 5

        # Callbacks
        self.on_migration: Optional[Callable] = None
        self.on_momentum_spike: Optional[Callable] = None

        # Token tracking — all seen tokens, never discarded
        self._trade_window: dict[str, list] = defaultdict(list)
        self._token_history: dict[str, dict] = {}   # mint -> rolling price/vol history
        self._seen: dict[str, dict] = {}             # mint -> latest known state
        self._last_spike: dict[str, float] = {}      # mint -> last spike fire time
        self._subscribed: set[str] = set()

        # Price history for floor detection (mint -> list of (timestamp, price))
        self._price_history: dict[str, list] = defaultdict(list)

        # Bonding curve fill tracking (mint -> {first_seen_sol, first_seen_time})
        self._bonding_start: dict[str, dict] = {}

    async def start_stream(self):
        self._running = True
        while self._running:
            try:
                logger.info("Connecting to PumpPortal WebSocket...")
                async with websockets.connect(
                    PUMPPORTAL_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = 5
                    # Only subscribe to migrations — no new token spam
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    logger.info("PumpPortal ✓  Streaming migrations only (soon_migrate + migrated)...")
                    async for raw in ws:
                        if not self._running: break
                        try: await self._handle(json.loads(raw))
                        except Exception as e: logger.error(f"PP msg error: {e}")
            except (ConnectionClosed, WebSocketException) as e:
                logger.warning(f"PumpPortal disconnected: {e}. Reconnecting in {self._reconnect_delay}s...")
            except Exception as e:
                logger.error(f"PumpPortal error: {e}")
            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _handle(self, data: dict):
        tx = data.get("txType", "")

        if tx == "migrate" or "raydiumPool" in data:
            if self.on_migration:
                await self.on_migration(data)

        elif tx in ("buy", "sell"):
            mint = data.get("mint", "")
            if mint:
                await self._record_trade(mint, data)

    async def _record_trade(self, mint: str, data: dict):
        now = time.time()
        sol    = float(data.get("solAmount", 0) or 0)
        is_buy = data.get("txType") == "buy"
        mc_sol = float(data.get("marketCapSol", 0) or 0)
        price  = float(data.get("tokenPrice", 0) or 0)

        # Rolling 60s window
        window = self._trade_window[mint]
        window.append({"t": now, "sol": sol, "buy": is_buy, "mc_sol": mc_sol, "price": price})
        self._trade_window[mint] = [x for x in window if now - x["t"] < 60]

        # Price history for floor detection (keep 10 min)
        self._price_history[mint].append((now, price * get_sol_price()))
        self._price_history[mint] = [(t, p) for t, p in self._price_history[mint] if now - t < 600]

        # Bonding curve: record first-seen SOL amount
        if mint not in self._bonding_start and sol > 0:
            self._bonding_start[mint] = {"sol": sol, "time": now}

        # Update latest state
        self._seen[mint] = {
            "mint": mint,
            "name": data.get("name", self._seen.get(mint, {}).get("name", "")),
            "symbol": data.get("symbol", self._seen.get(mint, {}).get("symbol", "")),
            "traderPublicKey": data.get("traderPublicKey", ""),
            "marketCapSol": mc_sol,
            "tokenPrice": price,
            "_last_trade": now,
        }

        # Check for momentum spike (re-scan trigger)
        if now - self._last_spike.get(mint, 0) > 60:
            await self._check_momentum(mint, now)

    async def _check_momentum(self, mint: str, now: float):
        w = self._trade_window.get(mint, [])
        if len(w) < 3: return

        buys   = sum(1 for x in w if x["buy"])
        sells  = sum(1 for x in w if not x["buy"])
        total  = buys + sells
        vol    = sum(x["sol"] for x in w)
        ratio  = buys / total if total > 0 else 0
        mc_sol = self._seen[mint].get("marketCapSol", 0)

        # Momentum conditions — catching coins waking up
        is_hot = (
            (total >= 8  and ratio >= 0.70 and vol >= 1.0)
            or (total >= 15 and vol >= 3.0)
            or (buys >= 10 and vol >= 2.0 and ratio >= 0.65)
            or (mc_sol >= 100 and total >= 8 and ratio >= 0.70)
            or (vol >= 5.0 and ratio >= 0.60)  # big volume even with moderate buy ratio
        )

        if is_hot and self.on_momentum_spike:
            self._last_spike[mint] = now
            spike = dict(self._seen[mint])
            spike.update({
                "_window_buys": buys, "_window_sells": sells,
                "_window_vol_sol": vol, "_buy_ratio": ratio,
                "_receipt_time": now,
            })
            logger.info(f"⚡ Momentum: {spike.get('name','?')} ({mint[:8]}...) "
                       f"buys={buys} vol={vol:.2f}SOL ratio={ratio*100:.0f}%")
            await self.on_momentum_spike(spike)

    async def subscribe_trades(self, mint: str):
        if mint in self._subscribed: return
        self._subscribed.add(mint)
        self._trade_window[mint] = []
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))

    async def unsubscribe_trades(self, mint: str):
        self._subscribed.discard(mint)
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))

    def get_trade_snapshot(self, mint: str) -> dict:
        w   = self._trade_window.get(mint, [])
        sp  = get_sol_price()
        buys  = sum(1 for x in w if x["buy"])
        sells = sum(1 for x in w if not x["buy"])
        vol   = sum(x["sol"] for x in w)
        info  = self._seen.get(mint, {})

        # Repeat buyer detection — wallets buying more than once
        # PumpPortal doesn't give wallet per trade in the stream easily,
        # so we approximate: if buys > unique time slots, repeated buying likely
        buy_times = [x["t"] for x in w if x["buy"]]
        repeat_buy_signal = len(buy_times) > 0 and (buys / max(len(set(int(t) for t in buy_times)), 1)) > 1.2

        # Price floor: check if price recovered after a dip
        ph = self._price_history.get(mint, [])
        price_floor_held = False
        if len(ph) >= 6:
            prices = [p for _, p in ph]
            peak   = max(prices)
            recent = prices[-3:]
            trough = min(prices)
            if peak > 0 and trough < peak * 0.85:  # there was a dip
                # Floor held if recent prices are above 80% of peak
                price_floor_held = all(p >= peak * 0.80 for p in recent)

        # Bonding curve fill speed
        bonding_fill_rate = 0.0
        bs = self._bonding_start.get(mint, {})
        if bs:
            elapsed = max(time.time() - bs["time"], 1)
            current_sol = vol
            bonding_fill_rate = current_sol / elapsed  # SOL per second

        return {
            "tx_buys_5m":         buys,
            "tx_sells_5m":        sells,
            "volume_5m_sol":      vol,
            "volume_5m_usd":      vol * sp,
            "market_cap_sol":     info.get("marketCapSol", 0),
            "market_cap_usd":     info.get("marketCapSol", 0) * sp,
            "price_usd":          info.get("tokenPrice", 0) * sp,
            "repeat_buy_signal":  repeat_buy_signal,
            "price_floor_held":   price_floor_held,
            "bonding_fill_rate":  bonding_fill_rate,
        }

    def _base_token(self, data: dict) -> dict:
        now = time.time()
        sp  = get_sol_price()
        mc_sol  = float(data.get("marketCapSol", 0) or 0)
        sol_amt = float(data.get("solAmount", 0) or 0)
        return {
            "mint":        data.get("mint", ""),
            "name":        data.get("name", "Unknown"),
            "symbol":      data.get("symbol", "???"),
            "dev_wallet":  data.get("traderPublicKey") or data.get("creator", ""),
            "image_uri":   data.get("image_uri", ""),
            "twitter":     data.get("twitter", ""),
            "website":     data.get("website", ""),
            "description": data.get("description", ""),
            "launchpad":   "pumpfun",
            "created_at":  now,
            "age_seconds": 0,
            "receipt_age_minutes": 0,
            "_receipt_time": now,
            "market_cap_usd":   mc_sol * sp,
            "market_cap_sol":   mc_sol,
            "price_usd":        float(data.get("tokenPrice", 0) or 0) * sp,
            "liquidity_usd":    0.0,
            "price_change_1h":  0.0,
            "price_change_24h": 0.0,
            "volume_5m_usd":    sol_amt * sp,
            "volume_24h_usd":   0.0,
            "tx_buys_5m":       0,
            "tx_sells_5m":      0,
            "total_sol_fees":   sol_amt,
            "holders_count":    0,
            "top10_holders_pct": 0.0,
            "dev_holding_pct":   0.0,
            "insider_pct":       0.0,
            "is_migrated":  False,
            "is_soon":      False,
            "repeat_buy_signal": False,
            "price_floor_held":  False,
            "bonding_fill_rate": 0.0,
            "rug_status":    "Unknown",
            "rug_score":     0,
            "rug_mintable":  False,
            "rug_freezable": False,
            "lp_locked":     False,
            "lp_lock_days":  0,
            "metadata_mutable": False,
            "transfer_fee_pct": 0.0,
            "verified":      False,
            "rug_risks":     [],
            "runner_score":    0,
            "runner_tier":     "",
            "runner_reasons":  [],
            "filter_category": None,
            "mobula_enriched": False,
            "narrative":       "",
        }

    def parse_migration(self, data: dict) -> dict:
        t = self._base_token(data)
        t["is_migrated"] = True
        return t

    def parse_spike(self, data: dict) -> dict:
        sp = get_sol_price()
        t  = self._base_token(data)
        t["tx_buys_5m"]    = int(data.get("_window_buys", 0))
        t["tx_sells_5m"]   = int(data.get("_window_sells", 0))
        vol_sol = float(data.get("_window_vol_sol", 0) or 0)
        t["volume_5m_usd"] = vol_sol * sp
        t["total_sol_fees"]= vol_sol
        return t
