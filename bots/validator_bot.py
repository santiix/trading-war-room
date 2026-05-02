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

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
TRADING_MODE = os.getenv("TRADING_MODE", "paper")
VALIDATOR_INTERVAL = int(os.getenv("VALIDATOR_INTERVAL", 30))
MIN_VALIDATOR_SCORE = int(os.getenv("MIN_VALIDATOR_SCORE", 75))

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ET = ZoneInfo("America/New_York")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
data_client = StockHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)

def get_premarket_bars(symbol):
    try:
        start = datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0)
        request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, limit=500)
        bars = data_client.get_stock_bars(request)
        return bars.data.get(symbol, [])
    except Exception as e:
        print(f"[BARS ERROR] {symbol}: {e}")
        return []

def calculate_vwap(bars):
    if not bars: return None
    total_pv = total_vol = 0
    for bar in bars:
        vol = float(bar.volume or 0)
        if vol <= 0: continue
        typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3
        total_pv += typical * vol
        total_vol += vol
    return round(total_pv / total_vol, 4) if total_vol > 0 else None

def calculate_ema(prices, period):
    if len(prices) < period: return None
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))
    return ema

def calculate_macd(closes):
    if len(closes) < 26: return False
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    if not ema12 or not ema26: return False
    return (ema12 - ema26) > 0

def validate_candidate(row):
    symbol = row["symbol"]
    score = 0
    reasons = []

    price = float(row.get("price") or 0)
    percent_change = float(row.get("percent_change") or 0)
    rel_vol = float(row.get("rel_vol") or 0)
    volume = int(row.get("volume") or 0)
    tier = row.get("scanner_tier")
    news_headline = row.get("news_headline")
    float_shares = int(row.get("float") or 999_999_999)

    # Price & Momentum
    if 3.00 <= price <= 20.00: score += 15; reasons.append("price $3-20")
    if percent_change >= 50: score += 30; reasons.append("monster 50%+")
    elif percent_change >= 30: score += 25; reasons.append("strong 30%+")

    # RVOL + Volume
    if rel_vol >= 10: score += 20; reasons.append("extreme RVOL")
    elif rel_vol >= 5: score += 15; reasons.append("strong RVOL")

    # News & Float
    if news_headline: score += 10; reasons.append("news catalyst")
    if float_shares <= 5_000_000: score += 15; reasons.append(f"low float ({float_shares:,})")

    # Technicals
    premarket_bars = get_premarket_bars(symbol)
    vwap = calculate_vwap(premarket_bars)

    try:
        daily_request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, limit=50)
        daily_bars = data_client.get_stock_bars(daily_request).data.get(symbol, [])
        closes = [float(b.close) for b in daily_bars] if daily_bars else []
    except:
        closes = []

    ema9 = calculate_ema(closes, 9) if closes else None
    ema20 = calculate_ema(closes, 20) if closes else None
    ema200 = calculate_ema(closes, 200) if closes else None
    macd_bullish = calculate_macd(closes)

    if ema9 and price > ema9: score += 8; reasons.append("above 9 EMA")
    if ema20 and price > ema20: score += 8; reasons.append("above 20 EMA")
    if ema200 and price > ema200: score += 5; reasons.append("above 200 EMA")
    if vwap and price >= vwap * 0.98: score += 15; reasons.append("holding VWAP")
    if macd_bullish: score += 10; reasons.append("bullish MACD")

    # Not too extended
    if premarket_bars:
        pm_high = max(float(b.high) for b in premarket_bars)
        if price <= pm_high * 1.08:
            score += 15
            reasons.append("clean - not extended")
        else:
            reasons.append("too extended")

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
        "trading_mode": TRADING_MODE,
        "created_at": datetime.now(ET).isoformat()
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
