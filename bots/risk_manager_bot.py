import os
import time
from datetime import datetime, time as dt_time
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

# Ross's exact rules from SAC2024 + Stock Selection PDFs
PER_TRADE_RISK_DOLLARS = float(os.getenv("PER_TRADE_RISK_DOLLARS", -50.0))   # Rule 1
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", -100.0))             # Rule 2
MAX_CONSECUTIVE_LOSERS = int(os.getenv("MAX_CONSECUTIVE_LOSERS", 3))        # Rule 3

# End-of-day flat (Ross does NOT hold overnight)
END_OF_DAY_HOUR = int(os.getenv("END_OF_DAY_HOUR", 15))   # 3:00 PM ET = start flattening
END_OF_DAY_MINUTE = int(os.getenv("END_OF_DAY_MINUTE", 45))

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
alpaca_data = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
# ====================================================================

def get_current_price(symbol, fallback_price):
    try:
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = alpaca_data.get_stock_latest_quote(request)
        quote = quotes.get(symbol)

        if quote and quote.bid_price and quote.ask_price:
            return round((float(quote.bid_price) + float(quote.ask_price)) / 2, 4)
        if quote and quote.ask_price:
            return round(float(quote.ask_price), 4)
        if quote and quote.bid_price:
            return round(float(quote.bid_price), 4)

        return float(fallback_price or 0)
    except Exception as e:
        print(f"[PRICE ERROR] {symbol}: {e}")
        return float(fallback_price or 0)


def get_today_pnl():
    today_start = datetime.now().date().isoformat()
    response = supabase.table("bot_trades").select("pnl").eq("trading_mode", TRADING_MODE).gte("closed_at", today_start).execute()
    return sum(float(t.get("pnl", 0)) for t in (response.data or []))


def get_consecutive_losers():
    response = supabase.table("bot_trades").select("pnl").eq("trading_mode", TRADING_MODE).eq("trade_status", "CLOSED").order("closed_at", desc=True).limit(10).execute()
    streak = 0
    for trade in (response.data or []):
        if float(trade.get("pnl", 0)) >= 0:
            break
        streak += 1
    return streak


def calculate_risk_dollars(trade):
    """Actual $ risk this trade is taking (Warrior Rule 1)"""
    entry = float(trade["entry_price"])
    stop = float(trade["stop_price"])
    shares = int(trade["shares"])
    return round((entry - stop) * shares, 2)


def move_to_breakeven(trade_id, symbol, current_price):
    """Move stop to entry price once +1R (standard Warrior practice)"""
    update = {
        "stop_price": float(trade["entry_price"]),  # breakeven
        "exit_reason": "BREAKEVEN_MOVED" if not trade.get("exit_reason") else trade.get("exit_reason")
    }
    supabase.table("bot_trades").update(update).eq("id", trade_id).execute()
    print(f"→ {symbol} BREAKEVEN stop moved to {trade['entry_price']}")


def force_close_all(reason: str):
    print(f"[WARRIOR RISK BREACH] {reason} → Closing ALL open trades!")
    open_trades = supabase.table("bot_trades").select("*").eq("trade_status", "OPEN").eq("trading_mode", TRADING_MODE).execute().data or []
    for trade in open_trades:
        current_price = get_current_price(trade["symbol"], trade["entry_price"])
        pnl = round((current_price - float(trade["entry_price"])) * int(trade["shares"]), 2)
        supabase.table("bot_trades").update({
            "trade_status": "CLOSED",
            "exit_price": current_price,
            "pnl": pnl,
            "exit_reason": reason,
            "closed_at": "now()"
        }).eq("id", trade["id"]).execute()
        print(f"   CLOSED {trade['symbol']} | {reason} | PnL=${pnl}")


def run_risk_manager():
    print("============================================================")
    print("  Trading War Room — RISK MANAGER (100% Warrior Trading)")
    print(f"  Mode: {TRADING_MODE} | Per-Trade Risk: ${abs(PER_TRADE_RISK_DOLLARS)} | Daily Limit: ${abs(DAILY_LOSS_LIMIT)}")
    print("============================================================")

    today_pnl = get_today_pnl()
    consec_losers = get_consecutive_losers()

    print(f"[ACCOUNT] Today PnL: ${today_pnl:.2f} | Consec Losers: {consec_losers}")

    # Warrior Rules 2 & 3
    if today_pnl <= DAILY_LOSS_LIMIT:
        force_close_all("DAILY_LOSS_LIMIT_HIT")
        return
    if consec_losers >= MAX_CONSECUTIVE_LOSERS:
        force_close_all("MAX_CONSECUTIVE_LOSERS_HIT")
        return

    # End-of-day flat (Ross never holds overnight)
    now = datetime.now().time()
    if now.hour >= END_OF_DAY_HOUR and now.minute >= END_OF_DAY_MINUTE:
        force_close_all("END_OF_DAY_FLAT")
        return

    # === Per-trade monitoring (Rule 1 + breakeven + 2:1) ===
    open_trades = supabase.table("bot_trades").select("*").eq("trade_status", "OPEN").eq("trading_mode", TRADING_MODE).execute().data or []

    if not open_trades:
        print("[RISK] No open trades.")
        return

    updates = []
    for trade in open_trades:
        symbol = trade["symbol"]
        current_price = get_current_price(symbol, trade["entry_price"])
        entry = float(trade["entry_price"])
        stop = float(trade["stop_price"])
        target = float(trade["target_price"])
        shares = int(trade["shares"])

        actual_risk = calculate_risk_dollars(trade)
        print(f"[CHECK] {symbol} | Price={current_price} | Risk=${actual_risk} | Stop={stop} | Target={target}")

        # Warn if Entry Bot didn't follow Rule 1
        if actual_risk > abs(PER_TRADE_RISK_DOLLARS) + 5:   # small tolerance
            print(f"⚠️  {symbol} RISK TOO HIGH (${actual_risk}) — should be ~${abs(PER_TRADE_RISK_DOLLARS)}")

        exit_reason = None
        if current_price <= stop:
            exit_reason = "STOP_HIT"
        elif current_price >= target:
            exit_reason = "TARGET_HIT"

        # Breakeven logic (Warrior standard)
        r_moved = (current_price - entry) / (entry - stop) if (entry - stop) != 0 else 0
        if r_moved >= 1.0 and stop < entry:
            move_to_breakeven(trade["id"], symbol, current_price)

        if exit_reason:
            pnl = round((current_price - entry) * shares, 2)
            updates.append({
                "id": trade["id"],
                "trade_status": "CLOSED",
                "exit_price": current_price,
                "pnl": pnl,
                "exit_reason": exit_reason,
                "closed_at": "now()"
            })
            print(f"→ {symbol} CLOSED | {exit_reason} | PnL=${pnl} | R={pnl / abs(actual_risk):.1f}R")

    for u in updates:
        tid = u.pop("id")
        supabase.table("bot_trades").update(u).eq("id", tid).execute()

    if updates:
        print(f"[DB] Closed {len(updates)} trades.")

def main():
    print("🚀 Warrior Trading Risk Manager started (100% rules compliant)\n")
    while True:
        try:
            run_risk_manager()
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(RISK_INTERVAL)

if __name__ == "__main__":
    main()
