import os
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce

load_dotenv()

# ====================== CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
RISK_INTERVAL = int(os.getenv("RISK_INTERVAL", 15))

DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", -100.0))
MAX_CONSECUTIVE_LOSERS = int(os.getenv("MAX_CONSECUTIVE_LOSERS", 3))

ENABLE_ALPACA_ORDERS = os.getenv("ENABLE_ALPACA_ORDERS", "false").lower() == "true"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ET = ZoneInfo("America/New_York")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

alpaca_data = StockHistoricalDataClient(
    api_key=ALPACA_API_KEY,
    secret_key=ALPACA_SECRET_KEY
)

trading_client = TradingClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    paper=(TRADING_MODE == "paper")
)

END_OF_DAY_TIME = dt_time(15, 45)
# ===================================================


def get_current_price(symbol):
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
    today = datetime.now(ET).date().isoformat()

    response = (
        supabase.table("bot_trades")
        .select("pnl")
        .eq("trading_mode", TRADING_MODE)
        .eq("trade_status", "CLOSED")
        .gte("closed_at", today)
        .execute()
    )

    return sum(float(t.get("pnl", 0) or 0) for t in (response.data or []))


def get_consecutive_losers():
    response = (
        supabase.table("bot_trades")
        .select("pnl")
        .eq("trading_mode", TRADING_MODE)
        .eq("trade_status", "CLOSED")
        .order("closed_at", desc=True)
        .limit(10)
        .execute()
    )

    streak = 0

    for trade in response.data or []:
        if float(trade.get("pnl", 0) or 0) < 0:
            streak += 1
        else:
            break

    return streak


def submit_sell_order(symbol, shares, reason):
    if not ENABLE_ALPACA_ORDERS:
        print(f"[ALPACA] Orders disabled. DB-only close for {symbol}.")
        return None

    try:
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY
        )

        order = trading_client.submit_order(order_data)
        order_id = getattr(order, "id", None)

        print(f"✅ ALPACA PAPER SELL → {symbol} {shares} shares | Reason={reason} | Order ID={order_id}")
        return str(order_id) if order_id else None

    except Exception as e:
        print(f"❌ ALPACA SELL FAILED {symbol}: {e}")
        return None


def close_trade(trade, exit_price, reason):
    symbol = trade["symbol"]
    shares = int(trade.get("shares") or 0)
    entry = float(trade["entry_price"])

    if shares <= 0:
        print(f"[CLOSE ERROR] {symbol} has invalid shares.")
        return

    sell_order_id = submit_sell_order(symbol, shares, reason)

    pnl = round((exit_price - entry) * shares, 2)

    update_data = {
        "trade_status": "CLOSED",
        "exit_price": exit_price,
        "pnl": pnl,
        "exit_reason": reason,
        "closed_at": datetime.now(ET).isoformat()
    }

    if sell_order_id:
        update_data["alpaca_order_id"] = sell_order_id

    try:
        supabase.table("bot_trades").update(update_data).eq("id", trade["id"]).execute()
        print(f"→ {symbol} CLOSED | {reason} | Exit={exit_price} | PnL=${pnl}")
    except Exception as e:
        print(f"[DB CLOSE ERROR] {symbol}: {e}")


def move_to_breakeven(trade):
    symbol = trade["symbol"]

    try:
        supabase.table("bot_trades").update({
            "stop_price": float(trade["entry_price"]),
            "exit_reason": "BREAKEVEN_MOVED"
        }).eq("id", trade["id"]).execute()

        print(f"→ {symbol} BREAKEVEN stop moved to entry")

    except Exception as e:
        print(f"[BREAKEVEN ERROR] {symbol}: {e}")


def get_open_trades():
    response = (
        supabase.table("bot_trades")
        .select("*")
        .eq("trade_status", "OPEN")
        .eq("trading_mode", TRADING_MODE)
        .execute()
    )

    return response.data or []


def force_close_all(reason):
    print(f"[RISK BREACH] {reason} → Closing ALL open trades")

    open_trades = get_open_trades()

    if not open_trades:
        print("[RISK] No open trades to force close.")
        return

    for trade in open_trades:
        symbol = trade["symbol"]
        current_price = get_current_price(symbol)

        if current_price is None:
            current_price = float(trade["entry_price"])
            print(f"[RISK] {symbol} no price available — using entry price for emergency close.")

        close_trade(trade, current_price, reason)


def run_risk_manager():
    print("============================================================")
    print("  Trading War Room — RISK MANAGER")
    print(f"  Mode: {TRADING_MODE} | Alpaca Orders: {ENABLE_ALPACA_ORDERS}")
    print("============================================================")

    today_pnl = get_today_pnl()
    consecutive_losers = get_consecutive_losers()

    print(f"[ACCOUNT] Today PnL=${today_pnl:.2f} | Consecutive Losers={consecutive_losers}")

    if today_pnl <= DAILY_LOSS_LIMIT:
        force_close_all("DAILY_LOSS_LIMIT_HIT")
        return

    if consecutive_losers >= MAX_CONSECUTIVE_LOSERS:
        force_close_all("MAX_CONSECUTIVE_LOSERS_HIT")
        return

    now_et = datetime.now(ET).time()

    if now_et >= END_OF_DAY_TIME:
        force_close_all("END_OF_DAY_FLAT")
        return

    open_trades = get_open_trades()

    if not open_trades:
        print("[RISK] No open trades.")
        return

    for trade in open_trades:
        symbol = trade["symbol"]

        current_price = get_current_price(symbol)
        if current_price is None:
            print(f"[RISK] No current price for {symbol} — skipping this cycle.")
            continue

        entry = float(trade["entry_price"])
        stop = float(trade["stop_price"])
        target = float(trade["target_price"])
        shares = int(trade.get("shares") or 0)

        print(
            f"[CHECK] {symbol} | Price={current_price} | Entry={entry} | "
            f"Stop={stop} | Target={target} | Shares={shares}"
        )

        risk_per_share = entry - stop

        if risk_per_share > 0:
            r_moved = (current_price - entry) / risk_per_share

            if r_moved >= 1.0 and stop < entry:
                move_to_breakeven(trade)
                stop = entry

        if current_price <= stop:
            close_trade(trade, current_price, "STOP_HIT")

        elif current_price >= target:
            close_trade(trade, current_price, "TARGET_HIT")


def main():
    print("🚀 Risk Manager Bot started — Alpaca Paper Exit Ready\n")

    while True:
        try:
            run_risk_manager()
        except Exception as e:
            print(f"[RISK LOOP ERROR] {e}")

        print(f"[LOOP] Sleeping {RISK_INTERVAL}s...\n")
        time.sleep(RISK_INTERVAL)


if __name__ == "__main__":
    main()
