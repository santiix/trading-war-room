🧠 Trading War Room — Master System Doc
🎯 Goal

Build a multi-bot trading system that:

Finds A+ momentum stocks
Trades only high-probability setups
Scales from small account → larger capital
Targets consistent daily/weekly gains, not gambling
🧱 SYSTEM ARCHITECTURE
🤖 Bot Roles
1. Scanner Bot (YOU JUST BUILT)

Purpose:

Find top market movers
Apply strict filters
Output only:
A_SETUP
WATCH
2. Validator Bot (Next)

Purpose:

Confirm setups are “clean”
Remove:
choppy charts
fake breakouts
illiquid junk
3. Entry Bot

Purpose:

Execute trades based on patterns:
premarket breakout
bull flag
pullback continuation
4. Risk Manager Bot

Purpose:

Enforce:
max daily loss
position sizing
stop trading after streak losses
5. Overseer Bot (Controller)

Purpose:

Coordinates everything
Stops trading when:
market is cold
performance drops
Adjusts aggression (size up/down)
📊 CORE STRATEGY (FROM DOCS)
🧱 Stock Selection (STRICT)

Must meet:

Price: $1 – $20
% Change: ≥ 10% (20% preferred)
Volume: ≥ 1M
Relative Volume: ≥ 5x
Spread: ≤ 1.5%
Float: < 20M ideal

👉 If not → REJECT

🧠 Philosophy

“Only trade obvious stocks”

Meaning:

Top 2–3 movers
Clean charts
High volume
Strong continuation potential
🚫 What we ignore
Slow movers
Low volume
<10% gainers
random stocks
“maybe” setups
⚙️ CURRENT SCANNER (FINAL VERSION)
Flow
Alpaca Movers API
→ Top 50 gainers
→ Snapshot data
→ Apply strict filters
→ Classify:
   A_SETUP / WATCH / REJECT
→ Save to Supabase
🧪 CLASSIFICATION LOGIC
A_SETUP (TRADE CANDIDATE)

Must meet ALL:

price in range
% change ≥ 20%
volume ≥ 1M
RVOL ≥ 5x
spread OK
WATCH
% change ≥ 10%
decent volume
still developing
REJECT

Anything else

📈 RELATIVE VOLUME (RVOL)
Formula
RVOL = current_volume / expected_volume

Expected volume:

avg_daily_volume * (minutes_since_open / 390)
Why it matters

RVOL tells you:

real attention
real momentum
real traders involved

👉 This is a hard requirement

🗄️ DATABASE (SUPABASE)
create table public.bot_watchlist (
  id uuid primary key default gen_random_uuid(),
  symbol text not null,
  price numeric,
  prev_close numeric,
  percent_change numeric,
  volume bigint,
  rel_vol numeric,
  spread_pct numeric,
  scanner_tier text,
  reason text,
  passed_core_filter boolean default false,
  trading_mode text default 'paper',
  created_at timestamptz default now(),
  scanned_at timestamptz
);


⚙️ ENV VARIABLES
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_DATA_BASE_URL=https://data.alpaca.markets

SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=

SCAN_INTERVAL_SECONDS=60
FAST_WATCH_INTERVAL_SECONDS=20
MOVERS_TOP=50
BATCH_SIZE=50
TRADING_MODE=paper
🚀 CURRENT STATE

✅ Scanner bot complete
✅ Real-time data working
✅ Filtering logic working
✅ Supabase integration working
✅ Movers-based scanning (no brute force)

🔜 NEXT STEPS
Step 1 — Stabilize Scanner
ensure no API errors
monitor RVOL calls
Step 2 — Build Validator Bot

Add:

candle pattern recognition
breakout structure
reject choppy charts
Step 3 — Entry Logic

Implement:

premarket high break
pullback entry
breakout confirmation
Step 4 — Risk Management

Rules:

risk per trade: $50–$100
stop loss: fixed %
3 losers → STOP
max daily loss
Step 5 — Automation
paper trade via Alpaca
log all trades
track win rate
⚠️ REALITY CHECK
What this system WILL do
find high-quality setups
reduce bad trades
give you consistency
What it will NOT do
guarantee profits
find trades every minute
turn $2k into millions overnight
🧠 FINAL PRINCIPLE

“No trade is better than a bad trade”

🔥 YOUR EDGE

You now have:

structured system
strict filtering
automation path
scalable architecture
