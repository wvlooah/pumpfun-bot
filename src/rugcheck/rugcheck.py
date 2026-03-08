"""
RugCheck client — full report with deep field extraction.

Fields we now extract:
  score, risks, mintAuthority, freezeAuthority, rugged
  topHolders[].pct          -> top10_holders_pct, dev_holding_pct
  graphInsiderReport        -> insider_pct
  markets[].lp.lpLockedPct  -> lp_locked, lp_lock_days
  tokenMeta.mutable         -> metadata_mutable
  token_extensions / transferFee -> transfer_fee_pct
  verification              -> verified (bool)
  markets[].liquidityA/B    -> total_liquidity_usd
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiohttp

logger = logging.getLogger("rugcheck")

RUGCHECK_BASE      = "https://api.rugcheck.xyz/v1"
REQUEST_TIMEOUT    = aiohttp.ClientTimeout(total=20)
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")

_jwt_token: str  = ""
_jwt_expiry: float = 0.0


async def _get_jwt() -> str:
    global _jwt_token, _jwt_expiry
    if not SOLANA_PRIVATE_KEY:
        return ""
    if _jwt_token and time.time() < _jwt_expiry - 3600:
        return _jwt_token
    try:
        import base58
        from solders.keypair import Keypair  # type: ignore
        kp     = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
        pubkey = str(kp.pubkey())
        msg    = {"message": "Sign-in to Rugcheck.xyz",
                  "timestamp": int(time.time() * 1000),
                  "publicKey": pubkey}
        sig      = kp.sign_message(json.dumps(msg, separators=(",", ":")).encode())
        sig_data = list(base58.b58decode(str(sig)))
        payload  = {"signature": {"data": sig_data, "type": "ed25519"},
                    "wallet": pubkey, "message": msg}
        async with aiohttp.ClientSession() as s:
            async with s.post("https://api.rugcheck.xyz/auth/login/solana",
                              json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    d = await r.json()
                    tok = d.get("token") or d.get("jwt") or ""
                    if tok:
                        _jwt_token  = tok
                        _jwt_expiry = time.time() + 172800
                        logger.info("RugCheck JWT ✓")
                        return tok
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"JWT error: {e}")
    return ""


class RugChecker:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, tuple[dict, float]] = {}
        self._CACHE_TTL = 300

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            hdrs = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
            jwt  = await _get_jwt()
            if jwt: hdrs["Authorization"] = f"Bearer {jwt}"
            self._session = aiohttp.ClientSession(headers=hdrs, timeout=REQUEST_TIMEOUT)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def check_token(self, mint: str) -> dict:
        if not mint: return self._unknown()
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
        sess = await self._sess()
        for endpoint in [f"{RUGCHECK_BASE}/tokens/{mint}/report",
                         f"{RUGCHECK_BASE}/tokens/{mint}/report/summary"]:
            try:
                async with sess.get(endpoint) as resp:
                    if resp.status == 200:
                        data   = await resp.json(content_type=None)
                        result = self._parse(data)
                        logger.info(
                            f"RugCheck {mint[:8]}: {result['status']} "
                            f"top10={result['top10_holders_pct']:.1f}% "
                            f"dev={result['dev_holding_pct']:.1f}% "
                            f"insider={result['insider_pct']:.1f}% "
                            f"mint={result['mintable']} freeze={result['freezable']} "
                            f"lp_lock={result['lp_locked']}({result['lp_lock_days']}d) "
                            f"mutable={result['metadata_mutable']} "
                            f"fee={result['transfer_fee_pct']:.1f}% "
                            f"verified={result['verified']}"
                        )
                        return result
                    elif resp.status == 429:
                        logger.warning("RugCheck rate limited — sleeping 3s")
                        await asyncio.sleep(3)
                        break
                    else:
                        logger.debug(f"RugCheck HTTP {resp.status} for {mint[:8]}")
            except asyncio.TimeoutError:
                logger.debug(f"RugCheck timeout {mint[:8]}")
            except Exception as e:
                logger.debug(f"RugCheck error {mint[:8]}: {e}")
        return self._unknown()

    def _parse(self, d: dict) -> dict:
        def pn(v):   # pct normalise 0-1 → 0-100
            v = float(v or 0)
            return v * 100 if v <= 1.0 else v

        # ── Basic flags ────────────────────────────────────────────────────────
        mintable  = d.get("mintAuthority")  is not None
        freezable = d.get("freezeAuthority") is not None
        rugged    = bool(d.get("rugged"))
        raw_score = int(d.get("score") or 0)
        safe_score = max(0, 1000 - raw_score)

        # ── Metadata mutability ────────────────────────────────────────────────
        token_meta = d.get("tokenMeta") or {}
        metadata_mutable = bool(token_meta.get("mutable", False))

        # ── Transfer fee ──────────────────────────────────────────────────────
        transfer_fee_pct = 0.0
        ext = d.get("token_extensions") or d.get("tokenExtensions") or {}
        if ext:
            tf = ext.get("transferFee") or ext.get("transfer_fee") or {}
            if tf:
                # transferFeeBasisPoints: 100 = 1%
                bp = float(tf.get("transferFeeBasisPoints") or tf.get("basisPoints") or 0)
                transfer_fee_pct = bp / 100.0
        # Also check risks for transfer fee mention
        risks_raw = d.get("risks") or []
        if transfer_fee_pct == 0:
            for r in risks_raw:
                if "transfer" in r.get("name", "").lower() and "fee" in r.get("name", "").lower():
                    val = r.get("value", "0%").replace("%", "")
                    try: transfer_fee_pct = float(val)
                    except: pass

        # ── Verification ──────────────────────────────────────────────────────
        verification = d.get("verification")
        verified = verification is not None and bool(verification)

        # ── Risks ─────────────────────────────────────────────────────────────
        risk_names   = [r["name"] for r in risks_raw if r.get("name")]
        danger_count = sum(1 for r in risks_raw if r.get("level") == "danger")
        warn_count   = sum(1 for r in risks_raw if r.get("level") == "warn")

        # ── Top holders ───────────────────────────────────────────────────────
        top_holders  = d.get("topHolders") or []
        creator_addr = d.get("creator") or ""
        top10_pct    = sum(pn(h.get("pct", 0)) for h in top_holders[:10])
        dev_pct      = 0.0
        insider_h_pct = 0.0
        for h in top_holders:
            owner = h.get("owner","") or h.get("address","") or ""
            p = pn(h.get("pct", 0))
            if owner == creator_addr and dev_pct == 0:
                dev_pct = p
            if h.get("insider"):
                insider_h_pct += p

        # ── Insider from graphInsiderReport ───────────────────────────────────
        graph = d.get("graphInsiderReport") or {}
        insider_pct = pn(graph.get("holdingPercent", 0)) if graph else insider_h_pct

        # ── LP lock — duration matters ─────────────────────────────────────────
        markets      = d.get("markets") or []
        lp_locked    = False
        lp_lock_days = 0
        for m in markets:
            lp = m.get("lp") or {}
            locked_pct = float(lp.get("lpLockedPct") or m.get("lpLockedPct") or 0)
            if locked_pct > 0:
                lp_locked = True
                # Try to get unlock time
                unlock_ts = lp.get("lpLockExpiry") or lp.get("unlockDate") or 0
                if unlock_ts:
                    days = max(0, (float(unlock_ts) - time.time()) / 86400)
                    lp_lock_days = max(lp_lock_days, int(days))

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
            "score":              safe_score,
            "raw_score":          raw_score,
            "risks":              risk_names or ["No major risks detected"],
            "mintable":           mintable,
            "freezable":          freezable,
            "lp_locked":          lp_locked,
            "lp_lock_days":       lp_lock_days,
            "metadata_mutable":   metadata_mutable,
            "transfer_fee_pct":   round(transfer_fee_pct, 2),
            "verified":           verified,
            "status":             status,
            "top10_holders_pct":  round(min(top10_pct, 100), 2),
            "dev_holding_pct":    round(min(dev_pct, 100), 2),
            "insider_pct":        round(min(insider_pct, 100), 2),
            "total_liquidity_usd": total_liq,
            "rugged":             rugged,
        }

    def _unknown(self) -> dict:
        return {
            "score": 0, "raw_score": 0, "risks": ["Could not fetch rug data"],
            "mintable": False, "freezable": False, "lp_locked": False,
            "lp_lock_days": 0, "metadata_mutable": False,
            "transfer_fee_pct": 0.0, "verified": False,
            "status": "Unknown", "top10_holders_pct": 0.0,
            "dev_holding_pct": 0.0, "insider_pct": 0.0,
            "total_liquidity_usd": 0.0, "rugged": False,
        }
