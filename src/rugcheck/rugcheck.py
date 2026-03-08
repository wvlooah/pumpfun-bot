"""
RugCheck client — full report endpoint.

Auth:
  Public (no key): GET /v1/tokens/{mint}/report
    → returns cached report, free, no rate issues
  Authenticated (JWT): same endpoint but fresher data
    → set SOLANA_PRIVATE_KEY in .env to enable JWT auth

Real response fields (from production API):
  mint, creator, tokenMeta {name, symbol, mutable, updateAuthority}
  token {supply, decimals}
  mintAuthority   string|null   — not null = can mint more tokens (BAD)
  freezeAuthority string|null   — not null = can freeze wallets (BAD)
  rugged          bool
  score           int  (0-1000+, HIGHER = MORE RISKY on rugcheck)
  risks           [{name, description, level, score, value}]
                    level: "danger"|"warn"|"info"
  topHolders      [{address, amount, decimals, pct, uiAmount, uiAmountString,
                    owner, insider}]
                    pct is 0.0–1.0 (e.g. 0.05 = 5%)
  markets         [{pubkey, marketType, mintA, mintB, liquidityA, liquidityB,
                    lpMint, lpLockedPct, lp {lpLockedPct, lpBurnedPct, ...}}]
  graphInsiderReport  {id, mintAddress, ... holdingPercent, ...} | null
                    holdingPercent is 0.0–1.0
  totalMarketLiquidity  float USD
  lockers         [...]
  verification    {mint, payer, name, symbol, description, jito, ...} | null
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiohttp

logger = logging.getLogger("rugcheck")

RUGCHECK_BASE   = "https://api.rugcheck.xyz/v1"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")

# JWT cache — valid ~2 days per RugCheck docs
_jwt_token: str = ""
_jwt_expiry: float = 0.0


async def _get_jwt() -> str:
    """
    Generate JWT via POST /auth/login/solana.
    Requires SOLANA_PRIVATE_KEY in .env (base58 encoded).
    Message format: {"message":"Sign-in to Rugcheck.xyz","timestamp":<ms>,"publicKey":"<pubkey>"}
    Signature: ed25519 sign of JSON string, returned as list of ints.
    """
    global _jwt_token, _jwt_expiry

    if not SOLANA_PRIVATE_KEY:
        return ""
    if _jwt_token and time.time() < _jwt_expiry - 3600:
        return _jwt_token

    try:
        import base58
        from solders.keypair import Keypair  # type: ignore

        kp = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
        pubkey = str(kp.pubkey())

        msg_data = {
            "message": "Sign-in to Rugcheck.xyz",
            "timestamp": int(time.time() * 1000),
            "publicKey": pubkey,
        }
        # Must be compact JSON (no spaces) to match what they verify
        msg_json = json.dumps(msg_data, separators=(",", ":"))
        msg_bytes = msg_json.encode("utf-8")

        sig = kp.sign_message(msg_bytes)
        sig_b58 = str(sig)
        sig_data = list(base58.b58decode(sig_b58))

        payload = {
            "signature": {"data": sig_data, "type": "ed25519"},
            "wallet": pubkey,
            "message": msg_data,
        }

        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                "https://api.rugcheck.xyz/auth/login/solana",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token = data.get("token") or data.get("jwt") or data.get("access_token","")
                    if token:
                        _jwt_token = token
                        _jwt_expiry = time.time() + 172800  # 2 days
                        logger.info("RugCheck JWT obtained ✓")
                        return _jwt_token
                    logger.warning(f"RugCheck login returned no token: {data}")
                else:
                    txt = await resp.text()
                    logger.warning(f"RugCheck login failed HTTP {resp.status}: {txt[:200]}")
    except ImportError:
        logger.debug("solders/base58 not installed — RugCheck running unauthenticated")
    except Exception as e:
        logger.warning(f"RugCheck JWT error: {e}")
    return ""


class RugChecker:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, tuple[dict, float]] = {}
        self._CACHE_TTL = 300  # 5 min

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
            jwt = await _get_jwt()
            if jwt:
                headers["Authorization"] = f"Bearer {jwt}"
            self._session = aiohttp.ClientSession(headers=headers, timeout=REQUEST_TIMEOUT)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def check_token(self, mint: str) -> dict:
        if not mint:
            return self._unknown()

        cached = self._cache.get(mint)
        if cached and time.time() - cached[1] < self._CACHE_TTL:
            return cached[0]

        result = await self._fetch(mint)
        self._cache[mint] = (result, time.time())

        if len(self._cache) > 3000:
            now = time.time()
            self._cache = {k: v for k, v in self._cache.items() if now - v[1] < self._CACHE_TTL}
        return result

    async def _fetch(self, mint: str) -> dict:
        sess = await self._get_session()

        for endpoint in [
            f"{RUGCHECK_BASE}/tokens/{mint}/report",
            f"{RUGCHECK_BASE}/tokens/{mint}/report/summary",
        ]:
            try:
                async with sess.get(endpoint) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        result = self._parse(data)
                        logger.info(
                            f"RugCheck {mint[:8]}: {result['status']} "
                            f"score={result['score']} "
                            f"top10={result['top10_holders_pct']:.1f}% "
                            f"dev={result['dev_holding_pct']:.1f}% "
                            f"insider={result['insider_pct']:.1f}% "
                            f"mint={result['mintable']} freeze={result['freezable']} "
                            f"lp_lock={result['lp_locked']}"
                        )
                        return result
                    elif resp.status == 429:
                        logger.warning("RugCheck rate limited — sleeping 3s")
                        await asyncio.sleep(3)
                        break
                    else:
                        logger.debug(f"RugCheck HTTP {resp.status} for {mint[:8]} via {endpoint.split('/')[-1]}")
            except asyncio.TimeoutError:
                logger.debug(f"RugCheck timeout for {mint[:8]}")
            except Exception as e:
                logger.debug(f"RugCheck error for {mint[:8]}: {e}")

        return self._unknown()

    def _parse(self, d: dict) -> dict:
        def pct_norm(v):
            """Normalise pct: RugCheck topHolders use 0.0-1.0, convert to 0-100."""
            v = float(v or 0)
            return v * 100 if v <= 1.0 else v

        # ── Mint / Freeze ──────────────────────────────────────────────────────
        mintable  = d.get("mintAuthority")  is not None
        freezable = d.get("freezeAuthority") is not None
        rugged    = bool(d.get("rugged"))

        # ── Score ──────────────────────────────────────────────────────────────
        # RugCheck score: higher = MORE risky (opposite of what you'd expect)
        # We invert it: safe_score = max(0, 1000 - raw_score) for our use
        raw_score  = int(d.get("score") or 0)
        safe_score = max(0, 1000 - raw_score)  # 1000 = safest, 0 = most risky

        # ── Risks ──────────────────────────────────────────────────────────────
        risks_raw    = d.get("risks") or []
        risk_names   = [r["name"] for r in risks_raw if r.get("name")]
        danger_count = sum(1 for r in risks_raw if r.get("level") == "danger")
        warn_count   = sum(1 for r in risks_raw if r.get("level") == "warn")

        # ── Top Holders ────────────────────────────────────────────────────────
        top_holders  = d.get("topHolders") or []
        creator_addr = d.get("creator") or ""

        top10_pct = 0.0
        dev_pct   = 0.0
        insider_holders_pct = 0.0

        for h in top_holders[:20]:  # check top 20 in case creator is not top 10
            p = pct_norm(h.get("pct", 0))
            owner   = h.get("owner","") or h.get("address","") or ""
            is_ins  = bool(h.get("insider"))

            if len(top_holders[:10]) >= 1 and h in top_holders[:10]:
                top10_pct += p

            if owner == creator_addr and dev_pct == 0:
                dev_pct = p

            if is_ins:
                insider_holders_pct += p

        # ── Insider % from graphInsiderReport (more accurate) ─────────────────
        graph = d.get("graphInsiderReport") or {}
        insider_pct = 0.0
        if graph:
            hp = float(graph.get("holdingPercent") or 0)
            insider_pct = hp * 100 if hp <= 1.0 else hp
        if insider_pct == 0 and insider_holders_pct > 0:
            insider_pct = insider_holders_pct

        # ── LP lock ────────────────────────────────────────────────────────────
        markets   = d.get("markets") or []
        lp_locked = False
        for m in markets:
            lp = m.get("lp") or {}
            lp_locked_pct = float(
                lp.get("lpLockedPct") or m.get("lpLockedPct") or 0
            )
            if lp_locked_pct > 0:
                lp_locked = True
                break

        # ── Total liquidity ────────────────────────────────────────────────────
        total_liq = float(d.get("totalMarketLiquidity") or 0)

        # ── Status ────────────────────────────────────────────────────────────
        if rugged or (mintable and freezable) or danger_count >= 2:
            status = "Danger"
        elif danger_count >= 1 or mintable or freezable or top10_pct >= 80:
            status = "Warn"
        elif safe_score >= 700 and danger_count == 0 and warn_count <= 1:
            status = "Good"
        else:
            status = "Warn"

        return {
            "score":              safe_score,      # inverted: 1000 = safe
            "raw_score":          raw_score,        # original rugcheck score
            "risks":              risk_names or ["No major risks detected"],
            "mintable":           mintable,
            "freezable":          freezable,
            "lp_locked":          lp_locked,
            "status":             status,
            "top10_holders_pct":  round(min(top10_pct, 100.0), 2),
            "dev_holding_pct":    round(min(dev_pct, 100.0), 2),
            "insider_pct":        round(min(insider_pct, 100.0), 2),
            "total_liquidity_usd": total_liq,
            "rugged":             rugged,
        }

    def _unknown(self) -> dict:
        return {
            "score": 0, "raw_score": 0,
            "risks": ["Could not fetch rug data"],
            "mintable": False, "freezable": False,
            "lp_locked": False, "status": "Unknown",
            "top10_holders_pct": 0.0,
            "dev_holding_pct": 0.0,
            "insider_pct": 0.0,
            "total_liquidity_usd": 0.0,
            "rugged": False,
        }
