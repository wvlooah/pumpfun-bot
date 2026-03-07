import asyncio
import logging
import os
import sys
from datetime import datetime

import discord
from discord.ext import tasks
from dotenv import load_dotenv

from scanner.scanner import TokenScanner
from discord_bot.client import BotClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


async def main():
    token = os.getenv("DISCORD_TOKEN")
    channel_id = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

    if not token or not channel_id:
        logger.error("Missing DISCORD_TOKEN or DISCORD_CHANNEL_ID in environment.")
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True

    client = BotClient(channel_id=channel_id, intents=intents)

    logger.info("Starting Pump.fun Runner Bot...")
    async with client:
        await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
