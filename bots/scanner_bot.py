import os
import time
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client, Client

from alpaca.data.historical.news import NewsClient

load_dotenv()

# ── ENV ────────────────────────────────────────────────────────────────────────
SCAN_INTERVAL       = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
FAST_WATCH_INTERVAL = int(os.getenv("FAST_WATCH_INTERVAL_SECONDS", "20"))
TRADING_MODE        = os.getenv("TRADING_MODE", "paper")

SUPABASE_URL             = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ALPACA_DATA_BASE_URL = os.getenv(
    "ALPACA_DATA_BASE_URL",
    "https://data.alpaca.markets"
).rstrip("/")

MOVERS_TOP = int(os.getenv("MOVERS_TOP", "50"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))

# ── HARD FILTER CONSTANTS (Warrior Trading) ───────────────────────────────────
MIN_PRICE          = 1.00
MAX_PRICE          = 20.00
MIN_PERCENT_CHANGE = 10.0
PREF_PERCENT_CHANGE = 20.0
MIN_VOLUME         = 1_000_000
WATCH_MIN_VOLUME   = 250_000
MIN_REL_VOLUME     = 5.0
MAX_SPREAD_PCT     = 1.5
MAX_FLOAT          = 20_000_000
MAX_SYMBOL_LENGTH  = 5

ET = ZoneInfo("America/New_York")

# ── CLIENTS ────────────────────────────────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
news_client = NewsClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


def is_tradeable_symbol(symbol: str) -> bool:
    if not symbol:
        return False
    if len(symbol) > MAX_SYMBOL_LENGTH:
        return False
    if symbol.endswith(("U", "W", "R")):
        return False
    if any(c in symbol for c in ("/", ".", "-", "+")):
        return False
    return True


def has_recent_news(symbol: str) -> bool:
    """Warrior Trading requirement: must have a news catalyst today"""
    try:
        start_time = datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0)
        news_response = news_client.get_news(symbols=symbol, start=start_time)
        news_list = news_response.get("news", []) if isinstance(news_response, dict) else news_response
        has_news = len(news_list) > 0
        if has_news:
            print(f"[NEWS] ✅ {symbol} has news")
        else:
            print(f"[NEWS] ❌ {symbol} has no news")
        return has_news
    except Exception as e:
        print(f"[NEWS ERROR] {symbol}: {e}")
        return False


def get_top_mover_symbols() -> list[str]:
    url = f"{ALPACA_DATA_BASE_URL}/v1beta1/screener/stocks/movers"
    try:
        resp = requests.get(
            url,
            headers=alpaca_headers(),
            params={"top": MOVERS_TOP},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"[MOVERS] Error {resp.status_code}")
            return []

        gainers = resp.json().get("gainers") or []
        symbols = [
            g["symbol"] for g in gainers
            if g.get("symbol") and is_tradeable_symbol(g["symbol"])
        ]
        symbols = sorted(set(symbols))
        print(f"[MOVERS] {len(symbols)} clean gainer symbols loaded.")
        return symbols
    except Exception as e:
        print(f"[MOVERS] Fetch error: {e}")
        return []


def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_snapshots(symbols: list[str]) -> dict:
    all_snaps = {}
    url = f"{ALPACA_DATA_BASE_URL}/v2/stocks/snapshots"
    for batch in chunk_list(symbols, BATCH_SIZE):
        try:
            resp = requests.get(
                url,
                headers=alpaca_headers(),
                params={"symbols": ",".join(batch)},
                timeout=30,
            )
            if resp.status_code != 200:
                continue
            all_snaps.update(resp.json() or {})
            time.sleep(0.25)
        except Exception as e:
            print(f"[SNAPSHOT] Batch error: {e}")
    return all_snaps


