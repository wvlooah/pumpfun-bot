"""
RugCheck integration.
Primary:  GET https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary  (no auth needed)
Fallback: GET https://api.rugcheck.xyz/v1/tokens/{mint}/report          (full report)

JWT auth is supported on the paid tier. We attempt unauthenticated first
(summary endpoint is public), then sign with Solana key if env var is set.

Fields extracted:
  score (0-1000, higher = safer)
  risks: list of risk strings
  mintable, freezable
  top_holder_pct, insider_pct, dev_holding_pct
  lp_locked (bool)
  status: "Good" | "Warn" | "Danger" | "Unknown"
"""

import asyncio
import base64
import json
import logging
import os
import time
from typing import Optional

import aiohttp

logger = logging.getLogger("rugcheck")

RUGCHECK_BASE    = "https://api.rugcheck.xyz/v1"
REQUEST_TIMEOUT  = aiohttp.ClientTimeout(total=20)

# Optional Solana private key for JWT auth (paid tier)
_SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")

# JWT cache
_jwt_token: str = ""
_jwt_expiry: float = 0.0


def _generate_jwt() -> str:
    """
    Generate a RugCheck JWT by signing a message with the Solana private key.
    Only used if SOLANA_PRIVATE_KEY is set in .env.
    Falls back gracefully if solders/nacl not installed.
    """
    global _jwt_token, _jwt_expiry

    if not _SOLANA_PRIVATE_KEY:
        return ""

    # Return cached token if still valid (5 min buffer)
    if _jwt_token and time.time() < _jwt_expiry - 300:
        return _jwt_token

    try:
        from solders.keypair import Keypair  # type: ignore
        import base58

        raw = base58.b58decode(_SOLANA_PRIVATE_KEY)
        kp = Keypair.from_bytes(raw)

        # RugCheck expects: sign the message "rugcheck:{timestamp}"
        ts = int(time.time())
        message = f"rugcheck:{ts}".encode()
        sig = kp.sign_message(message)
        sig_b64 = base64.b64encode(bytes(sig)).decode()
        pub_b58 = str(kp.pubkey())

        # Build JWT payload (RugCheck uses a simple base64 scheme)
        payload = {
            "publicKey": pub_b58,
            "signature": sig_b64,
            "timestamp": ts,
        }
        payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        _jwt_token = payload_b64
        _jwt_expiry = time.time() + 3600  # 1 hour
        logger.info("RugCheck JWT generated successfully")
        return _jwt_token

    except ImportError:
        logger.debug("solders/base58 not installed — using unauthenticated RugCheck")
        return ""
    except Exception as e:
        logger.warning(f"JWT generation failed: {e}")
        return ""


