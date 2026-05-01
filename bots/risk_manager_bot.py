import os
import time
from dotenv import load_dotenv
from supabase import create_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
RISK_INTERVAL = int(os.getenv("RISK_INTERVAL", 15))

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

alpaca_data = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)


def get_current_price(symbol, fallback_price):
    """
    Gets latest bid/ask midpoint from Alpaca.
    Falls back to entry price if Alpaca fails.
    """

    try:
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = alpaca_data.get_stock_latest_quote(request)

        quote = quotes.get(symbol)

        if not quote:
            print(f"[PRICE] No quote returned for {symbol}. Using fallback.")
            return float(fallback_price or 0)

        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)

        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4)

        if ask > 0:
            return round(ask, 4)

        if bid > 0:
            return round(bid, 4)

        print(f"[PRICE] Invalid bid/ask for {symbol}. Using fallback.")
        return float(fallback_price or 0)

    except Exception as e:
        print(f"[PRICE ERROR] {symbol}: {e}")
        return float(fallback_price or 0)


def run_risk_manager():
    print("============================================================")
    print("  Trading War Room — Risk Manager Bot")
    print(f"  Mode: {TRADING_MODE}")
    print("  Price Feed: Alpaca latest quote")
    print("============================================================")

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

        current_price = get_current_price(
            symbol,
            trade["entry_price"]
        )

        stop = float(trade["stop_price"])
        target = float(trade["target_price"])
        entry = float(trade["entry_price"])
        shares = int(trade["shares"])

        print(
            f"[CHECK] {symbol} | Current={current_price} | "
            f"Entry={entry} | Stop={stop} | Target={target}"
        )

        exit_reason = None

        if current_price <= stop:
            exit_reason = "STOP_HIT"
        elif current_price >= target:
            exit_reason = "TARGET_HIT"

        if not exit_reason:
            continue

        pnl = round((current_price - entry) * shares, 2)

        updates.append({
            "id": trade["id"],
            "trade_status": "CLOSED",
            "exit_price": current_price,
            "pnl": pnl,
            "exit_reason": exit_reason,
            "closed_at": "now()"
        })

        print(
            f"{symbol} CLOSED | {exit_reason} | "
            f"Exit={current_price} | PnL=${pnl}"
        )

    for u in updates:
        trade_id = u.pop("id")
        supabase.table("bot_trades").update(u).eq("id", trade_id).execute()

    print(f"[DB] Updated {len(updates)} trades.")


def main():
    while True:
        try:
            run_risk_manager()
        except Exception as e:
            print("[ERROR]", str(e))

        print(f"[LOOP] Sleeping {RISK_INTERVAL}s...\n")
        time.sleep(RISK_INTERVAL)


if __name__ == "__main__":
    main()
