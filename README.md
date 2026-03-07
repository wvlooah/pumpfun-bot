# 🚀 Pump.fun Runner Bot

A production-ready Discord bot that scans Pump.fun tokens in real time, detects potential runners early, and sends clean formatted alerts to your Discord channel.

---

## Features

- Scans **all Pump.fun tokens** continuously (new + trending)
- Three filter tiers: **New Pairs**, **Soon to Migrate**, **Migrated**
- **Runner scoring system** (0–100) — only alerts high-potential tokens
- **RugCheck integration** for safety analysis
- **Developer wallet stats** — deploy count, migration count, success ratio
- Duplicate alert prevention with cooldown cache
- Formatted Discord embeds matching the Bagchasers style

---

## Project Structure

```
pumpfun-bot/
├── src/
│   ├── main.py                    # Entry point
│   ├── discord_bot/
│   │   └── client.py              # Discord client + scan loop
│   ├── scanner/
│   │   ├── scanner.py             # Orchestrates full scan cycle
│   │   └── scoring.py             # Runner scoring (0-100)
│   ├── pumpfun/
│   │   └── api.py                 # Pump.fun + Dexscreener API
│   ├── rugcheck/
│   │   └── rugcheck.py            # RugCheck.xyz API
│   ├── devstats/
│   │   └── devstats.py            # Developer wallet analysis
│   ├── filters/
│   │   └── filters.py             # Three filter sets
│   └── formatter/
│       └── discord_formatter.py   # Discord embed builder
├── requirements.txt
├── Procfile
├── railway.toml
├── runtime.txt
├── .env.example
└── README.md
```

---

## Setup

### 1. Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → name it (e.g. "Runner Bot")
3. Go to **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**
5. Copy the **Bot Token** (you'll need this)
6. Go to **OAuth2 → URL Generator**
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Embed Links`, `View Channels`
7. Open the generated URL and invite the bot to your server

### 2. Get Your Channel ID

1. In Discord: **Settings → Advanced → Enable Developer Mode**
2. Right-click your alert channel → **Copy Channel ID**

### 3. Configure Environment

```bash
cp .env.example .env
```

Edit `.env`:
```
DISCORD_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=your_channel_id_here
SCAN_INTERVAL_SECONDS=15
MIN_RUNNER_SCORE=55
```

### 4. Run Locally

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the bot
python src/main.py
```

---

## Deploy to Railway

### Method 1: GitHub + Railway (Recommended)

1. **Push to GitHub**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/pumpfun-bot.git
   git push -u origin main
   ```

2. **Create Railway Project**
   - Go to [Railway.app](https://railway.app)
   - Click **New Project** → **Deploy from GitHub repo**
   - Select your repository

3. **Add Environment Variables in Railway**
   - In your Railway project → **Variables** tab
   - Add each variable from your `.env` file:
     ```
     DISCORD_TOKEN         = your_bot_token
     DISCORD_CHANNEL_ID    = your_channel_id
     SCAN_INTERVAL_SECONDS = 15
     MIN_RUNNER_SCORE      = 55
     ```

4. **Deploy**
   - Railway will auto-detect `Procfile` and deploy
   - Check **Logs** tab to confirm the bot is running

### Method 2: Railway CLI

```bash
npm install -g @railway/cli
railway login
railway init
railway up
railway variables set DISCORD_TOKEN=xxx DISCORD_CHANNEL_ID=xxx
```

---

## Filter Categories

| Category | Market Cap | Age | Notes |
|---|---|---|---|
| New Pair | ≥ $3,000 | ≤ 6 seconds | Min 0.1 SOL fees |
| Soon to Migrate | ≥ $8,000 | ≤ 30 seconds | Requires socials, 20+ pro holders |
| Migrated | ≥ $10,000 | ≤ 180 seconds | 100+ pro holders, 3.5+ SOL fees |

All categories enforce: top10 ≤ 40%, insiders ≤ 40%, snipers ≤ 40%, dev ≤ 40%

---

## Runner Scoring Breakdown

| Signal | Max Points |
|---|---|
| Price change 5m (+50% = 20pts) | 20 |
| Volume spike (5m) | 15 |
| Buy pressure vs sells | 15 |
| Holder distribution | 15 |
| Low sniper concentration | 10 |
| RugCheck Good status | 10 |
| Low dev holding | 10 |
| Socials present | 5 |
| **Total** | **100** |

Default minimum: **55/100** (configurable via `MIN_RUNNER_SCORE`)

---

## Tuning

- **More alerts**: lower `MIN_RUNNER_SCORE` to e.g. 40
- **Fewer alerts**: raise `MIN_RUNNER_SCORE` to e.g. 65
- **Faster scans**: lower `SCAN_INTERVAL_SECONDS` (be careful of rate limits)
- **Alert cooldown**: edit `ALERT_COOLDOWN` in `scanner/scanner.py` (default: 3600s)

---

## Notes

- The bot only alerts Pump.fun tokens (launchpad filtering enforced)
- Tokens with RugCheck "Danger" status are always skipped regardless of score
- Dexscreener data is used for real-time price/volume; falls back to Pump.fun data
- Developer stats are cached per wallet per scan cycle
