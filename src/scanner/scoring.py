import logging
from typing import Any

logger = logging.getLogger("scoring")

# Minimum score to trigger an alert (0-100)
MIN_SCORE = int(__import__("os").getenv("MIN_RUNNER_SCORE", "55"))


class RunnerScorer:
    """
    Scores a token 0-100 based on momentum indicators.
    Higher = more likely to run.
    """

    def score(self, token: dict) -> tuple[int, list[str]]:
        points = 0
        reasons: list[str] = []

        # ── Volume / Price momentum ───────────────────────────────────────────
        price_5m = token.get("price_change_5m", 0) or 0
        vol_5m = token.get("volume_5m_usd", 0) or 0
        vol_1h = token.get("volume_1h_usd", 0) or 0

        if price_5m >= 50:
            points += 20
            reasons.append(f"+{price_5m:.0f}% in 5m")
        elif price_5m >= 20:
            points += 12
            reasons.append(f"+{price_5m:.0f}% in 5m")
        elif price_5m > 0:
            points += 5

        if vol_5m >= 10000:
            points += 15
            reasons.append(f"High 5m vol ${vol_5m:,.0f}")
        elif vol_5m >= 3000:
            points += 8

        # ── Buy pressure ──────────────────────────────────────────────────────
        buys = token.get("tx_buys_5m", 0) or 0
        sells = token.get("tx_sells_5m", 0) or 0
        total_tx = buys + sells
        if total_tx > 0:
            buy_ratio = buys / total_tx
            if buy_ratio >= 0.75:
                points += 15
                reasons.append(f"Buy pressure {buy_ratio*100:.0f}%")
            elif buy_ratio >= 0.60:
                points += 8

        # ── Holder health ─────────────────────────────────────────────────────
        top10 = token.get("top10_holders_pct", 100) or 100
        snipers = token.get("snipers_pct", 100) or 100
        dev_pct = token.get("dev_holding_pct", 100) or 100
        bundles = token.get("bundles_pct", 100) or 100

        if top10 <= 20:
            points += 15
            reasons.append("Well distributed")
        elif top10 <= 35:
            points += 8

        if snipers <= 10:
            points += 10
            reasons.append("Low sniper %")
        elif snipers <= 20:
            points += 5

        if dev_pct <= 5:
            points += 10
            reasons.append("Low dev holding")
        elif dev_pct <= 15:
            points += 5

        if bundles <= 10:
            points += 5

        # ── Rugcheck bonus ────────────────────────────────────────────────────
        rug_status = token.get("rug_status", "Unknown")
        if rug_status == "Good":
            points += 10
            reasons.append("RugCheck OK")
        elif rug_status == "Warn":
            points += 2

        # ── Socials ───────────────────────────────────────────────────────────
        if token.get("twitter") and token.get("website"):
            points += 5
            reasons.append("Has socials")
        elif token.get("twitter"):
            points += 2

        score = min(points, 100)
        return score, reasons

    def passes(self, token: dict) -> tuple[bool, int, list[str]]:
        score, reasons = self.score(token)
        return score >= MIN_SCORE, score, reasons
