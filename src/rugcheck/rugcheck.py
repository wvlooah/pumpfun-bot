import logging
from typing import Any

import aiohttp

logger = logging.getLogger("rugcheck")

RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class RugCheckAPI:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def check_token(self, mint: str) -> dict:
        """
        Returns a normalised dict:
        {
            score: int (0-100, higher = safer),
            risks: list[str],
            mintable: bool,
            freezable: bool,
            status: str  ("Good" | "Warn" | "Danger" | "Unknown"),
            raw: dict,
        }
        """
        default = {
            "score": 0,
            "risks": ["Unable to fetch rugcheck data"],
            "mintable": True,
            "freezable": True,
            "status": "Unknown",
            "raw": {},
        }
        try:
            session = await self._get_session()
            url = f"{RUGCHECK_BASE}/tokens/{mint}/report/summary"
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"RugCheck {mint}: HTTP {resp.status}")
                    return default
                data = await resp.json()

            score = data.get("score", 0)
            risks_raw = data.get("risks", [])
            risks = [r.get("name", str(r)) for r in risks_raw] if isinstance(risks_raw, list) else []

            # Determine mintable / freezable from risks
            mintable = any("mint" in r.lower() for r in risks)
            freezable = any("freeze" in r.lower() for r in risks)

            # Score interpretation
            if score >= 700:
                status = "Good"
            elif score >= 400:
                status = "Warn"
            else:
                status = "Danger"

            return {
                "score": score,
                "risks": risks if risks else ["None detected"],
                "mintable": mintable,
                "freezable": freezable,
                "status": status,
                "raw": data,
            }

        except Exception as e:
            logger.error(f"RugCheck error for {mint}: {e}")
            return default

    async def passes_safety(self, mint: str) -> tuple[bool, dict]:
        """Returns (passes, report_dict)."""
        report = await self.check_token(mint)
        # Fail if score is Danger
        passes = report["status"] != "Danger"
        return passes, report
