import os
import time
import math
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
ENTRY_INTERVAL = int(os.getenv("ENTRY_INTERVAL", 30))

RISK_DOLLARS = float(os.getenv("RISK_DOLLARS", 50))
MAX_TRADE_DOLLARS = float(os.getenv("MAX_TRADE_DOLLARS", 1000))

STOP_PERCENT = float(os.getenv("STOP_PERCENT", 5)) / 100
MIN_VALIDATOR_SCORE = float(os.getenv("MIN_VALIDATOR_SCORE", 85))

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", 3))
ENTRY_WINDOW_START = os.getenv("ENTRY_WINDOW_START", "09:30")
ENTRY_WINDOW_END = os.getenv("ENTRY_WINDOW_END", "10:00")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ET = ZoneInfo("America/New_York")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

alpaca_data = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)


def parse_hhmm(value):
    h, m = value.split(":")
    return dt_time(int(h), int(m))


def is_entry_window_open():
    now = datetime.now(ET).time()
    start = parse_hhmm(ENTRY_WINDOW_START)
    end = parse_hhmm(ENTRY_WINDOW_END)

    return start <= now <= end


def is_trading_enabled():
    control = (
        supabase.table("bot_control")
        .select("*")
        .eq("trading_mode", TRADING_MODE)
        .limit(1)
        .execute()
    )

    if control.data and not control.data[0].get("is_enabled", True):
        reason = control.data[0].get("reason") or "UNKNOWN"
        status = control.data[0].get("status") or "HALTED"
        print(f"[ENTRY] Trading disabled by Overseer. Status={status} Reason={reason}")
        return False

    return True


def get_current_price(symbol, fallback_price):
    try:
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = alpaca_data.get_stock_latest_quote(request)
        quote = quotes.get(symbol)

        if not quote:
            return float(fallback_price or 0)

        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)

        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4)

        if ask > 0:
            return round(ask, 4)

        if bid > 0:
            return round(bid, 4)

    except Exception as e:
        print(f"[PRICE ERROR] {symbol}: {e}")

    return float(fallback_price or 0)


def get_intraday_bars(symbol):
    now = datetime.now(ET)
    start = now.replace(hour=4, minute=0, second=0, microsecond=0)

    try:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=now
        )

        bars = alpaca_data.get_stock_bars(request)
        symbol_bars = bars.data.get(symbol, [])

        return symbol_bars

    except Exception as e:
        print(f"[BARS ERROR] {symbol}: {e}")
        return []


def calculate_vwap(bars):
    total_volume = 0
    total_price_volume = 0

    for bar in bars:
        volume = float(bar.volume or 0)
        if volume <= 0:
            continue

        typical_price = (float(bar.high) + float(bar.low) + float(bar.close)) / 3
        total_price_volume += typical_price * volume
        total_volume += volume

    if total_volume <= 0:
        return None

    return round(total_price_volume / total_volume, 4)


def analyze_entry_setup(symbol, fallback_price):
    bars = get_intraday_bars(symbol)

    if len(bars) < 8:
        return {
            "approved": False,
            "setup": "NO_SETUP",
            "reason": "not enough intraday bars"
        }

    current_price = get_current_price(symbol, fallback_price)

    if current_price <= 0:
        return {
            "approved": False,
            "setup": "NO_SETUP",
            "reason": "invalid current price"
        }

    premarket_bars = []
    regular_bars = []

    for bar in bars:
        bar_time = bar.timestamp.astimezone(ET).time()

        if bar_time < dt_time(9, 30):
            premarket_bars.append(bar)
        else:
            regular_bars.append(bar)

    if not premarket_bars:
        return {
            "approved": False,
            "setup": "NO_SETUP",
            "reason": "no premarket bars available"
        }

    premarket_high = max(float(bar.high) for bar in premarket_bars)
    vwap = calculate_vwap(bars)

    if not vwap:
        return {
            "approved": False,
            "setup": "NO_SETUP",
            "reason": "could not calculate VWAP"
        }

    last_bars = bars[-5:]
    last_close = float(last_bars[-1].close)
    previous_close = float(last_bars[-2].close)

    recent_high = max(float(bar.high) for bar in last_bars)
    recent_low = min(float(bar.low) for bar in last_bars)

    is_above_vwap = current_price >= vwap
    near_premarket_high = current_price >= premarket_high * 0.97
    breaking_premarket_high = current_price >= premarket_high
    not_too_extended = current_price <= premarket_high * 1.08

    higher_low = float(last_bars[-1].low) >= float(last_bars[-3].low)
    reclaiming_strength = last_close > previous_close

    micro_pullback = (
        is_above_vwap
        and near_premarket_high
        and not_too_extended
        and higher_low
        and reclaiming_strength
    )

    breakout = (
        is_above_vwap
        and breaking_premarket_high
        and not_too_extended
    )

    if breakout:
        return {
            "approved": True,
            "setup": "PREMARKET_HIGH_BREAKOUT",
            "reason": f"price breaking premarket high | price={current_price} pm_high={round(premarket_high, 4)} vwap={vwap}",
            "current_price": current_price
        }

    if micro_pullback:
        return {
            "approved": True,
            "setup": "MICRO_PULLBACK",
            "reason": f"holding VWAP and reclaiming after pullback | price={current_price} recent_low={round(recent_low, 4)} vwap={vwap}",
            "current_price": current_price
        }

    return {
        "approved": False,
        "setup": "NO_SETUP",
        "reason": f"no clean breakout/pullback | price={current_price} pm_high={round(premarket_high, 4)} vwap={vwap}"
    }


