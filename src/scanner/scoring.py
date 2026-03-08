"""
Runner scoring — predictive early signals + deep rug safety.

Data sources:
  PumpPortal  → tx_buys_5m, tx_sells_5m, volume_5m_usd, total_sol_fees,
                repeat_buy_signal, price_floor_held, bonding_fill_rate, is_migrated
  Mobula REST → market_cap_usd, liquidity_usd, volume_24h_usd,
                price_change_1h, price_change_24h,
                mobula_vol_change_24h, mobula_organic_ratio, mobula_vol_accel
  RugCheck    → top10_holders_pct, dev_holding_pct, insider_pct,
                mintable, freezable, lp_locked, lp_lock_days,
                metadata_mutable, transfer_fee_pct, verified, status, score

Time of day: US hours (14:00-02:00 UTC) get a boost, dead hours get a penalty
"""
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("scoring")

TIER_AVERAGE = int(os.getenv("SCORE_AVERAGE", "25"))
TIER_GOOD    = int(os.getenv("SCORE_GOOD",    "45"))
TIER_STRONG  = int(os.getenv("SCORE_STRONG",  "65"))


def get_tier(score: int) -> str | None:
    if score >= TIER_STRONG:  return "🔥 STRONG"
    if score >= TIER_GOOD:    return "✅ GOOD"
    if score >= TIER_AVERAGE: return "📡 AVERAGE"
    return None


def _time_of_day_modifier() -> tuple[int, str]:
    """
    Returns (pts_adjustment, label).
    Memecoin volume peaks: US afternoon/evening + Asian morning.
    Dead zone: 04:00-10:00 UTC (overnight US, early EU, late Asia).
    """
    utc_hour = datetime.now(timezone.utc).hour
    # Peak: 14:00-02:00 UTC (US market hours + evening)
    if 14 <= utc_hour <= 23 or 0 <= utc_hour <= 2:
        return 5, "Peak hours 🕐"
    # Asian peak: 08:00-12:00 UTC
    elif 8 <= utc_hour <= 12:
        return 2, "Asian hours"
    # Dead zone: 03:00-07:00 UTC
    elif 3 <= utc_hour <= 7:
        return -5, "Dead hours 😴"
    return 0, ""


