"""Entry point."""
import asyncio
import logging
import os

import discord
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


async def main():
    from discord_bot.client import DiscordBot
    from scanner.scanner import TokenScanner

    scanner = TokenScanner()
    bot     = DiscordBot(scanner)

    logger.info("Starting Pump.fun Runner Bot (Mobula + RugCheck)...")
    await asyncio.gather(
        bot.start_bot(),
        scanner.start(),
    )


if __name__ == "__main__":
    asyncio.run(main())
