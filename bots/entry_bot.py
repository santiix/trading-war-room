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

# ====================== CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
ENTRY_INTERVAL = int(os.getenv("ENTRY_INTERVAL", 10))

RISK_DOLLARS = float(os.getenv("RISK_DOLLARS", 25))
MAX_TRADE_DOLLARS = float(os.getenv("MAX_TRADE_DOLLARS", 500))
STOP_PERCENT = float(os.getenv("STOP_PERCENT", 5)) / 100

MIN_VALIDATOR_SCORE = int(os.getenv("MIN_VALIDATOR_SCORE", 75))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", 3))

ENABLE_ALPACA_ORDERS = os.getenv("ENABLE_ALPACA_ORDERS", "false").lower() == "true"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ET = ZoneInfo("America/New_York")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

data_client = StockHistoricalDataClient(
    api_key=ALPACA_API_KEY,
    secret_key=ALPACA_SECRET_KEY
)

trading_client = TradingClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    paper=(TRADING_MODE == "paper")
)
# ===================================================


def is_entry_window_open():
    current = datetime.now(ET).time()
    start = dt_time(9, 30)
    end = dt_time(10, 0)
    return start <= current <= end


def trading_enabled():
    try:
        resp = (
            supabase.table("bot_control")
            .select("is_enabled,status,reason")
            .eq("trading_mode", TRADING_MODE)
            .limit(1)
            .execute()
        )

        if not resp.data:
            print("[ENTRY] No bot_control row found — allowing trading by default.")
            return True

        row = resp.data[0]
        enabled = row.get("is_enabled", True)

        if not enabled:
            print(f"[ENTRY] Trading disabled by overseer. Status={row.get('status')} Reason={row.get('reason')}")

        return enabled

    except Exception as e:
        print(f"[ENTRY] bot_control check failed — allowing trading. Error: {e}")
        return True


def count_open_trades():
    try:
        resp = (
            supabase.table("bot_trades")
            .select("id", count="exact")
            .eq("trade_status", "OPEN")
            .eq("trading_mode", TRADING_MODE)
            .execute()
        )
        return resp.count or 0
    except Exception as e:
        print(f"[ENTRY] Could not count open trades: {e}")
        return 0


def already_open_symbol(symbol):
    try:
        resp = (
            supabase.table("bot_trades")
            .select("id")
            .eq("symbol", symbol)
            .eq("trade_status", "OPEN")
            .eq("trading_mode", TRADING_MODE)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception as e:
        print(f"[ENTRY] Could not check open symbol {symbol}: {e}")
        return False


def get_current_price(symbol):
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = data_client.get_stock_latest_quote(req)
        quote = quotes.get(symbol)

        if quote and quote.ask_price:
            return round(float(quote.ask_price), 4)

        if quote and quote.bid_price:
            return round(float(quote.bid_price), 4)

        print(f"[PRICE] No quote price found for {symbol}")
        return None

    except Exception as e:
        print(f"[PRICE ERROR] {symbol}: {e}")
        return None


def is_first_new_high_candle(symbol):
    try:
        start = datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0)

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            limit=500
        )

        bars = data_client.get_stock_bars(request).data.get(symbol, [])

        if len(bars) < 15:
            return False

        pm_bars = [
            bar for bar in bars
            if bar.timestamp.astimezone(ET).time() < dt_time(9, 30)
        ]

        if not pm_bars:
            print(f"[PATTERN] {symbol} — no premarket bars found.")
            return False

        pm_high = max(float(bar.high) for bar in pm_bars)

        market_bars = [
            bar for bar in bars
            if bar.timestamp.astimezone(ET).time() >= dt_time(9, 30)
        ]

        if len(market_bars) < 2:
            return False

        recent_bars = market_bars[-15:]

        for i in range(1, len(recent_bars)):
            prev_high = float(recent_bars[i - 1].high)
            curr_high = float(recent_bars[i].high)
            curr_close = float(recent_bars[i].close)

            if curr_high > pm_high and curr_close > prev_high:
                return True

        return False

    except Exception as e:
        print(f"[PATTERN CHECK ERROR] {symbol}: {e}")
        return False


