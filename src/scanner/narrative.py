"""
Rule-based narrative generator — free, deterministic, no API needed.
Produces a 2-3 sentence plain-English summary of the token's situation.
"""


def build_narrative(token: dict) -> str:
    parts = []

    mc          = token.get("market_cap_usd", 0) or 0
    liq         = token.get("liquidity_usd", 0) or 0
    pc_1h       = token.get("price_change_1h", 0) or 0
    vol_change  = token.get("mobula_vol_change_24h", 0) or 0
    organic     = token.get("mobula_organic_ratio", 1.0)
    buys        = token.get("tx_buys_5m", 0) or 0
    sells       = token.get("tx_sells_5m", 0) or 0
    top10       = token.get("top10_holders_pct", 0) or 0
    dev_pct     = token.get("dev_holding_pct", 0) or 0
    insider_pct = token.get("insider_pct", 0) or 0
    lp_locked   = token.get("lp_locked", False)
    lp_days     = token.get("lp_lock_days", 0) or 0
    mutable     = token.get("metadata_mutable", False)
    fee_pct     = token.get("transfer_fee_pct", 0.0) or 0.0
    verified    = token.get("verified", False)
    mintable    = token.get("rug_mintable", False)
    freezable   = token.get("rug_freezable", False)
    rug         = token.get("rug_status", "Unknown")
    migrated    = token.get("is_migrated", False)
    floor_held  = token.get("price_floor_held", False)
    repeat_buys = token.get("repeat_buy_signal", False)
    bonding_rate= token.get("bonding_fill_rate", 0.0) or 0.0
    score       = token.get("runner_score", 0)
    category    = token.get("filter_category", "soon_migrate")

    total_tx    = buys + sells
    buy_ratio   = buys / total_tx if total_tx > 0 else 0

    # ── Sentence 1: Momentum / setup ──────────────────────────────────────────
    momentum_parts = []

    if vol_change >= 100:
        momentum_parts.append(f"volume is up {vol_change:.0f}% vs yesterday")
    elif vol_change >= 30:
        momentum_parts.append(f"volume accelerating (+{vol_change:.0f}% vs yesterday)")

    if buy_ratio >= 0.80 and total_tx >= 8:
        momentum_parts.append(f"strong buy pressure at {buy_ratio*100:.0f}%")
    elif buy_ratio >= 0.65 and total_tx >= 5:
        momentum_parts.append(f"buy-dominant activity ({buy_ratio*100:.0f}% buys)")

    if pc_1h >= 15:
        momentum_parts.append(f"up {pc_1h:.0f}% in the last hour")
    elif pc_1h >= 5:
        momentum_parts.append(f"gaining {pc_1h:.0f}% in the last hour")

    if bonding_rate >= 0.3:
        momentum_parts.append(f"filled the bonding curve at {bonding_rate:.1f} SOL/s")

    if repeat_buys:
        momentum_parts.append("wallets are buying back in multiple times")

    if floor_held:
        momentum_parts.append("price dipped and recovered — floor is holding")

    if migrated:
        momentum_parts.append("successfully migrated to Raydium")

    if momentum_parts:
        parts.append("📈 " + ", ".join(momentum_parts[:3]).capitalize() + ".")
    else:
        parts.append("📈 Token is showing early movement with limited data available.")

    # ── Sentence 2: Safety assessment ────────────────────────────────────────
    safety_parts = []
    risks = []
    positives = []

    if verified:
        positives.append("project is verified")
    if lp_locked and lp_days >= 30:
        positives.append(f"LP locked {lp_days} days")
    elif lp_locked:
        positives.append("LP is locked")
    if dev_pct > 0 and dev_pct <= 3:
        positives.append(f"dev holds only {dev_pct:.1f}%")
    if top10 > 0 and top10 <= 20:
        positives.append(f"top 10 holders own just {top10:.0f}%")
    if rug == "Good":
        positives.append("RugCheck clean")
    if organic >= 0.85:
        positives.append(f"{organic*100:.0f}% organic volume")

    if mintable:
        risks.append("mint authority not revoked")
    if freezable:
        risks.append("freeze authority active")
    if mutable:
        risks.append("metadata is mutable")
    if fee_pct > 0:
        risks.append(f"{fee_pct:.1f}% transfer fee")
    if not lp_locked:
        risks.append("LP not locked")
    if dev_pct > 15:
        risks.append(f"dev holds {dev_pct:.0f}%")
    if insider_pct > 20:
        risks.append(f"insider network holds {insider_pct:.0f}%")
    if top10 > 60:
        risks.append(f"top 10 hold {top10:.0f}% of supply")

    if positives and not risks:
        parts.append("🛡️ Safety looks strong — " + ", ".join(positives[:3]) + ".")
    elif positives and risks:
        parts.append(f"🛡️ Positives: {', '.join(positives[:2])}. Risks: {', '.join(risks[:2])}.")
    elif risks:
        parts.append("⚠️ Caution — " + ", ".join(risks[:3]) + ".")
    else:
        parts.append("🛡️ Safety data pending — trade carefully until RugCheck confirms.")

    # ── Sentence 3: Verdict ───────────────────────────────────────────────────
    if score >= 65:
        verdict = "Strong runner setup — high conviction entry window."
    elif score >= 45:
        verdict = "Good setup with manageable risk — watch for continued momentum."
    else:
        verdict = "Early signal — monitor closely before sizing in."

    parts.append(f"🎯 {verdict}")

    return " ".join(parts)
