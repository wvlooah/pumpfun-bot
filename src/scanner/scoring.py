"""
Runner scoring — early predictive signals only.

Data sources per field:
  PumpPortal  → tx_buys_5m, tx_sells_5m, volume_5m_usd, market_cap_usd (initial),
                total_sol_fees (bonding curve fill), is_migrated, socials
  Mobula REST → market_cap_usd (better), liquidity_usd, volume_24h_usd,
                price_change_1h, price_change_24h,
                mobula_vol_change_24h (vol accel vs yesterday),
                mobula_organic_ratio (on-chain / total — bot filter proxy),
                mobula_vol_accel (True if vol_change_24h > 50%)
  RugCheck    → top10_holders_pct, dev_holding_pct, insider_pct,
                mintable, freezable, lp_locked, status, score

Scoring is grouped:
  IGNITION  — early momentum signals (happening right now)
  STRUCTURE — token is clean enough to actually run
  FUEL      — capacity to sustain a move

Max theoretical score: ~100 (capped at 100, min 0)
"""

import logging
import os

logger = logging.getLogger("scoring")

TIER_AVERAGE = int(os.getenv("SCORE_AVERAGE", "25"))
TIER_GOOD    = int(os.getenv("SCORE_GOOD",    "45"))
TIER_STRONG  = int(os.getenv("SCORE_STRONG",  "65"))


def get_tier(score: int) -> str | None:
    if score >= TIER_STRONG:  return "🔥 STRONG"
    if score >= TIER_GOOD:    return "✅ GOOD"
    if score >= TIER_AVERAGE: return "📡 AVERAGE"
    return None


