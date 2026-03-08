"""Discord embed — shows every data field with its source labeled."""

import logging
from datetime import datetime, timezone

import discord

logger = logging.getLogger("formatter")

PUMPFUN_URL    = "https://pump.fun"
DEXSCREENER_URL = "https://dexscreener.com/solana"
RUGCHECK_URL   = "https://rugcheck.xyz/tokens"
BUBBLEMAPS_URL = "https://app.bubblemaps.io/sol/token"

CATEGORY_LABELS = {
    "new_pair":     "🆕 NEW PAIR",
    "soon_migrate": "⚡ SOON TO MIGRATE",
    "migrated":     "🚀 MIGRATED",
}
CATEGORY_COLORS = {
    "new_pair":     0x00FF99,
    "soon_migrate": 0xFFAA00,
    "migrated":     0xFF4400,
}
RUG_EMOJI = {"Good": "✅", "Warn": "⚠️", "Danger": "🚨", "Unknown": "❓"}

def _pct(v, show_sign=True) -> str:
    if v is None or v == 0: return "N/A"
    sign = "+" if v > 0 and show_sign else ""
    return f"{sign}{v:.1f}%"

def _usd(v) -> str:
    if not v: return "N/A"
    if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
    if v >= 1_000:     return f"${v/1_000:.1f}K"
    return f"${v:.2f}"

def _age(s: float) -> str:
    s = int(s or 0)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m {s%60}s"
    return f"{s//3600}h {(s%3600)//60}m"

def _short(w: str) -> str:
    return f"{w[:6]}...{w[-4:]}" if len(w) > 16 else w

def _na(v) -> str:
    return str(v) if v else "N/A"


