import logging
from datetime import datetime, timezone

import discord

logger = logging.getLogger("formatter")

PUMPFUN_URL = "https://pump.fun"
DEXSCREENER_URL = "https://dexscreener.com/solana"
RUGCHECK_URL = "https://rugcheck.xyz/tokens"
BUBBLEMAPS_URL = "https://app.bubblemaps.io/sol/token"

CATEGORY_LABELS = {
    "new_pair": "🆕 NEW PAIR",
    "soon_migrate": "⚡ SOON TO MIGRATE",
    "migrated": "🚀 MIGRATED",
}

CATEGORY_COLORS = {
    "new_pair": 0x00FF99,
    "soon_migrate": 0xFFAA00,
    "migrated": 0xFF4400,
}

RUG_EMOJI = {
    "Good": "✅",
    "Warn": "⚠️",
    "Danger": "🚨",
    "Unknown": "❓",
}


def _fmt_pct(val: float | None, show_sign: bool = True) -> str:
    if val is None:
        return "N/A"
    sign = "+" if val > 0 and show_sign else ""
    return f"{sign}{val:.1f}%"


def _fmt_usd(val: float | None) -> str:
    if val is None or val == 0:
        return "N/A"
    if val >= 1_000_000:
        return f"${val/1_000_000:.2f}M"
    if val >= 1_000:
        return f"${val/1_000:.1f}K"
    return f"${val:.2f}"


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m"
    return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"


def _shorten_wallet(wallet: str) -> str:
    if len(wallet) <= 16:
        return wallet
    return f"{wallet[:6]}...{wallet[-4:]}"


class DiscordFormatter:
    def build_embed(self, token: dict) -> discord.Embed:
        name = token.get("name", "Unknown")
        symbol = token.get("symbol", "???")
        mint = token.get("mint", "")
        category = token.get("filter_category", "new_pair")
        runner_score = token.get("runner_score", 0)
        runner_reasons = token.get("runner_reasons", [])

        color = CATEGORY_COLORS.get(category, 0x7289DA)
        cat_label = CATEGORY_LABELS.get(category, "📊 TOKEN")

        # ── Title ─────────────────────────────────────────────────────────────
        title = f"{name}  (${symbol})"
        embed = discord.Embed(
            title=title,
            description=f"`{mint}`",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        if token.get("image_uri"):
            embed.set_thumbnail(url=token["image_uri"])

        # ── Category + Score ──────────────────────────────────────────────────
        score_bar = "█" * (runner_score // 10) + "░" * (10 - runner_score // 10)
        score_line = f"`{score_bar}` {runner_score}/100"
        reason_str = " · ".join(runner_reasons[:4]) if runner_reasons else "—"

        embed.add_field(
            name=f"{cat_label}  │  Runner Score",
            value=f"{score_line}\n*{reason_str}*",
            inline=False,
        )

        # ── Market Data ───────────────────────────────────────────────────────
        age_str = _fmt_age(token.get("age_seconds", 0))
        dex_listed = "✅" if token.get("is_dex_listed") else "❌"
        migrated_str = "✅" if token.get("is_migrated") else "❌"

        market_val = (
            f"**MC:** {_fmt_usd(token.get('market_cap_usd'))}\n"
            f"**Age:** {age_str}\n"
            f"**DEX Listed:** {dex_listed}\n"
            f"**Migrated:** {migrated_str}"
        )
        embed.add_field(name="📊 Market", value=market_val, inline=True)

        pc5 = _fmt_pct(token.get("price_change_5m"))
        pc1h = _fmt_pct(token.get("price_change_1h"))
        pc24h = _fmt_pct(token.get("price_change_24h"))
        price_val = (
            f"**5m:** {pc5}\n"
            f"**1h:** {pc1h}\n"
            f"**24h:** {pc24h}"
        )
        embed.add_field(name="📈 Price Change", value=price_val, inline=True)

        v5 = _fmt_usd(token.get("volume_5m_usd"))
        v1h = _fmt_usd(token.get("volume_1h_usd"))
        v24h = _fmt_usd(token.get("volume_24h_usd"))
        buys = token.get("tx_buys_5m", 0)
        sells = token.get("tx_sells_5m", 0)
        vol_val = (
            f"**5m:** {v5}\n"
            f"**1h:** {v1h}\n"
            f"**24h:** {v24h}\n"
            f"**Buys/Sells:** {buys}/{sells}"
        )
        embed.add_field(name="💹 Volume", value=vol_val, inline=True)

        # ── Holder Health ─────────────────────────────────────────────────────
        top10 = token.get("top10_holders_pct")
        insiders = token.get("insider_pct")
        snipers = token.get("snipers_pct")
        bundles = token.get("bundles_pct")
        pro_h = token.get("pro_holders_count", 0)

        holder_val = (
            f"**Top 10:** {_fmt_pct(top10, False)}\n"
            f"**Insiders:** {_fmt_pct(insiders, False)}\n"
            f"**Snipers:** {_fmt_pct(snipers, False)}\n"
            f"**Bundlers:** {_fmt_pct(bundles, False)}\n"
            f"**Pro Holders:** {pro_h}"
        )
        embed.add_field(name="🛡️ Holder Health", value=holder_val, inline=True)

        # ── Security / RugCheck ───────────────────────────────────────────────
        rug_status = token.get("rug_status", "Unknown")
        rug_emoji = RUG_EMOJI.get(rug_status, "❓")
        rug_score = token.get("rug_score", 0)
        mintable = "✅ False" if not token.get("rug_mintable") else "❌ True"
        freezable = "✅ False" if not token.get("rug_freezable") else "❌ True"
        risks = token.get("rug_risks", ["None"])
        risks_str = ", ".join(risks[:3]) if risks else "None"
        dev_pct = _fmt_pct(token.get("dev_holding_pct"), False)

        security_val = (
            f"**RugCheck:** {rug_emoji} {rug_status} ({rug_score})\n"
            f"**Dev Holding:** {dev_pct}\n"
            f"**Mintable:** {mintable}\n"
            f"**Freezable:** {freezable}\n"
            f"**Risks:** {risks_str}"
        )
        embed.add_field(name="🔍 Security", value=security_val, inline=True)

        # ── Developer ─────────────────────────────────────────────────────────
        dev_wallet = token.get("dev_wallet", "Unknown")
        deploys = token.get("dev_deploy_count", 0)
        migrations = token.get("dev_migration_count", 0)
        ratio = token.get("dev_success_ratio", 0.0)

        dev_val = (
            f"`{dev_wallet}`\n"
            f"**Deploys:** {deploys}\n"
            f"**Migrations:** {migrations}\n"
            f"**Ratio:** {ratio:.0f}%"
        )
        embed.add_field(name="👤 Dev", value=dev_val, inline=True)

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
                tw = f"https://twitter.com/{tw}"
            links.append(f"[Twitter]({tw})")
        if token.get("website"):
            links.append(f"[Website]({token['website']})")

        embed.add_field(name="🔗 Links", value=" · ".join(links), inline=False)

        embed.set_footer(text=f"Pump.fun Runner Bot  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        return embed