def get_relative_volume(symbol: str, current_volume: int) -> float | None:
    try:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=40)

        resp = requests.get(
            f"{ALPACA_DATA_BASE_URL}/v2/stocks/{symbol}/bars",
            headers=alpaca_headers(),
            params={
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 30,
                "feed": "iex",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None

        bars = resp.json().get("bars") or []
        if len(bars) < 5:
            return None

        avg_daily_volume = sum(b["v"] for b in bars) / len(bars)
        if avg_daily_volume == 0:
            return None

        now_et = datetime.now(timezone.utc).astimezone(ET)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_elapsed = max((now_et - market_open).total_seconds() / 60, 1)
        minutes_elapsed = min(minutes_elapsed, 390)

        expected_volume = avg_daily_volume * (minutes_elapsed / 390)
        rel_vol = current_volume / expected_volume
        return round(rel_vol, 2)
    except Exception as e:
        print(f"[REL_VOL] {symbol} error: {e}")
        return None


def classify_stock(symbol: str, snap: dict) -> dict | None:
    latest_trade = snap.get("latestTrade") or {}
    latest_quote = snap.get("latestQuote") or {}
    daily_bar    = snap.get("dailyBar") or {}
    prev_bar     = snap.get("prevDailyBar") or {}

    price      = latest_trade.get("p") or daily_bar.get("c")
    prev_close = prev_bar.get("c")
    volume     = int(daily_bar.get("v") or 0)
    bid        = latest_quote.get("bp")
    ask        = latest_quote.get("ap")

    if not price or not prev_close or prev_close == 0:
        return None

    percent_change = ((price - prev_close) / prev_close) * 100
    spread_pct = None
    if bid and ask and ask > bid:
        spread_pct = ((ask - bid) / price) * 100

    # Hard Warrior gates
    if not (MIN_PRICE <= price <= MAX_PRICE):
        return None
    if percent_change < MIN_PERCENT_CHANGE:
        return None
    if volume < WATCH_MIN_VOLUME:
        return None
    if spread_pct is not None and spread_pct > MAX_SPREAD_PCT:
        return None

    rel_vol = get_relative_volume(symbol, volume)
    if rel_vol is None or rel_vol < MIN_REL_VOLUME:
        return None

    # ── NEWS CHECK (Warrior catalyst requirement) ─────────────────────────────
    has_news = has_recent_news(symbol)

    scanner_tier = "A_SETUP" if has_news else "WATCH"

    return {
        "symbol": symbol,
        "price": round(price, 4),
        "percent_change": round(percent_change, 2),
        "volume": volume,
        "rel_vol": rel_vol,
        "spread_pct": round(spread_pct, 2) if spread_pct else None,
        "scanner_tier": scanner_tier,
        "trading_mode": TRADING_MODE,
        "created_at": datetime.now(ET).isoformat()
    }


def run_scanner():
    print("============================================================")
    print("  Trading War Room — SCANNER BOT (with News Catalyst)")
    print(f"  Mode: {TRADING_MODE} | Time: {datetime.now(ET).strftime('%H:%M ET')}")
    print("============================================================")

    symbols = get_top_mover_symbols()
    if not symbols:
        print("[SCANNER] No movers found.")
        return

    snapshots = get_snapshots(symbols)

    candidates = []
    for symbol in symbols:
        snap = snapshots.get(symbol)
        if not snap:
            continue
        result = classify_stock(symbol, snap)
        if result:
            candidates.append(result)

    if not candidates:
        print("[SCANNER] No candidates passed filters.")
        return

    print(f"[SCANNER] Found {len(candidates)} candidates (A_SETUP requires news)")

    # Upsert to bot_watchlist
    supabase.table("bot_watchlist").upsert(candidates, on_conflict="symbol").execute()
    print(f"[DB] Upserted {len(candidates)} watchlist rows.")


def main():
    print("🚀 Scanner Bot with News Catalyst started\n")
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[ERROR] {e}")

        print(f"[LOOP] Sleeping {SCAN_INTERVAL}s...\n")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
