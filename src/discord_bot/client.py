import asyncio
import logging

import discord

from scanner.scanner import TokenScanner
from formatter.discord_formatter import DiscordFormatter

logger = logging.getLogger("discord_bot")


class BotClient(discord.Client):
    def __init__(self, channel_id: int, **kwargs):
        super().__init__(**kwargs)
        self.channel_id = channel_id
        self.scanner = TokenScanner()
        self.formatter = DiscordFormatter()
        self.alert_channel: discord.TextChannel | None = None

        # Register our send function as the alert callback
        self.scanner.add_alert_callback(self._send_alert)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        self.alert_channel = self.get_channel(self.channel_id)
        if not self.alert_channel:
            logger.error(f"Cannot find channel ID {self.channel_id}")
            return
        logger.info(f"Sending alerts to: #{self.alert_channel.name}")

        # Start the scanner as a background task
        self.loop.create_task(self.scanner.start())
        logger.info("Scanner started in background.")

    async def _send_alert(self, token: dict):
        """Called by the scanner whenever a qualifying token is found."""
        if not self.alert_channel:
            return
        try:
            embed = self.formatter.build_embed(token)
            await self.alert_channel.send(embed=embed)
            logger.info(f"Alert sent: {token.get('name')} (${token.get('symbol')})")
        except discord.HTTPException as e:
            logger.error(f"Discord send error: {e}")
        except Exception as e:
            logger.error(f"Alert send error: {e}", exc_info=True)