class RunnerScorer:

    def score(self, token: dict) -> tuple[int, list[str]]:
        pts = 0
        reasons: list[str] = []

        # ── Pull every field we have ───────────────────────────────────────────
        mc          = token.get("market_cap_usd", 0) or 0
        liquidity   = token.get("liquidity_usd", 0) or 0
        vol_5m      = token.get("volume_5m_usd", 0) or 0
        vol_24h     = token.get("volume_24h_usd", 0) or 0
        pc_1h       = token.get("price_change_1h", 0) or 0
        pc_24h      = token.get("price_change_24h", 0) or 0
        buys        = token.get("tx_buys_5m", 0) or 0
        sells       = token.get("tx_sells_5m", 0) or 0
        sol_fees    = token.get("total_sol_fees", 0) or 0

        # Holder data from RugCheck
        top10       = token.get("top10_holders_pct", 0) or 0
        dev_pct     = token.get("dev_holding_pct", 0) or 0
        insider_pct = token.get("insider_pct", 0) or 0

        # Mobula enrichment
        vol_change     = token.get("mobula_vol_change_24h", 0) or 0  # % vs yesterday
        organic_ratio  = token.get("mobula_organic_ratio", 1.0)       # 0-1, higher = more organic
        vol_accel_flag = token.get("mobula_vol_accel", False)          # True if vol > 50% above yesterday
        liq_change     = token.get("mobula_liq_change_24h", 0) or 0

        # Status
        rug         = token.get("rug_status", "Unknown")
        rug_score   = token.get("rug_score", 0) or 0
        lp_locked   = token.get("lp_locked", False)
        mintable    = token.get("rug_mintable", False)
        freezable   = token.get("rug_freezable", False)
        migrated    = token.get("is_migrated", False)
        is_soon     = token.get("is_soon", False)
        category    = token.get("filter_category", "new_pair")
        twitter     = bool(token.get("twitter"))
        website     = bool(token.get("website"))
        enriched    = token.get("mobula_enriched", False)

        # Derived
        total_tx      = buys + sells
        buy_ratio     = buys / total_tx if total_tx > 0 else 0.0
        liq_mc_ratio  = liquidity / mc if mc > 0 else 0.0

        # ═══════════════════════════════════════════════════════════════════════
        # IGNITION SIGNALS — is something starting to move RIGHT NOW?
        # ═══════════════════════════════════════════════════════════════════════

        # ── Volume acceleration vs yesterday (Mobula) ─────────────────────────
        # vol_change is % change: +200 means today's vol is 3x yesterday's
        # This is the clearest early runner signal — volume waking up
        if vol_accel_flag and vol_change >= 200:
            pts += 20; reasons.append(f"Vol +{vol_change:.0f}% vs yesterday 🚀")
        elif vol_accel_flag and vol_change >= 100:
            pts += 14; reasons.append(f"Vol +{vol_change:.0f}% vs yesterday")
        elif vol_accel_flag:
            pts += 8;  reasons.append(f"Vol accelerating +{vol_change:.0f}%")
        elif vol_change > 20:
            pts += 3

        # ── 5m buy pressure (PumpPortal live data) ────────────────────────────
        # buy_ratio + count together = real demand, not a single bot
        if buy_ratio >= 0.85 and total_tx >= 15:
            pts += 15; reasons.append(f"Buy pressure {buy_ratio*100:.0f}% ({total_tx} txns)")
        elif buy_ratio >= 0.80 and total_tx >= 8:
            pts += 10; reasons.append(f"Buy pressure {buy_ratio*100:.0f}%")
        elif buy_ratio >= 0.70 and total_tx >= 5:
            pts += 5
        elif buy_ratio >= 0.60 and total_tx >= 3:
            pts += 2

        # ── Price momentum (Mobula 1h) ────────────────────────────────────────
        # 1h change: small move early is more predictive than a huge move late
        if 10 <= pc_1h < 50:
            pts += 10; reasons.append(f"+{pc_1h:.0f}% 1h (early move)")
        elif pc_1h >= 50:
            pts += 6;  reasons.append(f"+{pc_1h:.0f}% 1h")  # may already be late
        elif 3 <= pc_1h < 10:
            pts += 4

        # ── Organic volume ratio (Mobula) ─────────────────────────────────────
        # High on-chain vs off-chain = real DEX activity, not wash trading
        if enriched:
            if organic_ratio >= 0.90:
                pts += 10; reasons.append(f"Organic vol {organic_ratio*100:.0f}%")
            elif organic_ratio >= 0.70:
                pts += 6
            elif organic_ratio >= 0.50:
                pts += 2
            elif organic_ratio < 0.20:
                pts -= 5  # mostly off-chain / bot volume

        # ── Liquidity growing (Mobula liq change) ────────────────────────────
        # LP increasing while price rises = organic buyers adding liquidity
        if liq_change > 50:
            pts += 8; reasons.append(f"Liq +{liq_change:.0f}% 24h")
        elif liq_change > 20:
            pts += 4
        elif liq_change < -30:
            pts -= 6  # LP being pulled while price up = red flag

        # ── 5m volume signal (PumpPortal) ────────────────────────────────────
        # Volume relative to market cap (turnover) — high turnover at low MC = hot
        if mc > 0 and vol_5m > 0:
            turnover_5m = vol_5m / mc
            if turnover_5m >= 0.10:
                pts += 8; reasons.append(f"5m turnover {turnover_5m*100:.0f}%")
            elif turnover_5m >= 0.05:
                pts += 4
            elif turnover_5m >= 0.02:
                pts += 2

        # ═══════════════════════════════════════════════════════════════════════
        # STRUCTURE SIGNALS — is this token actually safe to run?
        # ═══════════════════════════════════════════════════════════════════════

        # ── Top 10 holder concentration (RugCheck) ────────────────────────────
        # Lower = harder for whales to dump it on you
        if top10 > 0:
            if top10 <= 15:
                pts += 12; reasons.append(f"Top10 {top10:.0f}% — well spread 💎")
            elif top10 <= 25:
                pts += 8;  reasons.append(f"Top10 {top10:.0f}%")
            elif top10 <= 40:
                pts += 3
            elif top10 > 70:
                pts -= 10; reasons.append(f"Top10 {top10:.0f}% — concentrated ⚠️")

        # ── Dev holding (RugCheck) ────────────────────────────────────────────
        if dev_pct > 0:
            if dev_pct <= 3:
                pts += 8; reasons.append(f"Dev {dev_pct:.1f}% ✓")
            elif dev_pct <= 8:
                pts += 4
            elif dev_pct <= 15:
                pts += 1
            else:
                pts -= 8; reasons.append(f"Dev {dev_pct:.0f}% ⚠️")

        # ── Insider concentration (RugCheck) ──────────────────────────────────
        if insider_pct > 0:
            if insider_pct <= 5:
                pts += 5
            elif insider_pct <= 15:
                pts += 2
            elif insider_pct > 30:
                pts -= 6; reasons.append(f"Insiders {insider_pct:.0f}% ⚠️")

        # ── Mintable / Freezable (RugCheck) ───────────────────────────────────
        if mintable:
            pts -= 10; reasons.append("Mintable ❌")
        if freezable:
            pts -= 8; reasons.append("Freezable ❌")

        # ── RugCheck score + status ───────────────────────────────────────────
        if rug == "Good":
            pts += 8; reasons.append("RugCheck ✓")
        elif rug == "Warn":
            pts += 2
        elif rug == "Danger":
            pts -= 25  # shouldn't reach here (blocked in scanner) but safety net

        if lp_locked:
            pts += 5; reasons.append("LP Locked ✓")

        # ── Socials (PumpPortal metadata) ─────────────────────────────────────
        # Projects that run almost always have socials — it's a legitimacy signal
        if twitter and website:
            pts += 6; reasons.append("Socials ✓")
        elif twitter or website:
            pts += 3

        # ═══════════════════════════════════════════════════════════════════════
        # FUEL SIGNALS — does this token have enough capacity to actually run?
        # ═══════════════════════════════════════════════════════════════════════

        # ── Liquidity / MC ratio (Mobula) ─────────────────────────────────────
        # Higher = deeper book = slippage resistance = sustainable move
        if liquidity > 0 and mc > 0:
            if liq_mc_ratio >= 0.20:
                pts += 8; reasons.append(f"Liq/MC {liq_mc_ratio*100:.0f}% — deep")
            elif liq_mc_ratio >= 0.10:
                pts += 5; reasons.append(f"Liq/MC {liq_mc_ratio*100:.0f}%")
            elif liq_mc_ratio >= 0.05:
                pts += 2
            elif liq_mc_ratio < 0.02:
                pts -= 3  # very thin liquidity = easy to move but easy to dump

        # ── MC sweet spot ─────────────────────────────────────────────────────
        # Best runners: big enough to be noticed, small enough to 10x
        if 5_000 <= mc <= 30_000:
            pts += 6; reasons.append(f"Sweet spot ${mc/1000:.0f}K MC")
        elif 30_000 < mc <= 100_000:
            pts += 3
        elif mc > 500_000:
            pts -= 5  # may already have pumped

        # ── Bonding curve progress (PumpPortal) ───────────────────────────────
        # More SOL committed = more real buyer conviction
        if sol_fees >= 60:
            pts += 6; reasons.append(f"{sol_fees:.0f} SOL bonded")
        elif sol_fees >= 20:
            pts += 3
        elif sol_fees >= 5:
            pts += 1

        # ── Migration status ──────────────────────────────────────────────────
        # Migrated = survived bonding curve = proven demand
        if migrated:
            pts += 8; reasons.append("Migrated ✓")
        elif is_soon or category == "soon_migrate":
            pts += 4; reasons.append("Soon to migrate ⚡")

        return max(0, min(pts, 100)), reasons

    def passes(self, token: dict) -> tuple[bool, int, str | None, list[str]]:
        score, reasons = self.score(token)
        tier = get_tier(score)
        return tier is not None, score, tier, reasons
