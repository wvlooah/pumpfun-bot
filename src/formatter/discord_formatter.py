"""Discord embed formatter — uses all Mobula + RugCheck fields."""

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
    if v is None: return "N/A"
    sign = "+" if v > 0 and show_sign else ""
    return f"{sign}{v:.1f}%"

def _usd(v) -> str:
    if not v: return "N/A"
    if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
    if v >= 1_000:     return f"${v/1_000:.1f}K"
    return f"${v:.2f}"

def _age(s: float) -> str:
    s = int(s)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m {s%60}s"
    return f"{s//3600}h {(s%3600)//60}m"

def _short(w: str) -> str:
    return f"{w[:6]}...{w[-4:]}" if len(w) > 16 else w


class DiscordFormatter:
    def build_embed(self, token: dict) -> discord.Embed:
        name     = token.get("name", "Unknown")
        symbol   = token.get("symbol", "???")
        mint     = token.get("mint", "")
        category = token.get("filter_category", "new_pair")
        score    = token.get("runner_score", 0)
        tier     = token.get("runner_tier", "")
        reasons  = token.get("runner_reasons", [])

        color     = CATEGORY_COLORS.get(category, 0x7289DA)
        cat_label = CATEGORY_LABELS.get(category, "📊 TOKEN")

        title = f"{tier}  |  {name}  (${symbol})" if tier else f"{name}  (${symbol})"
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
            name=f"{cat_label}  │  Runner Score",
            value=f"`{bar}` {score}/100\n*{reason_str}*",
            inline=False,
        )

        # ── Market ────────────────────────────────────────────────────────────
        embed.add_field(
            name="📊 Market",
            value=(
                f"**MC:** {_usd(token.get('market_cap_usd'))}\n"
                f"**Liquidity:** {_usd(token.get('liquidity_usd'))}\n"
                f"**Age:** {_age(token.get('age_seconds', 0))}\n"
                f"**Migrated:** {'✅' if token.get('is_migrated') else '❌'}"
            ),
            inline=True,
        )

        # ── Price Change ──────────────────────────────────────────────────────
        embed.add_field(
            name="📈 Price Change",
            value=(
                f"**5m:** {_pct(token.get('price_change_5m'))}\n"
                f"**1h:** {_pct(token.get('price_change_1h'))}\n"
                f"**4h:** {_pct(token.get('price_change_4h'))}\n"
                f"**24h:** {_pct(token.get('price_change_24h'))}"
            ),
            inline=True,
        )

        # ── Volume ────────────────────────────────────────────────────────────
        organic = token.get("organic_volume_1h", 0) or 0
        vol_1h  = token.get("volume_1h_usd", 0) or 0
        org_pct = f" ({organic/vol_1h*100:.0f}% organic)" if vol_1h > 0 and organic > 0 else ""
        embed.add_field(
            name="💹 Volume",
            value=(
                f"**5m:** {_usd(token.get('volume_5m_usd'))}\n"
                f"**1h:** {_usd(vol_1h)}{org_pct}\n"
                f"**24h:** {_usd(token.get('volume_24h_usd'))}\n"
                f"**Trades 1h:** {token.get('trades_1h', 0)}"
            ),
            inline=True,
        )

        # ── Holder Health (Mobula data) ───────────────────────────────────────
        embed.add_field(
            name="🛡️ Holder Health",
            value=(
                f"**Holders:** {token.get('holders_count', 0):,}\n"
                f"**Top 10:** {_pct(token.get('top10_holders_pct'), False)}\n"
                f"**Insiders:** {_pct(token.get('insider_pct'), False)}\n"
                f"**Snipers:** {_pct(token.get('snipers_pct'), False)}\n"
                f"**Bundlers:** {_pct(token.get('bundles_pct'), False)}\n"
                f"**Dev:** {_pct(token.get('dev_holding_pct'), False)}"
            ),
            inline=True,
        )

        # ── Smart Money ───────────────────────────────────────────────────────
        embed.add_field(
            name="🧠 Smart Money",
            value=(
                f"**Pro Traders:** {token.get('pro_holders_count', 0)}\n"
                f"**Pro Buys:** {token.get('pro_traders_buys', 0)}\n"
                f"**Smart Buys:** {token.get('smart_traders_buys', 0)}\n"
                f"**Fresh Traders:** {token.get('fresh_traders', 0)}\n"
                f"**Insiders #:** {token.get('insiders_count', 0)}"
            ),
            inline=True,
        )

        # ── Security ──────────────────────────────────────────────────────────
        rug_status = token.get("rug_status", "Unknown")
        risks_str  = ", ".join((token.get("rug_risks") or ["None"])[:3])
        embed.add_field(
            name="🔍 Security",
            value=(
                f"**RugCheck:** {RUG_EMOJI.get(rug_status, '❓')} {rug_status} ({token.get('rug_score', 0)})\n"
                f"**Mintable:** {'❌ Yes' if token.get('rug_mintable') else '✅ No'}\n"
                f"**Freezable:** {'❌ Yes' if token.get('rug_freezable') else '✅ No'}\n"
                f"**LP Locked:** {'✅ Yes' if token.get('lp_locked') else '❌ No'}\n"
                f"**Risks:** {risks_str}"
            ),
            inline=True,
        )

        # ── Dev ───────────────────────────────────────────────────────────────
        dev = token.get("dev_wallet", "Unknown")
        embed.add_field(
            name="👤 Dev",
            value=f"`{_short(dev)}`",
            inline=False,
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
        embed.set_footer(
            text=f"Pump.fun Runner Bot  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        return embed
