import asyncio
import logging

import aiohttp

logger = logging.getLogger("rugcheck")

RUGCHECK_API = "https://api.rugcheck.xyz/v1"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Fallback on-chain RPC if rugcheck.xyz is down
RPC_ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
]


class RugChecker:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self._rpc_index = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                timeout=REQUEST_TIMEOUT,
            )
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    # ── RugCheck.xyz API (primary) ────────────────────────────────────────────

    async def check_token(self, mint: str) -> dict:
        """
        Try rugcheck.xyz API first — returns rich holder data.
        Falls back to on-chain RPC if unavailable.
        """
        try:
            result = await self._rugcheck_api(mint)
            if result:
                return result
        except Exception as e:
            logger.debug(f"RugCheck API failed: {e}, trying on-chain...")

        return await self._onchain_check(mint)

    async def _rugcheck_api(self, mint: str) -> dict | None:
        session = await self._get_session()
        url = f"{RUGCHECK_API}/tokens/{mint}/report/summary"
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.debug(f"RugCheck API HTTP {resp.status} for {mint[:8]}")
                return None
            data = await resp.json()

        risks_raw = data.get("risks", []) or []
        risk_names = [r.get("name", "") for r in risks_raw if r.get("level") in ("danger", "warn")]

        # Score: rugcheck uses 0-1000 where higher = safer
        score = int(data.get("score", 500) or 500)

        # Top holder %
        top_holders = data.get("topHolders", []) or []
        top10_pct = sum(float(h.get("pct", 0) or 0) for h in top_holders[:10]) * 100
        # rugcheck returns pct as 0-1 decimal
        if top10_pct <= 1.0 and len(top_holders) > 0:
            top10_pct = top10_pct * 100  # convert if needed

        # Mint / freeze
        mintable  = data.get("mintAuthority") is not None
        freezable = data.get("freezeAuthority") is not None

        # Markets / liquidity
        markets = data.get("markets", []) or []
        is_on_dex = len(markets) > 0

        # Insiders from rugcheck
        insider_pct   = float(data.get("insiderNetworkPct", 0) or 0)
        #RugCheck doesn't give snipers/bundles directly — default 0
        snipers_pct   = 0.0
        bundles_pct   = 0.0
        dev_holding   = 0.0

        # Try to get creator holding %
        creator = data.get("creator", "")
        for h in top_holders:
            if h.get("address") == creator or h.get("owner") == creator:
                dev_holding = float(h.get("pct", 0) or 0) * 100
                break

        # Derive status
        danger_risks = [r for r in risks_raw if r.get("level") == "danger"]
        if mintable and freezable:
            status = "Danger"
        elif danger_risks or mintable or freezable or top10_pct >= 80:
            status = "Warn"
        elif score >= 700:
            status = "Good"
        else:
            status = "Warn"

        if not risk_names:
            risk_names = ["No major risks detected"]

        logger.debug(
            f"RugCheck.xyz {mint[:8]}: {status} score={score} "
            f"top10={top10_pct:.0f}% mint={mintable} freeze={freezable}"
        )

        return {
            "score": score,
            "risks": risk_names,
            "mintable": mintable,
            "freezable": freezable,
            "status": status,
            "top_holder_pct": round(top10_pct, 1),
            "insider_pct": round(insider_pct, 1),
            "snipers_pct": snipers_pct,
            "bundles_pct": bundles_pct,
            "dev_holding_pct": round(dev_holding, 1),
            "is_on_dex": is_on_dex,
        }

    # ── On-chain fallback ─────────────────────────────────────────────────────

    async def _onchain_check(self, mint: str) -> dict:
        risks = []
        mintable = False
        freezable = False
        top_holder_pct = 0.0

        try:
            session = await self._get_session()

            async def rpc(method, params):
                payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                for url in RPC_ENDPOINTS:
                    try:
                        async with session.post(url, json=payload) as r:
                            if r.status == 200:
                                d = await r.json()
                                if "result" in d:
                                    return d["result"]
                    except Exception:
                        pass
                return None

            result = await rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
            if result and result.get("value"):
                info = result["value"].get("data", {})
                parsed = info.get("parsed", {}) if isinstance(info, dict) else {}
                mint_info = parsed.get("info", {})
                mintable  = mint_info.get("mintAuthority") is not None
                freezable = mint_info.get("freezeAuthority") is not None
                supply    = int(mint_info.get("supply", 0) or 0)

                if mintable:  risks.append("Mint authority active")
                if freezable: risks.append("Freeze authority active")

                holders_result = await rpc("getTokenLargestAccounts", [mint])
                if holders_result and holders_result.get("value") and supply > 0:
                    largest = holders_result["value"][:10]
                    top_total = sum(int(h.get("amount", 0) or 0) for h in largest)
                    top_holder_pct = (top_total / supply) * 100
                    if top_holder_pct >= 80:
                        risks.append(f"Top 10 own {top_holder_pct:.0f}%")

            score = 1000
            if mintable:          score -= 400
            if freezable:         score -= 300
            if top_holder_pct >= 80: score -= 300
            elif top_holder_pct >= 60: score -= 150
            score = max(0, score)

            if mintable and freezable:  status = "Danger"
            elif mintable or freezable or top_holder_pct >= 80: status = "Warn"
            elif score >= 700: status = "Good"
            else: status = "Warn"

            if not risks: risks = ["No major risks detected"]

            return {
                "score": score, "risks": risks,
                "mintable": mintable, "freezable": freezable,
                "status": status, "top_holder_pct": round(top_holder_pct, 1),
                "insider_pct": 0.0, "snipers_pct": 0.0,
                "bundles_pct": 0.0, "dev_holding_pct": 0.0,
                "is_on_dex": False,
            }

        except Exception as e:
            logger.error(f"On-chain check error {mint}: {e}")
            return self._unknown()

    def _unknown(self) -> dict:
        return {
            "score": 0, "risks": ["Could not fetch on-chain data"],
            "mintable": False, "freezable": False,
            "status": "Unknown", "top_holder_pct": 0.0,
            "insider_pct": 0.0, "snipers_pct": 0.0,
            "bundles_pct": 0.0, "dev_holding_pct": 0.0,
            "is_on_dex": False,
        }