class DiscordFormatter:
    def build_embed(self, token: dict) -> discord.Embed:
        name     = token.get("name", "Unknown")
        symbol   = token.get("symbol", "???")
        mint     = token.get("mint", "")
        category = token.get("filter_category", "new_pair")
        score    = token.get("runner_score", 0)
        tier     = token.get("runner_tier", "")
        reasons  = token.get("runner_reasons", [])
        enriched = token.get("mobula_enriched", False)

        color     = CATEGORY_COLORS.get(category, 0x7289DA)
        cat_label = CATEGORY_LABELS.get(category, "📊 TOKEN")
        title     = f"{tier}  |  {name}  (${symbol})" if tier else f"{name}  (${symbol})"

        embed = discord.Embed(
            title=title,
            description=f"`{mint}`",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        if token.get("image_uri"):
            embed.set_thumbnail(url=token["image_uri"])

        # ── Score bar ─────────────────────────────────────────────────────────
        bar = "█" * (score // 10) + "░" * (10 - score // 10)
        reason_str = " · ".join(reasons[:5]) if reasons else "—"
        embed.add_field(
            name=f"{cat_label}  │  Runner Score {score}/100",
            value=f"`{bar}`\n*{reason_str}*",
            inline=False,
        )

        # ── Market (Mobula + PumpPortal) ──────────────────────────────────────
        vol_change = token.get("mobula_vol_change_24h", 0) or 0
        vol_accel  = token.get("mobula_vol_accel", False)
        vol_accel_str = f" ({vol_change:+.0f}% vs yday {'🚀' if vol_accel else ''})" if vol_change else ""
        embed.add_field(
            name="📊 Market  *(Mobula)*",
            value=(
                f"**MC:** {_usd(token.get('market_cap_usd'))}\n"
                f"**Liquidity:** {_usd(token.get('liquidity_usd'))}\n"
                f"**Vol 24h:** {_usd(token.get('volume_24h_usd'))}{vol_accel_str}\n"
                f"**Vol 5m:** {_usd(token.get('volume_5m_usd'))}\n"
                f"**Age:** {_age(token.get('age_seconds', 0))}\n"
                f"**Migrated:** {'✅' if token.get('is_migrated') else '❌'}"
            ),
            inline=True,
        )

        # ── Price change (Mobula) ─────────────────────────────────────────────
        organic = token.get("mobula_organic_ratio", None)
        organic_str = f"\n**Organic Vol:** {organic*100:.0f}%" if organic is not None and enriched else ""
        embed.add_field(
            name="📈 Price  *(Mobula)*",
            value=(
                f"**1h:** {_pct(token.get('price_change_1h'))}\n"
                f"**24h:** {_pct(token.get('price_change_24h'))}\n"
                f"**7d:** {_pct(token.get('mobula_price_change_7d'))}"
                + organic_str
            ),
            inline=True,
        )

        # ── Live trades (PumpPortal) ──────────────────────────────────────────
        buys  = token.get("tx_buys_5m", 0) or 0
        sells = token.get("tx_sells_5m", 0) or 0
        total = buys + sells
        ratio = f"{buys/total*100:.0f}% buys" if total > 0 else "N/A"
        embed.add_field(
            name="⚡ Live 60s  *(PumpPortal)*",
            value=(
                f"**Buys:** {buys}\n"
                f"**Sells:** {sells}\n"
                f"**Pressure:** {ratio}\n"
                f"**SOL Bonded:** {token.get('total_sol_fees', 0):.2f} SOL"
            ),
            inline=True,
        )

        # ── Holder Health (RugCheck) ──────────────────────────────────────────
        rug_status = token.get("rug_status", "Unknown")
        risks_list = (token.get("rug_risks") or ["None"])[:3]
        risks_str  = ", ".join(risks_list)
        embed.add_field(
            name="🛡️ Security  *(RugCheck)*",
            value=(
                f"**Status:** {RUG_EMOJI.get(rug_status,'❓')} {rug_status} ({token.get('rug_score',0)})\n"
                f"**Mintable:** {'❌ YES' if token.get('rug_mintable') else '✅ No'}\n"
                f"**Freezable:** {'❌ YES' if token.get('rug_freezable') else '✅ No'}\n"
                f"**LP Locked:** {'✅ Yes' if token.get('lp_locked') else '❌ No'}\n"
                f"**Risks:** {risks_str}"
            ),
            inline=True,
        )

        # ── Holder distribution (RugCheck) ────────────────────────────────────
        top10   = token.get("top10_holders_pct", 0) or 0
        dev     = token.get("dev_holding_pct", 0) or 0
        insider = token.get("insider_pct", 0) or 0
        embed.add_field(
            name="👥 Holders  *(RugCheck)*",
            value=(
                f"**Top 10:** {f'{top10:.1f}%' if top10 else 'N/A'}\n"
                f"**Dev:** {f'{dev:.1f}%' if dev else 'N/A'}\n"
                f"**Insiders:** {f'{insider:.1f}%' if insider else 'N/A'}"
            ),
            inline=True,
        )

        # ── Dev wallet ───────────────────────────────────────────────────────
        dev_wallet = token.get("dev_wallet", "")
        embed.add_field(
            name="👤 Dev",
            value=f"`{_short(dev_wallet)}`" if dev_wallet else "Unknown",
            inline=True,
        )

        # ── Links ─────────────────────────────────────────────────────────────
        links = [
            f"[Pump.fun]({PUMPFUN_URL}/{mint})",
            f"[DexScreener]({DEXSCREENER_URL}/{mint})",
            f"[RugCheck]({RUGCHECK_URL}/{mint})",
            f"[Bubblemaps]({BUBBLEMAPS_URL}/{mint})",
        ]
        if token.get("twitter"):
            tw = token["twitter"]
            if not tw.startswith("http"):
                tw = f"https://twitter.com/{tw.lstrip('@')}"
            links.append(f"[Twitter]({tw})")
        if token.get("website"):
            links.append(f"[Website]({token['website']})")

        embed.add_field(name="🔗 Links", value=" · ".join(links), inline=False)

        src = "✅ Mobula enriched" if enriched else "⚠️ Mobula not indexed yet"
        embed.set_footer(
            text=f"PumpPortal + Mobula + RugCheck  |  {src}  |  {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        return embed