def build_paper_trade(row):
    symbol = row["symbol"]

    setup = analyze_entry_setup(symbol, row.get("price"))

    if not setup["approved"]:
        print(f"[ENTRY BLOCKED] {symbol} | {setup['setup']} | {setup['reason']}")
        return None

    entry_price = float(setup["current_price"])

    stop_price = round(entry_price * (1 - STOP_PERCENT), 4)
    risk_per_share = round(entry_price - stop_price, 4)

    if risk_per_share <= 0:
        return None

    target_price = round(entry_price + (risk_per_share * 2), 4)
    reward_per_share = round(target_price - entry_price, 4)

    shares_by_risk = math.floor(RISK_DOLLARS / risk_per_share)
    shares_by_buying_power = math.floor(MAX_TRADE_DOLLARS / entry_price)

    shares = min(shares_by_risk, shares_by_buying_power)

    if shares <= 0:
        return None

    actual_risk = round(shares * risk_per_share, 2)

    return {
        "validation_id": row["id"],
        "watchlist_id": row.get("watchlist_id"),
        "symbol": symbol,

        "trade_status": "OPEN",
        "trade_type": "PAPER_LONG",

        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,

        "risk_per_share": risk_per_share,
        "reward_per_share": reward_per_share,
        "shares": shares,
        "risk_dollars": actual_risk,

        "validator_score": row.get("validator_score"),
        "entry_reason": f"{setup['setup']} | {setup['reason']} | validator={row.get('reason')}",

        "trading_mode": TRADING_MODE,
    }


def run_entry_bot():
    print("============================================================")
    print("  Trading War Room — Entry Bot")
    print(f"  Mode:          {TRADING_MODE}")
    print(f"  Window:        {ENTRY_WINDOW_START}-{ENTRY_WINDOW_END} ET")
    print(f"  Risk:          ${RISK_DOLLARS} max risk")
    print(f"  Buying Power:  ${MAX_TRADE_DOLLARS} max per paper trade")
    print(f"  Stop:          {STOP_PERCENT * 100}%")
    print(f"  Min Score:     {MIN_VALIDATOR_SCORE}")
    print("============================================================")

    if not is_trading_enabled():
        return

    if not is_entry_window_open():
        print("[ENTRY] Outside entry window. No new trades.")
        return

    open_trades = (
        supabase.table("bot_trades")
        .select("*")
        .eq("trade_status", "OPEN")
        .eq("trading_mode", TRADING_MODE)
        .execute()
    )

    if len(open_trades.data or []) >= MAX_OPEN_TRADES:
        print("[ENTRY] Max open trades reached.")
        return

    response = (
        supabase.table("bot_validations")
        .select("*")
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

    trades = []

    available_slots = MAX_OPEN_TRADES - len(open_trades.data or [])

    for row in rows:
        if len(trades) >= available_slots:
            break

        trade = build_paper_trade(row)

        if trade:
            trades.append(trade)

    if not trades:
        print("[ENTRY] No valid breakout/pullback entries.")
        return

    print("───── ENTRY OUTPUT ─────────────────────────────────")

    for t in trades:
        print(
            f"{t['symbol']} | ENTRY={t['entry_price']} | STOP={t['stop_price']} | "
            f"TARGET={t['target_price']} | SHARES={t['shares']} | RISK=${t['risk_dollars']}"
        )

    print("────────────────────────────────────────────────────")

    supabase.table("bot_trades").upsert(
        trades,
        on_conflict="validation_id"
    ).execute()

    print(f"[DB] Upserted {len(trades)} paper trade rows.")


def main():
    while True:
        try:
            run_entry_bot()
        except Exception as e:
            print("[ERROR]", str(e))

        print(f"[LOOP] Sleeping {ENTRY_INTERVAL}s...\n")
        time.sleep(ENTRY_INTERVAL)


if __name__ == "__main__":
    main()
