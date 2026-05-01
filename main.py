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

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def get_market_movers():
    url = "https://data.alpaca.markets/v2/stocks/snapshots"

    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

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
        prev_close = prev_bar.get("c")
        volume = daily_bar.get("v", 0)

        if not price or not prev_close:
            continue

        percent_change = ((price - prev_close) / prev_close) * 100

        score = 0
        reasons = []

        # --- CORE STRATEGY FILTER ---
        price_ok = 1 <= price <= 20
        momentum_ok = percent_change >= 10
        volume_ok = volume >= 1_000_000

        if price_ok:
            score += 25
            reasons.append("price_1_20")
        else:
            reasons.append("price_outside_range")

        if percent_change >= 10:
            score += 30
            reasons.append("strong_momentum")
        elif percent_change >= 5:
            score += 15
            reasons.append("moderate_momentum")
        else:
            reasons.append("weak_momentum")

        if volume >= 1_000_000:
            score += 20
            reasons.append("high_volume")
        elif volume >= 250_000:
            score += 10
            reasons.append("moderate_volume")
        else:
            reasons.append("low_volume")

        # --- CORE FILTER MATCH ---
        passed_core_filter = price_ok and momentum_ok and volume_ok

        # --- CLASSIFICATION ---
        if passed_core_filter:
            scanner_tier = "A_SETUP"
        elif score >= 50:
            scanner_tier = "WATCH"
        else:
            scanner_tier = "REJECT"

        results.append({
            "symbol": symbol,
            "price": round(price, 4),
            "percent_change": round(percent_change, 2),
            "volume": volume,
            "score": score,
            "scanner_tier": scanner_tier,
            "reason": ",".join(reasons),
            "passed_core_filter": passed_core_filter,
            "trading_mode": TRADING_MODE,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:10]


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
    print("Trading War Room worker started.")
    print(f"Mode: {TRADING_MODE}")
    print(f"Scan interval: {SCAN_INTERVAL}s")

    while True:
        try:
            movers = get_market_movers()

            print("\n===== SCANNER OUTPUT =====")
            for m in movers:
                print(
                    f"{m['symbol']} | {m['scanner_tier']} | "
                    f"{m['percent_change']}% | vol:{m['volume']} | score:{m['score']}"
                )

            save_watchlist(movers)

        except Exception as e:
            print("Worker error:", str(e))

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
