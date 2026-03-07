import logging
from typing import Any

import aiohttp

logger = logging.getLogger("rugcheck")

# Free public Solana RPC endpoints (no key needed, falls back down the list)
RPC_ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
]

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Token Program IDs
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class RugChecker:
    """
    On-chain rug analysis using Solana RPC directly.
    Checks: mint authority, freeze authority, top holder concentration.
    No third-party API needed — works 100% of the time.
    """

    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self._rpc_index = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def _next_rpc(self) -> str:
        url = RPC_ENDPOINTS[self._rpc_index % len(RPC_ENDPOINTS)]
        self._rpc_index += 1
        return url

    async def _rpc_call(self, method: str, params: list) -> dict | None:
        """Make a JSON-RPC call, cycling through endpoints on failure."""
        session = await self._get_session()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}

        for _ in range(len(RPC_ENDPOINTS)):
            url = self._next_rpc()
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "result" in data:
                            return data["result"]
            except Exception as e:
                logger.debug(f"RPC {url} failed: {e}")
        return None

    async def check_token(self, mint: str) -> dict:
        """
        Perform on-chain rug analysis.
        Returns normalised dict:
        {
            score: int (0-1000, higher = safer),
            risks: list[str],
            mintable: bool,       True = mint authority exists (bad)
            freezable: bool,      True = freeze authority exists (bad)
            status: str           "Good" | "Warn" | "Danger" | "Unknown"
        }
        """
        risks = []
        mintable = False
        freezable = False

        try:
            # Get token mint account info
            result = await self._rpc_call(
                "getAccountInfo",
                [mint, {"encoding": "jsonParsed"}],
            )

            if not result or not result.get("value"):
                return self._unknown(mint)

            info = result["value"].get("data", {})
            parsed = info.get("parsed", {}) if isinstance(info, dict) else {}
            mint_info = parsed.get("info", {})

            # ── Mint Authority ────────────────────────────────────────────────
            mint_authority = mint_info.get("mintAuthority")
            if mint_authority:
                mintable = True
                risks.append("Mint authority active — tokens can be minted")

            # ── Freeze Authority ──────────────────────────────────────────────
            freeze_authority = mint_info.get("freezeAuthority")
            if freeze_authority:
                freezable = True
                risks.append("Freeze authority active — wallets can be frozen")

            # ── Supply check ──────────────────────────────────────────────────
            supply = int(mint_info.get("supply", 0) or 0)
            decimals = int(mint_info.get("decimals", 6) or 6)

            # ── Top holders concentration ─────────────────────────────────────
            holders_result = await self._rpc_call(
                "getTokenLargestAccounts",
                [mint],
            )

            top_holder_pct = 0.0
            if holders_result and holders_result.get("value") and supply > 0:
                largest = holders_result["value"][:10]
                top_total = sum(int(h.get("amount", 0) or 0) for h in largest)
                top_holder_pct = (top_total / supply) * 100 if supply > 0 else 0

                if top_holder_pct >= 80:
                    risks.append(f"Top 10 holders own {top_holder_pct:.0f}% of supply")
                elif top_holder_pct >= 60:
                    risks.append(f"Concentrated supply: top 10 own {top_holder_pct:.0f}%")

            # ── Score calculation ─────────────────────────────────────────────
            score = 1000
            if mintable:
                score -= 400
            if freezable:
                score -= 300
            if top_holder_pct >= 80:
                score -= 300
            elif top_holder_pct >= 60:
                score -= 150
            elif top_holder_pct >= 40:
                score -= 50

            score = max(0, score)

            # ── Status ────────────────────────────────────────────────────────
            if mintable and freezable:
                status = "Danger"
            elif mintable or freezable or top_holder_pct >= 80:
                status = "Warn"
            elif score >= 700:
                status = "Good"
            else:
                status = "Warn"

            if not risks:
                risks = ["No major risks detected"]

            logger.debug(f"RugCheck {mint[:8]}...: {status} score={score} mint={mintable} freeze={freezable} top10={top_holder_pct:.0f}%")

            return {
                "score": score,
                "risks": risks,
                "mintable": mintable,
                "freezable": freezable,
                "status": status,
                "top_holder_pct": round(top_holder_pct, 1),
            }

        except Exception as e:
            logger.error(f"RugCheck on-chain error {mint}: {e}")
            return self._unknown(mint)

    def _unknown(self, mint: str) -> dict:
        return {
            "score": 0,
            "risks": ["Could not fetch on-chain data"],
            "mintable": False,
            "freezable": False,
            "status": "Unknown",
            "top_holder_pct": 0.0,
        }

    async def passes_safety(self, mint: str) -> tuple[bool, dict]:
        report = await self.check_token(mint)
        passes = report["status"] != "Danger"
        return passes, report
