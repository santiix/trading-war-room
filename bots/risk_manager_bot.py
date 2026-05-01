import os
import time
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

load_dotenv()

# ====================== CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
RISK_INTERVAL = int(os.getenv("RISK_INTERVAL", 15))

# Warrior Trading Risk Rules (from SAC2024 & Stock Selection PDFs)
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", -100.0))          # Rule 2
MAX_CONSECUTIVE_LOSERS = int(os.getenv("MAX_CONSECUTIVE_LOSERS", 3))     # Rule 3

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
alpaca_data = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
# ===================================================

def get_current_price(symbol, fallback_price):
    """Gets latest bid/ask midpoint from Alpaca (conservative for momentum stocks)."""
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

        print(f"[PRICE] No quote for {symbol}. Using fallback.")
        return float(fallback_price or 0)
    except Exception as e:
        print(f"[PRICE ERROR] {symbol}: {e}")
        return float(fallback_price or 0)


def get_today_pnl():
    """Sum of realized P&L for today (Warrior Trading daily loss rule)."""
    today_start = datetime.now().date().isoformat()
    response = (
        supabase.table("bot_trades")
        .select("pnl")
        .eq("trading_mode", TRADING_MODE)
        .gte("closed_at", today_start)
        .execute()
    )
    trades = response.data or []
    return sum(float(t.get("pnl", 0)) for t in trades)


def get_consecutive_losers():
    """Count how many losing trades in a row (Warrior Trading Rule 3)."""
    response = (
        supabase.table("bot_trades")
        .select("pnl")
        .eq("trading_mode", TRADING_MODE)
        .eq("trade_status", "CLOSED")
        .order("closed_at", desc=True)
        .limit(10)
        .execute()
    )
    trades = response.data or []
    streak = 0
    for trade in trades:
        if float(trade.get("pnl", 0)) >= 0:
            break
        streak += 1
    return streak


def force_close_all_open_trades(reason: str):
    """Emergency close all open trades (daily loss or consecutive losers)."""
    response = (
        supabase.table("bot_trades")
        .select("*")
        .eq("trade_status", "OPEN")
        .eq("trading_mode", TRADING_MODE)
        .execute()
    )
    open_trades = response.data or []

    if not open_trades:
        return

    print(f"[RISK BREACH] {reason} → Forcing close of {len(open_trades)} open trades!")

    for trade in open_trades:
        symbol = trade["symbol"]
        current_price = get_current_price(symbol, trade["entry_price"])
        entry = float(trade["entry_price"])
        shares = int(trade["shares"])
        pnl = round((current_price - entry) * shares, 2)

        update = {
            "trade_status": "CLOSED",
            "exit_price": current_price,
            "pnl": pnl,
            "exit_reason": reason,
            "closed_at": "now()"
        }
        supabase.table("bot_trades").update(update).eq("id", trade["id"]).execute()
        print(f"   → {symbol} CLOSED | {reason} | PnL=${pnl}")


def run_risk_manager():
    print("============================================================")
    print("  Trading War Room — Risk Manager Bot (Warrior Trading Rules)")
    print(f"  Mode: {TRADING_MODE} | Interval: {RISK_INTERVAL}s")
    print("============================================================")

    # === ACCOUNT-LEVEL RISK (Warrior Trading Small Account Rules) ===
    today_pnl = get_today_pnl()
    consec_losers = get_consecutive_losers()

    print(f"[ACCOUNT] Today's PnL: ${today_pnl:.2f} | Consecutive Losers: {consec_losers}")

    if today_pnl <= DAILY_LOSS_LIMIT:
        force_close_all_open_trades("DAILY_LOSS_LIMIT_HIT")
        return  # stop further processing today

    if consec_losers >= MAX_CONSECUTIVE_LOSERS:
        force_close_all_open_trades("MAX_CONSECUTIVE_LOSERS_HIT")
        return

    # === PER-TRADE RISK (Stop / Target + 2:1 enforcement) ===
    response = (
        supabase.table("bot_trades")
        .select("*")
        .eq("trade_status", "OPEN")
        .eq("trading_mode", TRADING_MODE)
        .execute()
    )
    trades = response.data or []

    if not trades:
        print("[RISK] No open trades.")
        return

    updates = []

    for trade in trades:
        symbol = trade["symbol"]
        current_price = get_current_price(symbol, trade["entry_price"])

        entry = float(trade["entry_price"])
        stop = float(trade["stop_price"])
        target = float(trade["target_price"])
        shares = int(trade["shares"])

        print(f"[CHECK] {symbol} | Current={current_price} | Stop={stop} | Target={target}")

        exit_reason = None
        if current_price <= stop:
            exit_reason = "STOP_HIT"
        elif current_price >= target:
            exit_reason = "TARGET_HIT"

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
            print(f"→ {symbol} CLOSED | {exit_reason} | PnL=${pnl}")

    # Apply DB updates
    for u in updates:
        trade_id = u.pop("id")
        supabase.table("bot_trades").update(u).eq("id", trade_id).execute()

    if updates:
        print(f"[DB] Updated {len(updates)} closed trades.")


def main():
    print("🚀 Risk Manager Bot started (following Warrior Trading rules)...\n")
    while True:
        try:
            run_risk_manager()
        except Exception as e:
            print(f"[ERROR] {e}")

        print(f"[LOOP] Sleeping {RISK_INTERVAL}s...\n")
        time.sleep(RISK_INTERVAL)


if __name__ == "__main__":
    main()
