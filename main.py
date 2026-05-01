import os
import time
import requests
from datetime import datetime, timezone
from supabase import create_client, Client

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "120"))
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ALPACA_DATA_BASE_URL = os.getenv(
    "ALPACA_DATA_BASE_URL",
    "https://data.alpaca.markets"
).rstrip("/")

MOVERS_TOP = int(os.getenv("MOVERS_TOP", "50"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


def get_top_mover_symbols():
    """
    Uses Alpaca's real top movers endpoint.
    This replaces brute-force scanning thousands of tickers.
    """

    url = f"{ALPACA_DATA_BASE_URL}/v1beta1/screener/stocks/movers"

    try:
        response = requests.get(
            url,
            headers=alpaca_headers(),
            params={"top": MOVERS_TOP},
            timeout=30,
        )

        if response.status_code != 200:
            print("Alpaca movers error:", response.status_code, response.text[:500])
            print("Movers URL used:", url)
            return []

        data = response.json() or {}

        gainers = data.get("gainers") or []
        symbols = []

        for item in gainers:
            symbol = item.get("symbol")

            if not symbol:
                continue

            # Avoid SPAC units/warrants/rights and odd tickers
            if symbol.endswith("U") or symbol.endswith("W") or symbol.endswith("R"):
                continue

            if "/" in symbol or "." in symbol or "-" in symbol:
                continue

            symbols.append(symbol)

        symbols = sorted(list(set(symbols)))

        print(f"Top mover gainers loaded: {len(symbols)} symbols")
        return symbols

    except Exception as e:
        print("Movers fetch error:", str(e))
        return []


def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_snapshots_for_symbols(symbols):
    all_snapshots = {}
    url = f"{ALPACA_DATA_BASE_URL}/v2/stocks/snapshots"

    for batch in chunk_list(symbols, BATCH_SIZE):
        try:
            response = requests.get(
                url,
                headers=alpaca_headers(),
                params={"symbols": ",".join(batch)},
                timeout=30,
            )

            if response.status_code != 200:
                print("Alpaca snapshot error:", response.status_code, response.text[:500])
                print("Snapshot URL used:", url)
                continue

            data = response.json() or {}
            all_snapshots.update(data)

            time.sleep(0.25)

        except Exception as e:
            print("Snapshot batch error:", str(e))

    return all_snapshots


def classify_stock(symbol, snap):
    latest_trade = snap.get("latestTrade") or {}
    latest_quote = snap.get("latestQuote") or {}
    daily_bar = snap.get("dailyBar") or {}
    prev_bar = snap.get("prevDailyBar") or {}

    price = latest_trade.get("p") or daily_bar.get("c")
    prev_close = prev_bar.get("c")
    volume = daily_bar.get("v", 0) or 0

    bid = latest_quote.get("bp")
    ask = latest_quote.get("ap")

    if not price or not prev_close:
        return None

    percent_change = ((price - prev_close) / prev_close) * 100

    spread_pct = None

    if bid and ask and ask > bid:
        spread = ask - bid
        spread_pct = (spread / price) * 100

    score = 0
    reasons = []

    # Strict documentation-aligned criteria:
    # price $1-$20, already moving, high volume, tradable spread.
    price_ok = 1 <= price <= 20
    momentum_ok = percent_change >= 10
    strong_momentum = percent_change >= 20
    volume_ok = volume >= 1_000_000
    watch_volume_ok = volume >= 250_000
    spread_ok = spread_pct is None or spread_pct <= 1.5

    if price_ok:
        score += 25
        reasons.append("price_1_20")
    else:
        reasons.append("price_outside_range")

    if strong_momentum:
        score += 40
        reasons.append("gap_20_plus")
    elif momentum_ok:
        score += 30
        reasons.append("gap_10_plus")
    else:
        reasons.append("below_10_percent_reject")

    if volume >= 5_000_000:
        score += 25
        reasons.append("very_high_volume")
    elif volume_ok:
        score += 20
        reasons.append("high_volume")
    elif watch_volume_ok:
        score += 10
        reasons.append("moderate_volume")
    else:
        reasons.append("low_volume_reject")

    if spread_pct is not None:
        if spread_pct <= 0.5:
            score += 10
            reasons.append("tight_spread")
        elif spread_pct <= 1.5:
            score += 5
            reasons.append("acceptable_spread")
        else:
            score -= 25
            reasons.append("wide_spread_reject")
    else:
        reasons.append("spread_unknown")

    passed_core_filter = price_ok and momentum_ok and volume_ok and spread_ok

    # A_SETUP = strict A-quality candidate.
    if passed_core_filter and score >= 75:
        scanner_tier = "A_SETUP"

    # WATCH = still strict, but not full A.
    elif (
        price_ok
        and momentum_ok
        and watch_volume_ok
        and spread_ok
        and score >= 60
    ):
        scanner_tier = "WATCH"

    else:
        scanner_tier = "REJECT"

    return {
        "symbol": symbol,
        "price": round(price, 4),
        "percent_change": round(percent_change, 2),
        "volume": int(volume),
        "score": score,
        "scanner_tier": scanner_tier,
        "reason": ",".join(reasons),
        "passed_core_filter": passed_core_filter,
        "trading_mode": TRADING_MODE,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def get_market_movers():
    symbols = get_top_mover_symbols()

    if not symbols:
        print("No mover symbols loaded.")
        return []

    snapshots = get_snapshots_for_symbols(symbols)

    results = []

    for symbol, snap in snapshots.items():
        classified = classify_stock(symbol, snap)

        if not classified:
            continue

        if classified["scanner_tier"] in ["A_SETUP", "WATCH"]:
            results.append(classified)

    results.sort(
        key=lambda x: (
            x["scanner_tier"] == "A_SETUP",
            x["score"],
            x["percent_change"],
            x["volume"],
        ),
        reverse=True,
    )

    return results[:25]


def save_watchlist(rows):
    if not rows:
        print("No scanner results.")
        return

    try:
        supabase.table("bot_watchlist").insert(rows).execute()
        print(f"Saved {len(rows)} rows to Supabase.")
    except Exception as e:
        print("Supabase insert error:", str(e))


def main():
    print("Trading War Room MOVERS scanner started.")
    print(f"Mode: {TRADING_MODE}")
    print(f"Scan interval: {SCAN_INTERVAL}s")
    print(f"Movers top: {MOVERS_TOP}")
    print(f"Data API: {ALPACA_DATA_BASE_URL}")

    while True:
        try:
            movers = get_market_movers()

            print("\n===== MOVERS SCANNER OUTPUT =====")

            if not movers:
                print("No A_SETUP or WATCH candidates found.")

            for m in movers:
                print(
                    f"{m['symbol']} | {m['scanner_tier']} | "
                    f"{m['percent_change']}% | ${m['price']} | "
                    f"vol:{m['volume']} | score:{m['score']} | {m['reason']}"
                )

            save_watchlist(movers)

        except Exception as e:
            print("Worker error:", str(e))

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
