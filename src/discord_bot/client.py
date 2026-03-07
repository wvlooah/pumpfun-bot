"""Discord bot client."""
import logging
import os

import discord
from formatter.discord_formatter import DiscordFormatter

logger = logging.getLogger("discord_bot")

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))


class DiscordBot:
    def __init__(self, scanner):
        intents = discord.Intents.default()
        self.client    = discord.Client(intents=intents)
        self.formatter = DiscordFormatter()
        self.channel   = None
        self._scanner  = scanner

        @self.client.event
        async def on_ready():
            logger.info(f"Logged in as {self.client.user}")
            self.channel = self.client.get_channel(DISCORD_CHANNEL_ID)
            if self.channel:
                logger.info(f"Sending alerts to: #{self.channel.name}")
            else:
                logger.error(f"Channel {DISCORD_CHANNEL_ID} not found!")
            self._scanner.add_alert_callback(self._send_alert)
            logger.info("Scanner started in background.")

    async def _send_alert(self, token: dict):
        if not self.channel:
            return
        try:
            embed = self.formatter.build_embed(token)
            await self.channel.send(embed=embed)
            logger.info(f"Alert sent: {token.get('name')} (${token.get('symbol')})")
        except Exception as e:
            logger.error(f"Discord send error: {e}")

    async def start_bot(self):
        await self.client.start(DISCORD_TOKEN)
