import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

load_dotenv()

# ====================== WARRIOR TRADING CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
VALIDATOR_INTERVAL = int(os.getenv("VALIDATOR_INTERVAL", 30))

MIN_VALIDATOR_SCORE = int(os.getenv("MIN_VALIDATOR_SCORE", 75))

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ET = ZoneInfo("America/New_York")

# Early credential check
if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    raise RuntimeError("❌ Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
data_client = StockHistoricalDataClient(
    api_key=ALPACA_API_KEY,
    secret_key=ALPACA_SECRET_KEY
)
# ===================================================

def get_premarket_bars(symbol):
    try:
        start = datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            limit=500
        )
        bars = data_client.get_stock_bars(request)
        return bars.data.get(symbol, [])
    except Exception as e:
        print(f"[BARS ERROR] {symbol}: {e}")
        return []


def calculate_vwap(bars):
    if not bars:
        return None
    total_pv = 0
    total_vol = 0
    for bar in bars:
        vol = float(bar.volume or 0)
        if vol <= 0:
            continue
        typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3
        total_pv += typical * vol
        total_vol += vol
    return round(total_pv / total_vol, 4) if total_vol > 0 else None


def get_daily_emas(symbol):
    try:
        request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, limit=30)
        bars = data_client.get_stock_bars(request).data.get(symbol, [])
        if len(bars) < 20:
            return {"above_20": False, "above_50": False, "above_200": False}

        closes = [float(b.close) for b in bars]
        price = closes[-1]

        ema20 = sum(closes[-20:]) / 20
        ema50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else ema20
        ema200 = sum(closes) / len(closes) if len(closes) >= 200 else ema50

        return {
            "above_20": price > ema20,
            "above_50": price > ema50,
            "above_200": price > ema200,
        }
    except Exception as e:
        print(f"[EMA ERROR] {symbol}: {e}")
        return {"above_20": False, "above_50": False, "above_200": False}


def validate_candidate(row):
    symbol = row["symbol"]
    score = 0
    reasons = []

    price = float(row.get("price") or 0)
    percent_change = float(row.get("percent_change") or 0)
    rel_vol = float(row.get("rel_vol") or 0)
    volume = int(row.get("volume") or 0)
    spread = float(row.get("spread_pct") or 999)
    tier = row.get("scanner_tier")

    # 1. Price Range ($3.00 - $20.00)
    if 3.00 <= price <= 20.00:
        score += 15
        reasons.append("price in ideal $3-20 range")
    else:
        reasons.append("price outside $3-20")

    # 2. Strong Momentum (30%+ preferred)
    if percent_change >= 50:
        score += 30
        reasons.append("monster 50%+ move")
    elif percent_change >= 30:
        score += 25
        reasons.append("strong 30%+ move")
    elif percent_change >= 15:
        score += 10
        reasons.append("decent mover")

    # 3. RVOL & Volume
    if rel_vol >= 10:
        score += 20
        reasons.append("extreme RVOL")
    elif rel_vol >= 5:
        score += 15
        reasons.append("strong RVOL")

    if volume >= 2_000_000:
        score += 10
        reasons.append("heavy volume")

    # 4. Technical Cleanliness (Warrior chart rules)
    premarket_bars = get_premarket_bars(symbol)
    vwap = calculate_vwap(premarket_bars)
    emas = get_daily_emas(symbol)

    if vwap and price >= vwap * 0.98:
        score += 15
        reasons.append("holding above VWAP")
    if emas["above_20"] and emas["above_50"]:
        score += 10
        reasons.append("strong daily trend")
    if emas["above_200"]:
        score += 5
        reasons.append("above 200 EMA")

    # 5. Not too extended from premarket high
    if premarket_bars:
        pm_high = max(float(b.high) for b in premarket_bars)
        if price <= pm_high * 1.08:
            score += 15
            reasons.append("clean setup - not extended")
        else:
            reasons.append("too extended from PM high")

    # 6. Scanner Tier
    if tier == "A_SETUP":
        score += 10
        reasons.append("A_SETUP + news")

    status = "VALIDATED" if score >= MIN_VALIDATOR_SCORE else "REJECTED_BY_VALIDATOR"

    print(f"[VALIDATOR] {symbol} → {status} | Score: {score}/100")

    return {
        "watchlist_id": row["id"],
        "symbol": symbol,
        "validator_status": status,
        "validator_score": score,
        "reason": " | ".join(reasons),
        "price": price,
        "percent_change": percent_change,
        "volume": volume,
        "rel_vol": rel_vol,
        "spread_pct": spread,
        "scanner_tier": tier,
        "trading_mode": TRADING_MODE,
        "validated_at": datetime.now(ET).isoformat()
    }


def run_validator():
    print("============================================================")
    print("  Trading War Room — VALIDATOR BOT (100% Warrior Trading)")
    print(f"  Mode: {TRADING_MODE} | Min Score: {MIN_VALIDATOR_SCORE}")
    print("============================================================")

    response = (
        supabase.table("bot_watchlist")
        .select("*")
        .in_("scanner_tier", ["A_SETUP", "WATCH"])
        .eq("trading_mode", TRADING_MODE)
        .order("created_at", desc=True)
        .limit(25)
        .execute()
    )

    rows = response.data or []
    if not rows:
        print("[VALIDATOR] No candidates to validate.")
        return

    results = [validate_candidate(r) for r in rows]

    supabase.table("bot_validations").upsert(results, on_conflict="watchlist_id").execute()
    print(f"[DB] Saved {len(results)} validation rows.")


def main():
    print("🚀 Warrior Trading Validator Bot started\n")
    while True:
        try:
            run_validator()
        except Exception as e:
            print(f"[ERROR] {e}")

        print(f"[LOOP] Sleeping {VALIDATOR_INTERVAL}s...\n")
        time.sleep(VALIDATOR_INTERVAL)


if __name__ == "__main__":
    main()
