import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

load_dotenv()

# ====================== CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
VALIDATOR_INTERVAL = int(os.getenv("VALIDATOR_INTERVAL", 30))

# Warrior minimum score to be VALIDATED
MIN_VALIDATOR_SCORE = int(os.getenv("MIN_VALIDATOR_SCORE", 75))

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ET = ZoneInfo("America/New_York")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
# ===================================================

def get_premarket_bars(symbol):
    """Get today's premarket bars for chart strength"""
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
    """Simple check if price is above key daily EMAs"""
    try:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            limit=30
        )
        bars = data_client.get_stock_bars(request).data.get(symbol, [])
        if len(bars) < 20:
            return {"above_20": False, "above_50": False, "above_200": False}

        closes = [float(b.close) for b in bars]
        current_price = closes[-1]

        # Very simple EMA approximation for scoring
        ema20 = sum(closes[-20:]) / 20
        ema50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else ema20
        ema200 = sum(closes) / len(closes) if len(closes) >= 200 else ema50

        return {
            "above_20": current_price > ema20,
            "above_50": current_price > ema50,
            "above_200": current_price > ema200,
        }
    except Exception as e:
        print(f"[EMA ERROR] {symbol}: {e}")
        return {"above_20": False, "above_50": False, "above_200": False}


def validate_candidate(row):
    symbol = row["symbol"]
    score = 0
    reasons = []

    # Base from scanner
    percent_change = float(row.get("percent_change") or 0)
    rel_vol = float(row.get("rel_vol") or 0)
    volume = int(row.get("volume") or 0)
    spread = float(row.get("spread_pct") or 999)
    tier = row.get("scanner_tier")

    # 1. Momentum (Ross loves big movers)
    if percent_change >= 30:
        score += 25
        reasons.append("monster % change")
    elif percent_change >= 20:
        score += 20
        reasons.append("strong % change")
    elif percent_change >= 10:
        score += 10
        reasons.append("ok % change")

    # 2. RVOL & Volume
    if rel_vol >= 10:
        score += 20
        reasons.append("extreme RVOL")
    elif rel_vol >= 5:
        score += 15
        reasons.append("strong RVOL")

    if volume >= 2_000_000:
        score += 15
        reasons.append("heavy volume")

    # 3. Technical Cleanliness (Warrior core)
    premarket_bars = get_premarket_bars(symbol)
    vwap = calculate_vwap(premarket_bars + get_premarket_bars(symbol))  # full day for accuracy
    emas = get_daily_emas(symbol)

    if vwap and float(row.get("price", 0)) >= vwap * 0.98:
        score += 15
        reasons.append("holding VWAP")
    else:
        reasons.append("below VWAP")

    if emas["above_20"] and emas["above_50"]:
        score += 10
        reasons.append("strong daily trend")
    if emas["above_200"]:
        score += 5
        reasons.append("above 200 EMA")

    # 4. Not too extended / clean setup
    if premarket_bars:
        pm_high = max(float(b.high) for b in premarket_bars)
        price = float(row.get("price", 0))
        if price <= pm_high * 1.08:   # not too extended
            score += 15
            reasons.append("not extended")
        else:
            reasons.append("too extended")

    # 5. Scanner Tier & News (already filtered)
    if tier == "A_SETUP":
        score += 10
        reasons.append("A_SETUP + news")

    # Final status
    status = "VALIDATED" if score >= MIN_VALIDATOR_SCORE else "REJECTED_BY_VALIDATOR"

    print(f"[VALIDATOR] {symbol} → {status} | Score: {score}/100")

    return {
        "watchlist_id": row["id"],
        "symbol": symbol,
        "validator_status": status,
        "validator_score": score,
        "reason": " | ".join(reasons),
        "price": row.get("price"),
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

    # Save to bot_validations
    supabase.table("bot_validations") \
        .upsert(results, on_conflict="watchlist_id") \
        .execute()

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
