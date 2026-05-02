import os
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

load_dotenv()

# ====================== WARRIOR TRADING CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
RISK_INTERVAL = int(os.getenv("RISK_INTERVAL", 15))

# Ross Cameron Small Account Rules
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", -100.0))
MAX_CONSECUTIVE_LOSERS = int(os.getenv("MAX_CONSECUTIVE_LOSERS", 3))

# End-of-day flat (Ross never holds overnight)
END_OF_DAY_HOUR = 15
END_OF_DAY_MINUTE = 45

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ET = ZoneInfo("America/New_York")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
alpaca_data = StockHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)
# ===================================================

def get_current_price(symbol):
    """Use bid price for stops (more conservative)"""
    try:
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = alpaca_data.get_stock_latest_quote(request)
        quote = quotes.get(symbol)
        if quote and quote.bid_price:
            return round(float(quote.bid_price), 4)
        if quote and quote.ask_price:
            return round(float(quote.ask_price), 4)
        return None
    except Exception as e:
        print(f"[PRICE ERROR] {symbol}: {e}")
        return None


def get_today_pnl():
    """True daily realized PnL"""
    today = datetime.now(ET).date().isoformat()
    response = supabase.table("bot_trades") \
        .select("pnl") \
        .eq("trading_mode", TRADING_MODE) \
        .eq("trade_status", "CLOSED") \
        .gte("closed_at", today) \
        .execute()
    return sum(float(t.get("pnl", 0)) for t in (response.data or []))


def get_consecutive_losers():
    response = supabase.table("bot_trades") \
        .select("pnl") \
        .eq("trading_mode", TRADING_MODE) \
        .eq("trade_status", "CLOSED") \
        .order("closed_at", desc=True) \
        .limit(10) \
        .execute()
    streak = 0
    for trade in (response.data or []):
        if float(trade.get("pnl", 0)) < 0:
            streak += 1
        else:
            break
    return streak


def move_to_breakeven(trade):
    """Standard Warrior breakeven move after +1R"""
    try:
        supabase.table("bot_trades").update({
            "stop_price": float(trade["entry_price"]),
            "exit_reason": "BREAKEVEN_MOVED"
        }).eq("id", trade["id"]).execute()
        print(f"→ {trade['symbol']} BREAKEVEN stop moved to entry")
    except Exception as e:
        print(f"[BREAKEVEN ERROR] {trade['symbol']}: {e}")


def force_close_all(reason: str):
    print(f"[RISK BREACH] {reason} → Closing ALL open trades")
    open_trades = supabase.table("bot_trades") \
        .select("*") \
        .eq("trade_status", "OPEN") \
        .eq("trading_mode", TRADING_MODE) \
        .execute().data or []

    for trade in open_trades:
        current_price = get_current_price(trade["symbol"]) or float(trade["entry_price"])
        pnl = round((current_price - float(trade["entry_price"])) * int(trade["shares"]), 2)
        supabase.table("bot_trades").update({
            "trade_status": "CLOSED",
            "exit_price": current_price,
            "pnl": pnl,
            "exit_reason": reason,
            "closed_at": datetime.now(ET).isoformat()
        }).eq("id", trade["id"]).execute()
        print(f"   CLOSED {trade['symbol']} | {reason} | PnL=${pnl}")


def run_risk_manager():
    print("============================================================")
    print("  Trading War Room — RISK MANAGER BOT (100% Warrior Trading)")
    print(f"  Mode: {TRADING_MODE}")
    print("============================================================")

    today_pnl = get_today_pnl()
    consec_losers = get_consecutive_losers()

    print(f"[ACCOUNT] Today PnL: ${today_pnl:.2f} | Consecutive Losers: {consec_losers}")

    # Warrior Rules 2 & 3
    if today_pnl <= DAILY_LOSS_LIMIT:
        force_close_all("DAILY_LOSS_LIMIT_HIT")
        return
    if consec_losers >= MAX_CONSECUTIVE_LOSERS:
        force_close_all("MAX_CONSECUTIVE_LOSERS_HIT")
        return

    # End-of-day flat
    now_et = datetime.now(ET).time()
    if now_et.hour >= END_OF_DAY_HOUR and now_et.minute >= END_OF_DAY_MINUTE:
        force_close_all("END_OF_DAY_FLAT")
        return

    # Per-trade monitoring
    open_trades = supabase.table("bot_trades") \
        .select("*") \
        .eq("trade_status", "OPEN") \
        .eq("trading_mode", TRADING_MODE) \
        .execute().data or []

    if not open_trades:
        print("[RISK] No open trades.")
        return

    for trade in open_trades:
        symbol = trade["symbol"]
        current_price = get_current_price(symbol) or float(trade["entry_price"])
        entry = float(trade["entry_price"])
        stop = float(trade["stop_price"])
        target = float(trade["target_price"])
        shares = int(trade["shares"])

        print(f"[CHECK] {symbol} | Price={current_price} | Stop={stop} | Target={target}")

        # Breakeven logic
        r_moved = (current_price - entry) / (entry - stop) if (entry - stop) != 0 else 0
        if r_moved >= 1.0 and stop < entry:
            move_to_breakeven(trade)

        # Exit on stop or target
        if current_price <= stop:
            pnl = round((current_price - entry) * shares, 2)
            supabase.table("bot_trades").update({
                "trade_status": "CLOSED",
                "exit_price": current_price,
                "pnl": pnl,
                "exit_reason": "STOP_HIT",
                "closed_at": datetime.now(ET).isoformat()
            }).eq("id", trade["id"]).execute()
            print(f"→ {symbol} CLOSED | STOP_HIT | PnL=${pnl}")

        elif current_price >= target:
            pnl = round((current_price - entry) * shares, 2)
            supabase.table("bot_trades").update({
                "trade_status": "CLOSED",
                "exit_price": current_price,
                "pnl": pnl,
                "exit_reason": "TARGET_HIT",
                "closed_at": datetime.now(ET).isoformat()
            }).eq("id", trade["id"]).execute()
            print(f"→ {symbol} CLOSED | TARGET_HIT | PnL=${pnl}")


def main():
    print("🚀 Warrior Trading Risk Manager Bot started (Final Polished Version)\n")
    while True:
        try:
            run_risk_manager()
        except Exception as e:
            print(f"[ERROR] {e}")

        print(f"[LOOP] Sleeping {RISK_INTERVAL}s...\n")
        time.sleep(RISK_INTERVAL)


if __name__ == "__main__":
    main()
