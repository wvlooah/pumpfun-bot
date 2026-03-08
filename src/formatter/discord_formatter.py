"""Discord embed — full data display with narrative."""

import logging
from datetime import datetime, timezone

import discord

logger = logging.getLogger("formatter")

PUMPFUN_URL    = "https://pump.fun"
DEXSCREENER_URL = "https://dexscreener.com/solana"
RUGCHECK_URL   = "https://rugcheck.xyz/tokens"
BUBBLEMAPS_URL = "https://app.bubblemaps.io/sol/token"

CATEGORY_COLORS = {"soon_migrate": 0xFFAA00, "migrated": 0xFF4400}
CATEGORY_LABELS = {"soon_migrate": "⚡ SOON TO MIGRATE", "migrated": "🚀 MIGRATED"}
RUG_EMOJI = {"Good": "✅", "Warn": "⚠️", "Danger": "🚨", "Unknown": "❓"}

def _pct(v, sign=True) -> str:
    if v is None or v == 0: return "N/A"
    s = "+" if v > 0 and sign else ""
    return f"{s}{v:.1f}%"

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
    return f"{w[:6]}...{w[-4:]}" if len(w) > 16 else (w or "Unknown")

def _bool(v, true_str="✅ Yes", false_str="❌ No") -> str:
    return true_str if v else false_str


class DiscordFormatter:
    def build_embed(self, token: dict) -> discord.Embed:
        name     = token.get("name", "Unknown")
        symbol   = token.get("symbol", "???")
        mint     = token.get("mint", "")
        category = token.get("filter_category", "soon_migrate")
        score    = token.get("runner_score", 0)
        tier     = token.get("runner_tier", "")
        reasons  = token.get("runner_reasons", [])
        enriched = token.get("mobula_enriched", False)
        narrative= token.get("narrative", "")

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

        # ── Narrative ─────────────────────────────────────────────────────────
        if narrative:
            embed.add_field(name="💬 Analysis", value=narrative, inline=False)

        # ── Market ────────────────────────────────────────────────────────────
        vol_change = token.get("mobula_vol_change_24h", 0) or 0
        vol_str    = f" ({vol_change:+.0f}% vs yday)" if vol_change else ""
        embed.add_field(
            name="📊 Market",
            value=(
                f"**MC:** {_usd(token.get('market_cap_usd'))}\n"
                f"**Liquidity:** {_usd(token.get('liquidity_usd'))}\n"
                f"**Vol 24h:** {_usd(token.get('volume_24h_usd'))}{vol_str}\n"
                f"**Vol 5m:** {_usd(token.get('volume_5m_usd'))}\n"
                f"**Age:** {_age(token.get('age_seconds', 0))}\n"
                f"**Migrated:** {_bool(token.get('is_migrated'))}"
            ),
            inline=True,
        )

        # ── Price ──────────────────────────────────────────────────────────────
        organic = token.get("mobula_organic_ratio")
        org_str = f"\n**Organic Vol:** {organic*100:.0f}%" if organic and enriched else ""
        embed.add_field(
            name="📈 Price",
            value=(
                f"**1h:** {_pct(token.get('price_change_1h'))}\n"
                f"**24h:** {_pct(token.get('price_change_24h'))}\n"
                f"**Floor Held:** {_bool(token.get('price_floor_held'))}"
                + org_str
            ),
            inline=True,
        )

        # ── Live Trades ───────────────────────────────────────────────────────
        buys  = token.get("tx_buys_5m", 0) or 0
        sells = token.get("tx_sells_5m", 0) or 0
        total = buys + sells
        ratio = f"{buys/total*100:.0f}%" if total > 0 else "N/A"
        bonding_rate = token.get("bonding_fill_rate", 0) or 0
        embed.add_field(
            name="⚡ Live Activity",
            value=(
                f"**Buys:** {buys}  **Sells:** {sells}\n"
                f"**Buy Ratio:** {ratio}\n"
                f"**Repeat Buyers:** {_bool(token.get('repeat_buy_signal'))}\n"
                f"**Bonding Rate:** {bonding_rate:.2f} SOL/s\n"
                f"**SOL Bonded:** {token.get('total_sol_fees', 0):.2f}"
            ),
            inline=True,
        )

        # ── Holder health ──────────────────────────────────────────────────────
        top10   = token.get("top10_holders_pct", 0) or 0
        dev     = token.get("dev_holding_pct", 0) or 0
        insider = token.get("insider_pct", 0) or 0
        embed.add_field(
            name="👥 Holders",
            value=(
                f"**Top 10:** {f'{top10:.1f}%' if top10 else 'N/A'}\n"
                f"**Dev:** {f'{dev:.1f}%' if dev else 'N/A'}\n"
                f"**Insiders:** {f'{insider:.1f}%' if insider else 'N/A'}"
            ),
            inline=True,
        )

        # ── Security ──────────────────────────────────────────────────────────
        rug_status = token.get("rug_status", "Unknown")
        lp_days    = token.get("lp_lock_days", 0) or 0
        lp_str     = f"✅ {lp_days}d" if token.get("lp_locked") and lp_days else (
                     "✅ Locked" if token.get("lp_locked") else "❌ No")
        fee_pct    = token.get("transfer_fee_pct", 0) or 0
        risks_str  = ", ".join((token.get("rug_risks") or ["None"])[:3])
        embed.add_field(
            name="🔍 Security",
            value=(
                f"**RugCheck:** {RUG_EMOJI.get(rug_status,'❓')} {rug_status}\n"
                f"**Mintable:** {'❌ YES' if token.get('rug_mintable') else '✅ No'}\n"
                f"**Freezable:** {'❌ YES' if token.get('rug_freezable') else '✅ No'}\n"
                f"**Mutable Meta:** {'❌ YES' if token.get('metadata_mutable') else '✅ No'}\n"
                f"**Transfer Fee:** {'❌ ' if fee_pct > 0 else ''}{fee_pct:.1f}%\n"
                f"**LP Lock:** {lp_str}\n"
                f"**Verified:** {'✅ Yes' if token.get('verified') else '❌ No'}\n"
                f"**Risks:** {risks_str}"
            ),
            inline=True,
        )

        # ── Dev ───────────────────────────────────────────────────────────────
        socials = []
        if token.get("twitter"):
            tw = token["twitter"]
            if not tw.startswith("http"): tw = f"https://twitter.com/{tw.lstrip('@')}"
            socials.append(f"[Twitter]({tw})")
        if token.get("website"):
            socials.append(f"[Website]({token['website']})")
        embed.add_field(
            name="👤 Dev",
            value=(
                f"`{_short(token.get('dev_wallet',''))}`\n"
                + (" · ".join(socials) if socials else "No socials")
            ),
            inline=True,
        )

        # ── Links ─────────────────────────────────────────────────────────────
        links = [
            f"[Pump.fun]({PUMPFUN_URL}/{mint})",
            f"[DexScreener]({DEXSCREENER_URL}/{mint})",
            f"[RugCheck]({RUGCHECK_URL}/{mint})",
            f"[Bubblemaps]({BUBBLEMAPS_URL}/{mint})",
        ]
        embed.add_field(name="🔗 Links", value=" · ".join(links), inline=False)

        src = "✅ Mobula enriched" if enriched else "⚠️ Mobula not indexed yet"
        embed.set_footer(
            text=f"PumpPortal + Mobula + RugCheck  |  {src}  |  {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        return embed