class RunnerScorer:

    def score(self, token: dict) -> tuple[int, list[str]]:
        pts = 0
        reasons: list[str] = []

        # ── Pull all fields ────────────────────────────────────────────────────
        mc             = token.get("market_cap_usd", 0) or 0
        liquidity      = token.get("liquidity_usd", 0) or 0
        vol_5m         = token.get("volume_5m_usd", 0) or 0
        vol_24h        = token.get("volume_24h_usd", 0) or 0
        pc_1h          = token.get("price_change_1h", 0) or 0
        buys           = token.get("tx_buys_5m", 0) or 0
        sells          = token.get("tx_sells_5m", 0) or 0
        sol_fees       = token.get("total_sol_fees", 0) or 0
        bonding_rate   = token.get("bonding_fill_rate", 0) or 0
        repeat_buyers  = token.get("repeat_buy_signal", False)
        floor_held     = token.get("price_floor_held", False)
        migrated       = token.get("is_migrated", False)
        category       = token.get("filter_category", "soon_migrate")
        twitter        = bool(token.get("twitter"))
        website        = bool(token.get("website"))

        # Mobula
        vol_change     = token.get("mobula_vol_change_24h", 0) or 0
        organic_ratio  = token.get("mobula_organic_ratio", 1.0)
        vol_accel      = token.get("mobula_vol_accel", False)
        enriched       = token.get("mobula_enriched", False)

        # RugCheck deep fields
        top10          = token.get("top10_holders_pct", 0) or 0
        dev_pct        = token.get("dev_holding_pct", 0) or 0
        insider_pct    = token.get("insider_pct", 0) or 0
        rug            = token.get("rug_status", "Unknown")
        lp_locked      = token.get("lp_locked", False)
        lp_days        = token.get("lp_lock_days", 0) or 0
        mutable        = token.get("metadata_mutable", False)
        fee_pct        = token.get("transfer_fee_pct", 0.0) or 0.0
        verified       = token.get("verified", False)
        mintable       = token.get("rug_mintable", False)
        freezable      = token.get("rug_freezable", False)

        # Derived
        total_tx      = buys + sells
        buy_ratio     = buys / total_tx if total_tx > 0 else 0.0
        liq_mc_ratio  = liquidity / mc if mc > 0 else 0.0
        vol_mc_ratio  = vol_24h / mc if mc > 0 else 0.0   # daily turnover

        # ═══════════════════════════════════════════════════════════════════════
        # IGNITION — momentum signals happening right now
        # ═══════════════════════════════════════════════════════════════════════

        # ── Volume acceleration (Mobula) ──────────────────────────────────────
        if vol_accel and vol_change >= 200:
            pts += 18; reasons.append(f"Vol +{vol_change:.0f}% vs yday 🚀")
        elif vol_accel and vol_change >= 100:
            pts += 12; reasons.append(f"Vol +{vol_change:.0f}% vs yday")
        elif vol_accel:
            pts += 6;  reasons.append(f"Vol accel +{vol_change:.0f}%")
        elif vol_change > 20:
            pts += 2

        # ── Buy pressure (PumpPortal live) ────────────────────────────────────
        if buy_ratio >= 0.85 and total_tx >= 15:
            pts += 15; reasons.append(f"Buy pressure {buy_ratio*100:.0f}% ({total_tx} txns)")
        elif buy_ratio >= 0.80 and total_tx >= 8:
            pts += 10; reasons.append(f"Buy pressure {buy_ratio*100:.0f}%")
        elif buy_ratio >= 0.70 and total_tx >= 5:
            pts += 5
        elif buy_ratio >= 0.60 and total_tx >= 3:
            pts += 2

        # ── Repeat buyers (conviction signal) ────────────────────────────────
        if repeat_buyers:
            pts += 8; reasons.append("Repeat buyers detected 🔁")

        # ── Price floor holding (resilience) ─────────────────────────────────
        if floor_held:
            pts += 8; reasons.append("Price floor held 📊")

        # ── Bonding curve fill speed ──────────────────────────────────────────
        # SOL per second during bonding. Fast fill = explosive demand
        if bonding_rate >= 1.0:
            pts += 12; reasons.append(f"Fast bonding {bonding_rate:.1f} SOL/s 🔥")
        elif bonding_rate >= 0.3:
            pts += 7;  reasons.append(f"Bonding {bonding_rate:.2f} SOL/s")
        elif bonding_rate >= 0.1:
            pts += 3

        # ── Price momentum (Mobula 1h) ────────────────────────────────────────
        if 5 <= pc_1h < 50:
            pts += 8; reasons.append(f"+{pc_1h:.0f}% 1h (early move)")
        elif pc_1h >= 50:
            pts += 4; reasons.append(f"+{pc_1h:.0f}% 1h")
        elif 2 <= pc_1h < 5:
            pts += 2

        # ── Volume/MC daily turnover (Mobula) ─────────────────────────────────
        # High turnover at low MC = lots of activity relative to size
        if vol_mc_ratio >= 0.5:
            pts += 8; reasons.append(f"Turnover {vol_mc_ratio*100:.0f}%/day")
        elif vol_mc_ratio >= 0.2:
            pts += 4
        elif vol_mc_ratio >= 0.1:
            pts += 1

        # ── Organic volume (Mobula) ───────────────────────────────────────────
        if enriched:
            if organic_ratio >= 0.90:
                pts += 8; reasons.append(f"Organic vol {organic_ratio*100:.0f}%")
            elif organic_ratio >= 0.70:
                pts += 4
            elif organic_ratio < 0.20:
                pts -= 6  # mostly bots

        # ── 5m volume vs MC ───────────────────────────────────────────────────
        if mc > 0 and vol_5m > 0:
            turnover_5m = vol_5m / mc
            if turnover_5m >= 0.08:
                pts += 6; reasons.append(f"5m vol {turnover_5m*100:.0f}% of MC")
            elif turnover_5m >= 0.03:
                pts += 3

        # ── Time of day ───────────────────────────────────────────────────────
        tod_pts, tod_label = _time_of_day_modifier()
        if tod_pts != 0:
            pts += tod_pts
            if tod_label: reasons.append(tod_label)

        # ═══════════════════════════════════════════════════════════════════════
        # STRUCTURE — is this token safe to actually run?
        # ═══════════════════════════════════════════════════════════════════════

        # ── LP lock + duration ────────────────────────────────────────────────
        if lp_locked and lp_days >= 30:
            pts += 12; reasons.append(f"LP locked {lp_days}d ✓")
        elif lp_locked and lp_days >= 7:
            pts += 7;  reasons.append(f"LP locked {lp_days}d")
        elif lp_locked and lp_days > 0:
            pts += 3;  reasons.append(f"LP locked {lp_days}d (short)")
        elif lp_locked:
            pts += 4;  reasons.append("LP locked ✓")
        else:
            pts -= 5;  reasons.append("No LP lock ⚠️")

        # ── Metadata mutability ───────────────────────────────────────────────
        if mutable:
            pts -= 8; reasons.append("Mutable metadata ❌")
        else:
            pts += 3

        # ── Transfer fee ──────────────────────────────────────────────────────
        if fee_pct > 5:
            pts -= 10; reasons.append(f"Transfer fee {fee_pct:.1f}% ❌")
        elif fee_pct > 0:
            pts -= 3;  reasons.append(f"Transfer fee {fee_pct:.1f}%")

        # ── Verification badge ────────────────────────────────────────────────
        if verified:
            pts += 10; reasons.append("Verified ✅")

        # ── Mintable / Freezable ──────────────────────────────────────────────
        if mintable:
            pts -= 12; reasons.append("Mintable ❌")
        if freezable:
            pts -= 8;  reasons.append("Freezable ❌")

        # ── RugCheck status ───────────────────────────────────────────────────
        if rug == "Good":
            pts += 8; reasons.append("RugCheck ✓")
        elif rug == "Warn":
            pts += 1
        elif rug == "Danger":
            pts -= 25

        # ── Top holder concentration ──────────────────────────────────────────
        if top10 > 0:
            if top10 <= 15:
                pts += 10; reasons.append(f"Top10 {top10:.0f}% 💎")
            elif top10 <= 25:
                pts += 6
            elif top10 <= 40:
                pts += 2
            elif top10 > 70:
                pts -= 10; reasons.append(f"Top10 {top10:.0f}% ⚠️")

        # ── Dev holding ───────────────────────────────────────────────────────
        if dev_pct > 0:
            if dev_pct <= 3:
                pts += 8; reasons.append(f"Dev {dev_pct:.1f}% ✓")
            elif dev_pct <= 8:
                pts += 4
            elif dev_pct > 15:
                pts -= 8; reasons.append(f"Dev {dev_pct:.0f}% ⚠️")

        # ── Insider network ───────────────────────────────────────────────────
        if insider_pct > 0:
            if insider_pct <= 5:
                pts += 5
            elif insider_pct <= 15:
                pts += 2
            elif insider_pct > 30:
                pts -= 8; reasons.append(f"Insiders {insider_pct:.0f}% ⚠️")

        # ── Socials ───────────────────────────────────────────────────────────
        if twitter and website:
            pts += 6; reasons.append("Socials ✓")
        elif twitter or website:
            pts += 3

        # ═══════════════════════════════════════════════════════════════════════
        # FUEL — capacity to sustain a move
        # ═══════════════════════════════════════════════════════════════════════

        # ── Liquidity/MC ratio ────────────────────────────────────────────────
        if liq_mc_ratio >= 0.20:
            pts += 8; reasons.append(f"Liq/MC {liq_mc_ratio*100:.0f}%")
        elif liq_mc_ratio >= 0.10:
            pts += 4
        elif liq_mc_ratio >= 0.05:
            pts += 1
        elif 0 < liq_mc_ratio < 0.02:
            pts -= 3

        # ── MC sweet spot ─────────────────────────────────────────────────────
        if 5_000 <= mc <= 30_000:
            pts += 6; reasons.append(f"Sweet spot ${mc/1000:.0f}K")
        elif 30_000 < mc <= 100_000:
            pts += 3
        elif mc > 500_000:
            pts -= 5

        # ── SOL bonded ────────────────────────────────────────────────────────
        if sol_fees >= 60:
            pts += 6; reasons.append(f"{sol_fees:.0f} SOL bonded")
        elif sol_fees >= 20:
            pts += 3
        elif sol_fees >= 5:
            pts += 1

        # ── Migration ─────────────────────────────────────────────────────────
        if migrated:
            pts += 6; reasons.append("Migrated ✓")
        elif category == "soon_migrate":
            pts += 3

        return max(0, min(pts, 100)), reasons

    def passes(self, token: dict) -> tuple[bool, int, str | None, list[str]]:
        score, reasons = self.score(token)
        tier = get_tier(score)
        return tier is not None, score, tier, reasons
