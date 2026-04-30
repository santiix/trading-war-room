import os
import time
import requests
from datetime import datetime, timezone
from supabase import create_client, Client

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def get_market_movers():
    """
    MVP placeholder scanner.
    Later we will replace/expand this with Polygon, Alpaca snapshots,
    Benzinga/news, float data, and true RVOL.
    """

    url = "https://data.alpaca.markets/v2/stocks/snapshots"

    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

    # Starter watchlist for testing. Later this becomes dynamic universe scanning.
    symbols = ["AAPL", "TSLA", "NVDA", "AMD", "PLTR", "SOFI", "MARA", "RIOT"]

    response = requests.get(
        url,
        headers=headers,
        params={"symbols": ",".join(symbols)},
        timeout=20,
    )

    if response.status_code != 200:
        print("Alpaca error:", response.status_code, response.text)
        return []

    data = response.json()
    results = []

    for symbol, snap in data.items():
        latest_trade = snap.get("latestTrade") or {}
        daily_bar = snap.get("dailyBar") or {}
        prev_bar = snap.get("prevDailyBar") or {}

        price = latest_trade.get("p")
        open_price = daily_bar.get("o")
        prev_close = prev_bar.get("c")
        volume = daily_bar.get("v", 0)

        if not price or not prev_close:
            continue

        percent_change = ((price - prev_close) / prev_close) * 100

        score = 0

        # Strategy-inspired filters
        if 1 <= price <= 20:
            score += 25

        if percent_change >= 10:
            score += 30
        elif percent_change >= 5:
            score += 15

        if volume >= 1_000_000:
            score += 20
        elif volume >= 250_000:
            score += 10

        # Placeholder until we add real RVOL/news/float
        score += 5

        results.append({
            "symbol": symbol,
            "price": round(price, 4),
            "percent_change": round(percent_change, 2),
            "volume": volume,
            "score": score,
            "trading_mode": TRADING_MODE,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:10]


def save_watchlist(rows):
    if not rows:
        print("No scanner results.")
        return

    result = supabase.table("bot_watchlist").insert(rows).execute()
    print(f"Saved {len(rows)} rows to Supabase.")


def main():
    print("Trading War Room worker started.")
    print(f"Mode: {TRADING_MODE}")
    print(f"Scan interval: {SCAN_INTERVAL}s")

    while True:
        try:
            movers = get_market_movers()
            print("Top scanner results:", movers)
            save_watchlist(movers)
        except Exception as e:
            print("Worker error:", str(e))

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
