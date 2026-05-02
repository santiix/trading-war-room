import os
import time
import math
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce

load_dotenv()

# ====================== WARRIOR TRADING CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
ENTRY_INTERVAL = int(os.getenv("ENTRY_INTERVAL", 10))

RISK_DOLLARS = float(os.getenv("RISK_DOLLARS", 50))
MAX_TRADE_DOLLARS = float(os.getenv("MAX_TRADE_DOLLARS", 1000))
STOP_PERCENT = float(os.getenv("STOP_PERCENT", 5)) / 100

MIN_VALIDATOR_SCORE = int(os.getenv("MIN_VALIDATOR_SCORE", 75))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", 3))

ENTRY_WINDOW_START = os.getenv("ENTRY_WINDOW_START", "09:30")
ENTRY_WINDOW_END = os.getenv("ENTRY_WINDOW_END", "10:00")
ENABLE_ALPACA_ORDERS = os.getenv("ENABLE_ALPACA_ORDERS", "false").lower() == "true"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ET = ZoneInfo("America/New_York")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
data_client = StockHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=(TRADING_MODE == "paper"))
# ===================================================

def is_entry_window_open():
    current = datetime.now(ET).time()
    start = dt_time(9, 30)
    end = dt_time(10, 0)
    return start <= current <= end


def trading_enabled():
    try:
        resp = supabase.table("bot_control").select("is_enabled").eq("trading_mode", TRADING_MODE).limit(1).execute()
        return resp.data and resp.data[0].get("is_enabled", True)
    except:
        return True


def count_open_trades():
    resp = supabase.table("bot_trades").select("id", count="exact").eq("trade_status", "OPEN").eq("trading_mode", TRADING_MODE).execute()
    return resp.count or 0


def get_current_price(symbol):
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = data_client.get_stock_latest_quote(req)
        quote = quotes.get(symbol)
        return round(float(quote.ask_price), 4) if quote and quote.ask_price else None
    except Exception as e:
        print(f"[PRICE ERROR] {symbol}: {e}")
        return None


def is_first_new_high_candle(symbol, current_price):
    """Prioritizes the exact pattern from your image: first candle making a new high"""
    try:
        start = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, limit=120)
        bars = data_client.get_stock_bars(request).data.get(symbol, [])

        if len(bars) < 15:
            return False

        # Find premarket high
        pm_high = max(float(bar.high) for bar in bars if bar.timestamp.astimezone(ET).time() < dt_time(9, 30))

        # Check last few candles for the first new high break
        recent_bars = bars[-12:]
        for i in range(1, len(recent_bars)):
            prev_high = float(recent_bars[i-1].high)
            current_high = float(recent_bars[i].high)
            current_close = float(recent_bars[i].close)

            if current_high > pm_high and current_close > prev_high:
                return True
        return False
    except Exception as e:
        print(f"[PATTERN CHECK ERROR] {symbol}: {e}")
        return False


def build_trade(row):
    symbol = row["symbol"]
    watchlist_id = row.get("watchlist_id")

    current_price = get_current_price(symbol)
    if not current_price:
        return None

    # === PRIORITIZE THE EXACT PATTERN FROM YOUR IMAGE ===
    if is_first_new_high_candle(symbol, current_price):
        entry_reason = "FIRST_NEW_HIGH_CANDLE"
    else:
        entry_reason = "PREMARKET_HIGH_BREAKOUT_OR_PULLBACK"

    # Re-check float at exact entry time
    try:
        float_resp = supabase.table("bot_watchlist").select("float").eq("id", watchlist_id).limit(1).execute()
        float_shares = int(float_resp.data[0].get("float") or 999_999_999) if float_resp.data else 999_999_999
        if float_shares > 5_000_000:
            print(f"[ENTRY] {symbol} float too high ({float_shares:,}) — skipping")
            return None
    except:
        pass

    # Warrior risk sizing
    stop_price = round(current_price * (1 - STOP_PERCENT), 4)
    risk_per_share = current_price - stop_price
    if risk_per_share <= 0:
        return None

    shares = min(
        math.floor(RISK_DOLLARS / risk_per_share),
        math.floor(MAX_TRADE_DOLLARS / current_price)
    )
    if shares < 1:
        return None

    target_price = round(current_price + (risk_per_share * 2), 4)

    # Read news headline for better logging
    news_headline = row.get("news_headline") or "No headline"

    return {
        "validation_id": row["id"],
        "watchlist_id": watchlist_id,
        "symbol": symbol,
        "trade_status": "OPEN",
        "trade_type": "PAPER_LONG",
        "entry_price": current_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "risk_per_share": risk_per_share,
        "reward_per_share": round(target_price - current_price, 4),
        "shares": shares,
        "risk_dollars": round(shares * risk_per_share, 2),
        "validator_score": row.get("validator_score"),
        "entry_reason": f"{entry_reason} | {news_headline[:100]}",
        "trading_mode": TRADING_MODE,
        "created_at": datetime.now(ET).isoformat()
    }


def run_entry_bot():
    print("============================================================")
    print("  Trading War Room — ENTRY BOT (100% Warrior Trading)")
    print(f"  Mode: {TRADING_MODE} | Window: 09:30-10:00 ET | Risk: ${RISK_DOLLARS}")
    print("============================================================")

    if not is_entry_window_open():
        print("[ENTRY] Outside 9:30-10:00 ET window.")
        return

    if not trading_enabled():
        print("[ENTRY] Overseer has disabled trading.")
        return

    if count_open_trades() >= MAX_OPEN_TRADES:
        print(f"[ENTRY] Max open trades ({MAX_OPEN_TRADES}) reached.")
        return

    response = (
        supabase.table("bot_validations")
        .select("*, bot_watchlist!inner(news_headline)")
        .eq("validator_status", "VALIDATED")
        .eq("trading_mode", TRADING_MODE)
        .gte("validator_score", MIN_VALIDATOR_SCORE)
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    )

    rows = response.data or []
    if not rows:
        print("[ENTRY] No validated setups found.")
        return

    for row in rows:
        trade = build_trade(row)
        if not trade:
            continue

        result = supabase.table("bot_trades").insert(trade).execute()
        trade_id = result.data[0]["id"]

        print(f"[ENTRY] ✅ {trade['symbol']} | {trade['entry_reason']} | Shares={trade['shares']} | Risk=${trade['risk_dollars']}")

        if ENABLE_ALPACA_ORDERS:
            try:
                order_data = MarketOrderRequest(
                    symbol=trade["symbol"],
                    qty=trade["shares"],
                    side=OrderSide.BUY,
                    type=OrderType.MARKET,
                    time_in_force=TimeInForce.DAY
                )
                order = trading_client.submit_order(order_data)
                print(f"✅ ALPACA ORDER PLACED → {trade['symbol']} {trade['shares']} shares")
            except Exception as e:
                print(f"❌ ORDER FAILED {trade['symbol']}: {e}")

        supabase.table("bot_validations").update({"trade_id": trade_id}).eq("id", row["id"]).execute()


def main():
    print("🚀 Warrior Trading Entry Bot started (100% compliant + first new high priority)\n")
    while True:
        try:
            run_entry_bot()
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(ENTRY_INTERVAL)


if __name__ == "__main__":
    main()
