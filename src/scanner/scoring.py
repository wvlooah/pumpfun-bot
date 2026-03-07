"""
Runner scoring — built around EARLY predictive signals, not lagging confirmation.

The goal is to catch a coin BEFORE it pumps, not after.

Signals are grouped into three categories:
  1. IGNITION  — the spark: something is just starting to move
  2. STRUCTURE — the setup: token is actually safe to run
  3. FUEL      — the capacity: enough capital and interest to sustain a run

Each signal is weighted by how EARLY and PREDICTIVE it is, not how big it is.
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

        # ── Raw data ──────────────────────────────────────────────────────────
        mc           = token.get("market_cap_usd", 0) or 0
        liquidity    = token.get("liquidity_usd", 0) or 0
        vol_5m       = token.get("volume_5m_usd", 0) or 0
        vol_1h       = token.get("volume_1h_usd", 0) or 0
        vol_24h      = token.get("volume_24h_usd", 0) or 0
        organic_vol  = token.get("organic_volume_1h", 0) or 0
        organic_txns = token.get("organic_trades_1h", 0) or 0
        pc_5m        = token.get("price_change_5m", 0) or 0
        pc_1h        = token.get("price_change_1h", 0) or 0
        pc_4h        = token.get("price_change_4h", 0) or 0
        buys         = token.get("tx_buys_5m", 0) or 0
        sells        = token.get("tx_sells_5m", 0) or 0
        trades_1h    = token.get("trades_1h", 0) or 0
        top10        = token.get("top10_holders_pct", 0) or 0
        dev_pct      = token.get("dev_holding_pct", 0) or 0
        insider_pct  = token.get("insider_pct", 0) or 0
        snipers      = token.get("snipers_pct", 0) or 0
        bundles      = token.get("bundles_pct", 0) or 0
        holders      = token.get("holders_count", 0) or 0
        pro_holders  = token.get("pro_holders_count", 0) or 0
        smart_buys   = token.get("smart_traders_buys", 0) or 0
        pro_buys     = token.get("pro_traders_buys", 0) or 0
        fresh_traders= token.get("fresh_traders", 0) or 0
        sol_fees     = token.get("total_sol_fees", 0) or 0
        lp_locked    = token.get("lp_locked", False)
        migrated     = token.get("is_migrated", False)
        is_soon      = token.get("is_soon", False)
        category     = token.get("filter_category", "new_pair")
        rug          = token.get("rug_status", "Unknown")
        twitter      = bool(token.get("twitter"))
        website      = bool(token.get("website"))
        age_min      = token.get("receipt_age_minutes", 0) or 0

        # ── Derived signals ───────────────────────────────────────────────────
        total_tx     = buys + sells
        buy_ratio    = buys / total_tx if total_tx > 0 else 0

        # Volume acceleration: 5m vs 1h hourly rate
        # If 5m vol is outpacing 1h average, volume is ACCELERATING right now
        vol_1h_rate  = vol_1h / 60 if vol_1h > 0 else 0   # per-minute average
        vol_accel    = (vol_5m / 5) / vol_1h_rate if vol_1h_rate > 0 else 0

        # Organic ratio: real vs total volume (bot filter)
        organic_ratio = organic_vol / vol_1h if vol_1h > 0 else 0

        # Liquidity/MC ratio: how much of the cap is actually liquid
        liq_mc_ratio  = liquidity / mc if mc > 0 else 0

        # Holder velocity: holders relative to age (fast accumulation = early signal)
        holder_velocity = holders / max(age_min, 1)

        # ═══════════════════════════════════════════════════════════════════════
        # IGNITION SIGNALS (max ~45 pts) — the spark that precedes a run
        # ═══════════════════════════════════════════════════════════════════════

        # ── Volume acceleration (STRONGEST early signal) ──────────────────────
        # 5m pace is outrunning 1h average = volume is spiking RIGHT NOW
        if vol_accel >= 5.0:
            pts += 18; reasons.append(f"Vol accel {vol_accel:.1f}x 🚀")
        elif vol_accel >= 3.0:
            pts += 12; reasons.append(f"Vol accel {vol_accel:.1f}x")
        elif vol_accel >= 2.0:
            pts += 7;  reasons.append(f"Vol accel {vol_accel:.1f}x")
        elif vol_accel >= 1.5:
            pts += 3

        # ── Smart / pro money entering (very early signal) ────────────────────
        # Smart wallets buy BEFORE the crowd — this is the most predictive signal
        if smart_buys >= 3:
            pts += 18; reasons.append(f"{smart_buys} smart wallet buys 🧠")
        elif smart_buys >= 1:
            pts += 10; reasons.append(f"{smart_buys} smart wallet buy")

        if pro_buys >= 5:
            pts += 10; reasons.append(f"{pro_buys} pro buys")
        elif pro_buys >= 2:
            pts += 5

        # ── Buy pressure (sustained demand signal) ────────────────────────────
        if buy_ratio >= 0.85 and total_tx >= 10:
            pts += 12; reasons.append(f"Buy pressure {buy_ratio*100:.0f}% ({total_tx} txns)")
        elif buy_ratio >= 0.75 and total_tx >= 5:
            pts += 8;  reasons.append(f"Buy pressure {buy_ratio*100:.0f}%")
        elif buy_ratio >= 0.65 and total_tx >= 5:
            pts += 4

        # ── 5m price spike (momentum just starting) ───────────────────────────
        # Small, fresh spike in 5m is more predictive than a big 1h move
        if pc_5m >= 30 and pc_1h < 80:   # Big 5m move but 1h not already pumped
            pts += 10; reasons.append(f"+{pc_5m:.0f}% 5m (early)")
        elif pc_5m >= 15:
            pts += 6;  reasons.append(f"+{pc_5m:.0f}% 5m")
        elif pc_5m >= 5:
            pts += 2

        # ── Organic volume spike (real humans, not bots) ──────────────────────
        if organic_ratio >= 0.85 and vol_1h >= 2000:
            pts += 10; reasons.append(f"Organic vol {organic_ratio*100:.0f}%")
        elif organic_ratio >= 0.65 and vol_1h >= 1000:
            pts += 5

        # ── Fresh trader influx (new buyers entering = early adoption) ────────
        if fresh_traders >= 20:
            pts += 8; reasons.append(f"{fresh_traders} fresh traders")
        elif fresh_traders >= 8:
            pts += 4
        elif fresh_traders >= 3:
            pts += 2

        # ── Rapid holder accumulation (relative to token age) ────────────────
        if holder_velocity >= 10:   # 10+ new holders per minute
            pts += 8; reasons.append(f"Holder velocity {holder_velocity:.0f}/min")
        elif holder_velocity >= 4:
            pts += 4
        elif holder_velocity >= 1:
            pts += 1

        # ═══════════════════════════════════════════════════════════════════════
        # STRUCTURE SIGNALS (max ~30 pts) — token is safe to run
        # ═══════════════════════════════════════════════════════════════════════

        # ── Low concentration = room to run without easy dump ─────────────────
        if top10 > 0:
            if top10 <= 15:
                pts += 10; reasons.append(f"Top10 only {top10:.0f}% 💎")
            elif top10 <= 25:
                pts += 6
            elif top10 <= 35:
                pts += 2
            elif top10 >= 70:
                pts -= 10  # concentrated = dump risk

        # ── Low dev holding (dev can't dump easily) ───────────────────────────
        if dev_pct > 0:
            if dev_pct <= 3:
                pts += 8; reasons.append(f"Dev only {dev_pct:.1f}% ✓")
            elif dev_pct <= 8:
                pts += 4
            elif dev_pct > 15:
                pts -= 8; reasons.append(f"Dev holds {dev_pct:.0f}% ⚠️")

        # ── Low snipers / insiders (clean launch) ─────────────────────────────
        if snipers > 0 and snipers <= 5:
            pts += 5
        elif snipers > 15:
            pts -= 5

        if insider_pct > 0 and insider_pct <= 10:
            pts += 3
        elif insider_pct > 25:
            pts -= 5

        if bundles > 0 and bundles > 20:
            pts -= 4; reasons.append(f"Bundles {bundles:.0f}% ⚠️")

        # ── RugCheck (safety baseline) ────────────────────────────────────────
        if rug == "Good":
            pts += 8; reasons.append("RugCheck ✓")
        elif rug == "Warn":
            pts += 1
        elif rug == "Danger":
            pts -= 25  # Hard penalty

        if lp_locked:
            pts += 5; reasons.append("LP Locked ✓")

        # ── Socials (legitimacy signal — projects that run have socials) ───────
        if twitter and website:
            pts += 6; reasons.append("Socials ✓")
        elif twitter or website:
            pts += 2

        # ═══════════════════════════════════════════════════════════════════════
        # FUEL SIGNALS (max ~25 pts) — enough capacity to sustain a run
        # ═══════════════════════════════════════════════════════════════════════

        # ── Liquidity/MC ratio (how deep the book is relative to cap) ─────────
        # Higher ratio = harder to manipulate, more sustainable moves
        if liq_mc_ratio >= 0.15:
            pts += 10; reasons.append(f"Liq/MC {liq_mc_ratio*100:.0f}%")
        elif liq_mc_ratio >= 0.08:
            pts += 6
        elif liq_mc_ratio >= 0.03:
            pts += 2

        # ── Pro holder count (smart money conviction) ─────────────────────────
        if pro_holders >= 20:
            pts += 6; reasons.append(f"{pro_holders} pro holders")
        elif pro_holders >= 5:
            pts += 3

        # ── Migration stage bonus ─────────────────────────────────────────────
        # Migrated/soon coins survived the bonding curve = proven demand
        if migrated:
            pts += 8; reasons.append("Migrated ✓")
        elif is_soon or category == "soon_migrate":
            pts += 5; reasons.append("Soon migrate ⚡")

        # ── Bonding curve fill (how much SOL has been committed) ──────────────
        if sol_fees >= 60:
            pts += 6; reasons.append(f"{sol_fees:.0f} SOL bonded")
        elif sol_fees >= 20:
            pts += 3
        elif sol_fees >= 5:
            pts += 1

        # ── Market cap sweet spot: not already pumped, not too small ──────────
        # Best runners start between $5K-$50K MC — big enough to have legs
        if 5_000 <= mc <= 50_000:
            pts += 5; reasons.append(f"MC sweet spot ${mc/1000:.0f}K")
        elif mc <= 100_000:
            pts += 2
        # Penalise if already at huge MC (late)
        elif mc >= 500_000:
            pts -= 5; reasons.append(f"MC ${mc/1000:.0f}K (late?)")

        return max(0, min(pts, 100)), reasons

    def passes(self, token: dict) -> tuple[bool, int, str | None, list[str]]:
        score, reasons = self.score(token)
        tier = get_tier(score)
        return tier is not None, score, tier, reasons
