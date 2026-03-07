import logging
import os

logger = logging.getLogger("scoring")

# ── Three alert tiers ─────────────────────────────────────────────────────────
# Average: early signal, loose filters — catches things fast
# Good:    solid setup, decent volume
# Strong:  high confidence runner

TIER_AVERAGE  = int(os.getenv("SCORE_AVERAGE", "25"))   # Low bar — catch early
TIER_GOOD     = int(os.getenv("SCORE_GOOD",    "45"))
TIER_STRONG   = int(os.getenv("SCORE_STRONG",  "65"))


def get_tier(score: int) -> str | None:
    if score >= TIER_STRONG:
        return "🔥 STRONG"
    if score >= TIER_GOOD:
        return "✅ GOOD"
    if score >= TIER_AVERAGE:
        return "📡 AVERAGE"
    return None


class RunnerScorer:

    def score(self, token: dict) -> tuple[int, list[str]]:
        points = 0
        reasons: list[str] = []

        mc       = token.get("market_cap_usd", 0) or 0
        buys     = token.get("tx_buys_5m", 0) or 0
        sells    = token.get("tx_sells_5m", 0) or 0
        vol      = token.get("volume_5m_usd", 0) or 0
        top10    = token.get("top10_holders_pct", 0) or 0
        dev_pct  = token.get("dev_holding_pct", 0) or 0
        snipers  = token.get("snipers_pct", 0) or 0
        bundles  = token.get("bundles_pct", 0) or 0
        rug      = token.get("rug_status", "Unknown")
        migrated = token.get("is_migrated", False)
        twitter  = token.get("twitter", "")
        website  = token.get("website", "")
        sol_fees = token.get("total_sol_fees", 0) or 0

        # ── Market cap momentum ────────────────────────────────────────────────
        if mc >= 50000:
            points += 20; reasons.append(f"MC ${mc:,.0f}")
        elif mc >= 20000:
            points += 14; reasons.append(f"MC ${mc:,.0f}")
        elif mc >= 8000:
            points += 8
        elif mc >= 3000:
            points += 4

        # ── Buy pressure ──────────────────────────────────────────────────────
        total_tx = buys + sells
        if total_tx > 0:
            buy_ratio = buys / total_tx
            if buy_ratio >= 0.80:
                points += 20; reasons.append(f"Buy pressure {buy_ratio*100:.0f}%")
            elif buy_ratio >= 0.65:
                points += 12; reasons.append(f"Buy pressure {buy_ratio*100:.0f}%")
            elif buy_ratio >= 0.50:
                points += 5

        # ── Transaction count ─────────────────────────────────────────────────
        if total_tx >= 30:
            points += 15; reasons.append(f"{total_tx} txns")
        elif total_tx >= 15:
            points += 10; reasons.append(f"{total_tx} txns")
        elif total_tx >= 5:
            points += 5

        # ── Volume ────────────────────────────────────────────────────────────
        if vol >= 10000:
            points += 15; reasons.append(f"Vol ${vol:,.0f}")
        elif vol >= 3000:
            points += 8
        elif vol >= 500:
            points += 3

        # ── Migration bonus ───────────────────────────────────────────────────
        if migrated:
            points += 10; reasons.append("Migrated ✓")

        # ── SOL fees (bonding curve progress) ────────────────────────────────
        if sol_fees >= 10:
            points += 8; reasons.append(f"{sol_fees:.0f} SOL fees")
        elif sol_fees >= 2:
            points += 4
        elif sol_fees >= 0.5:
            points += 2

        # ── Holder health ─────────────────────────────────────────────────────
        if top10 > 0:
            if top10 <= 20:
                points += 8; reasons.append("Well distributed")
            elif top10 <= 35:
                points += 4

        if snipers <= 10:
            points += 5
        if bundles <= 10:
            points += 3
        if dev_pct <= 5:
            points += 5
        elif dev_pct <= 15:
            points += 2

        # ── Rugcheck ─────────────────────────────────────────────────────────
        if rug == "Good":
            points += 8; reasons.append("RugCheck OK")
        elif rug == "Warn":
            points += 2
        elif rug == "Danger":
            points -= 30  # Penalty but don't zero out — let hard block handle it

        # ── Socials ───────────────────────────────────────────────────────────
        if twitter and website:
            points += 5; reasons.append("Socials ✓")
        elif twitter:
            points += 2

        return max(0, min(points, 100)), reasons

    def passes(self, token: dict) -> tuple[bool, int, str | None, list[str]]:
        """Returns (passes, score, tier_label, reasons)"""
        score, reasons = self.score(token)
        tier = get_tier(score)
        return tier is not None, score, tier, reasons
