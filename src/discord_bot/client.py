import asyncio
import logging
import os
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from scanner.scanner import TokenScanner
from formatter.discord_formatter import DiscordFormatter

logger = logging.getLogger("discord_bot")

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "15"))


class BotClient(discord.Client):
    def __init__(self, channel_id: int, **kwargs):
        super().__init__(**kwargs)
        self.channel_id = channel_id
        self.scanner = TokenScanner()
        self.formatter = DiscordFormatter()
        self.alert_channel: discord.TextChannel | None = None

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        self.alert_channel = self.get_channel(self.channel_id)
        if not self.alert_channel:
            logger.error(f"Could not find channel ID {self.channel_id}")
            return
        logger.info(f"Sending alerts to: #{self.alert_channel.name}")
        self.scan_loop.start()

    @tasks.loop(seconds=SCAN_INTERVAL_SECONDS)
    async def scan_loop(self):
        try:
            logger.info("Running token scan cycle...")
            alerts = await self.scanner.scan()
            for token_data in alerts:
                try:
                    embed = self.formatter.build_embed(token_data)
                    if self.alert_channel:
                        await self.alert_channel.send(embed=embed)
                        logger.info(f"Alert sent: {token_data.get('name', 'Unknown')} ({token_data.get('symbol', '?')})")
                        await asyncio.sleep(1)  # Rate limit buffer
                except Exception as e:
                    logger.error(f"Error sending alert: {e}")
        except Exception as e:
            logger.error(f"Scan loop error: {e}", exc_info=True)

    @scan_loop.before_loop
    async def before_scan_loop(self):
        await self.wait_until_ready()
        logger.info("Scan loop starting...")