class RugChecker:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, tuple[dict, float]] = {}  # mint -> (report, timestamp)
        self._CACHE_TTL = 300  # 5 minutes

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            }
            jwt = _generate_jwt()
            if jwt:
                headers["Authorization"] = f"Bearer {jwt}"
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def check_token(self, mint: str) -> dict:
        if not mint:
            return self._unknown()

        # Check cache
        cached = self._cache.get(mint)
        if cached:
            report, ts = cached
            if time.time() - ts < self._CACHE_TTL:
                return report

        result = await self._fetch_report(mint)
        self._cache[mint] = (result, time.time())

        # Trim cache
        if len(self._cache) > 2000:
            now = time.time()
            self._cache = {k: v for k, v in self._cache.items()
                           if now - v[1] < self._CACHE_TTL}
        return result

    async def _fetch_report(self, mint: str) -> dict:
        """Try summary endpoint first, then full report as fallback."""
        session = await self._get_session()

        # Try summary (public, fast)
        try:
            url = f"{RUGCHECK_BASE}/tokens/{mint}/report/summary"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return self._parse_summary(data, mint)
                elif resp.status == 429:
                    logger.warning(f"RugCheck rate limited for {mint[:8]}")
                    await asyncio.sleep(2)
                else:
                    logger.debug(f"RugCheck summary HTTP {resp.status} for {mint[:8]}")
        except asyncio.TimeoutError:
            logger.debug(f"RugCheck summary timeout for {mint[:8]}")
        except Exception as e:
            logger.debug(f"RugCheck summary error: {e}")

        # Try full report
        try:
            url = f"{RUGCHECK_BASE}/tokens/{mint}/report"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return self._parse_full(data, mint)
                logger.debug(f"RugCheck full report HTTP {resp.status} for {mint[:8]}")
        except Exception as e:
            logger.debug(f"RugCheck full report error: {e}")

        return self._unknown()

    def _parse_summary(self, data: dict, mint: str) -> dict:
        """Parse /report/summary response."""
        risks_raw = data.get("risks") or []
        risk_names = []
        danger_count = 0
        warn_count = 0

        for r in risks_raw:
            level = r.get("level", "")
            name  = r.get("name", "")
            if name:
                risk_names.append(name)
            if level == "danger":
                danger_count += 1
            elif level == "warn":
                warn_count += 1

        score      = int(data.get("score", 500) or 500)
        mintable   = data.get("mintAuthority") is not None
        freezable  = data.get("freezeAuthority") is not None

        # Top holders
        top_holders = data.get("topHolders") or []
        total_supply = float(data.get("totalSupply") or data.get("supply") or 1)
        top10_pct = 0.0
        if top_holders:
            raw_pct = sum(float(h.get("pct", 0) or 0) for h in top_holders[:10])
            # Mobula returns 0-1 decimals, rugcheck may return 0-100
            top10_pct = raw_pct * 100 if raw_pct <= 1.0 else raw_pct

        # LP lock
        markets = data.get("markets") or []
        lp_locked = any(m.get("lpLocked") or m.get("lp_locked") for m in markets)

        # Insider pct
        insider_pct = float(data.get("insiderNetworkPct") or 0) * 100
        if insider_pct > 100:
            insider_pct = insider_pct / 100

        # Dev holding — find creator in top holders
        creator = data.get("creator") or ""
        dev_pct = 0.0
        for h in top_holders:
            if h.get("address") == creator or h.get("owner") == creator:
                dev_pct = float(h.get("pct", 0) or 0)
                if dev_pct <= 1.0:
                    dev_pct *= 100
                break

        # Derive status
        if (mintable and freezable) or danger_count >= 2:
            status = "Danger"
        elif danger_count >= 1 or mintable or freezable or top10_pct >= 80:
            status = "Warn"
        elif score >= 700 and warn_count == 0:
            status = "Good"
        else:
            status = "Warn"

        if not risk_names:
            risk_names = ["No major risks detected"]

        report = {
            "score":           score,
            "risks":           risk_names,
            "mintable":        mintable,
            "freezable":       freezable,
            "lp_locked":       lp_locked,
            "status":          status,
            "top_holder_pct":  round(top10_pct, 1),
            "insider_pct":     round(insider_pct, 1),
            "dev_holding_pct": round(dev_pct, 1),
            "snipers_pct":     0.0,
            "bundles_pct":     0.0,
        }
        logger.debug(
            f"RugCheck {mint[:8]}: {status} score={score} "
            f"top10={top10_pct:.0f}% mint={mintable} freeze={freezable} lp_lock={lp_locked}"
        )
        return report

    def _parse_full(self, data: dict, mint: str) -> dict:
        """Parse /report response (same structure, just more fields)."""
        return self._parse_summary(data, mint)

    def _unknown(self) -> dict:
        return {
            "score": 0, "risks": ["Could not fetch rugcheck data"],
            "mintable": False, "freezable": False, "lp_locked": False,
            "status": "Unknown", "top_holder_pct": 0.0,
            "insider_pct": 0.0, "dev_holding_pct": 0.0,
            "snipers_pct": 0.0, "bundles_pct": 0.0,
        }