def build_trade(row):
    symbol = row["symbol"]
    watchlist_id = row.get("watchlist_id")

    if already_open_symbol(symbol):
        print(f"[ENTRY] {symbol} already has an open trade — skipping.")
        return None

    current_price = get_current_price(symbol)
    if not current_price:
        return None

    if is_first_new_high_candle(symbol):
        entry_reason = "FIRST_NEW_HIGH_CANDLE"
    else:
        entry_reason = "PREMARKET_HIGH_BREAKOUT_OR_PULLBACK"

    stop_price = round(current_price * (1 - STOP_PERCENT), 4)
    risk_per_share = round(current_price - stop_price, 4)

    if risk_per_share <= 0:
        print(f"[ENTRY] {symbol} invalid risk per share.")
        return None

    shares_by_risk = math.floor(RISK_DOLLARS / risk_per_share)
    shares_by_cap = math.floor(MAX_TRADE_DOLLARS / current_price)
    shares = min(shares_by_risk, shares_by_cap)

    if shares < 1:
        print(f"[ENTRY] {symbol} position too small — skipping.")
        return None

    target_price = round(current_price + (risk_per_share * 2), 4)

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
        "created_at": datetime.now(ET).isoformat(),
        "opened_at": datetime.now(ET).isoformat()
    }


def place_alpaca_order(trade):
    if not ENABLE_ALPACA_ORDERS:
        print(f"[ENTRY] Alpaca orders disabled. DB paper trade only for {trade['symbol']}.")
        return None

    try:
        order_data = MarketOrderRequest(
            symbol=trade["symbol"],
            qty=trade["shares"],
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY
        )

        order = trading_client.submit_order(order_data)
        order_id = getattr(order, "id", None)

        print(f"✅ ALPACA PAPER ORDER PLACED → {trade['symbol']} {trade['shares']} shares | Order ID: {order_id}")
        return str(order_id) if order_id else None

    except Exception as e:
        print(f"❌ ALPACA ORDER FAILED {trade['symbol']}: {e}")
        return None


def get_validated_setups():
    try:
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

        # Your schema does NOT currently have bot_validations.trade_id.
        # So we filter duplicates by checking bot_trades.validation_id instead.
        clean_rows = []

        for row in rows:
            validation_id = row.get("id")

            existing = (
                supabase.table("bot_trades")
                .select("id")
                .eq("validation_id", validation_id)
                .limit(1)
                .execute()
            )

            if existing.data:
                print(f"[ENTRY] Validation {validation_id} already has a trade — skipping.")
                continue

            clean_rows.append(row)

        return clean_rows

    except Exception as e:
        print(f"[ENTRY] Failed to load validated setups: {e}")
        return []


def run_entry_bot():
    print("============================================================")
    print("  Trading War Room — ENTRY BOT")
    print(f"  Mode: {TRADING_MODE} | Window: 09:30-10:00 ET | Risk: ${RISK_DOLLARS}")
    print("============================================================")

    if not is_entry_window_open():
        print("[ENTRY] Outside 9:30-10:00 ET window.")
        return

    if not trading_enabled():
        return

    open_count = count_open_trades()
    if open_count >= MAX_OPEN_TRADES:
        print(f"[ENTRY] Max open trades reached: {open_count}/{MAX_OPEN_TRADES}")
        return

    rows = get_validated_setups()

    if not rows:
        print("[ENTRY] No validated setups ready.")
        return

    slots_remaining = MAX_OPEN_TRADES - open_count

    for row in rows[:slots_remaining]:
        trade = build_trade(row)

        if not trade:
            continue

        alpaca_order_id = place_alpaca_order(trade)

        if alpaca_order_id:
            trade["alpaca_order_id"] = alpaca_order_id

        try:
            result = supabase.table("bot_trades").insert(trade).execute()

            if result.data:
                print(
                    f"[ENTRY] ✅ TRADE OPENED: {trade['symbol']} | "
                    f"Entry={trade['entry_price']} | Shares={trade['shares']} | "
                    f"Risk=${trade['risk_dollars']} | Target={trade['target_price']}"
                )
            else:
                print(f"[ENTRY] Insert returned no data for {trade['symbol']}")

        except Exception as e:
            print(f"[ENTRY] DB insert failed for {trade['symbol']}: {e}")


def main():
    print("🚀 Entry Bot started — Monday Paper Ready\n")

    while True:
        try:
            run_entry_bot()
        except Exception as e:
            print(f"[ENTRY LOOP ERROR] {e}")

        print(f"[LOOP] Sleeping {ENTRY_INTERVAL}s...\n")
        time.sleep(ENTRY_INTERVAL)


if __name__ == "__main__":
    main()
