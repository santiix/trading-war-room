import os
import time
import requests
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client

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

# ── HARD FILTER CONSTANTS (from PDFs) ─────────────────────────────────────────
MIN_PRICE          = 1.00
MAX_PRICE          = 20.00
MIN_PERCENT_CHANGE = 10.0    # absolute minimum — 20%+ preferred
PREF_PERCENT_CHANGE = 20.0   # A_SETUP threshold
MIN_VOLUME         = 1_000_000
WATCH_MIN_VOLUME   = 250_000
MIN_REL_VOLUME     = 5.0     # 5x above average — hard gate per PDFs
MAX_SPREAD_PCT     = 1.5
MAX_FLOAT          = 20_000_000   # ideal; up to 50M considered
MAX_SYMBOL_LENGTH  = 5

# ── CLIENT ────────────────────────────────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


# ── SYMBOL VALIDATION ─────────────────────────────────────────────────────────
def is_tradeable_symbol(symbol: str) -> bool:
    """
    Reject warrants, units, rights, SPACs, foreign ordinaries.
    Keep clean common-stock tickers only.
    """
    if not symbol:
        return False
    if len(symbol) > MAX_SYMBOL_LENGTH:
        return False
    if symbol.endswith(("U", "W", "R")):
        return False
    if any(c in symbol for c in ("/", ".", "-", "+")):
        return False
    return True


# ── ALPACA: TOP MOVERS ────────────────────────────────────────────────────────
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
            print(f"[MOVERS] Error {resp.status_code}: {resp.text[:300]}")
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


# ── ALPACA: SNAPSHOTS ─────────────────────────────────────────────────────────
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
                print(f"[SNAPSHOT] Error {resp.status_code}: {resp.text[:300]}")
                continue
            all_snaps.update(resp.json() or {})
            time.sleep(0.25)
        except Exception as e:
            print(f"[SNAPSHOT] Batch error: {e}")

    return all_snaps


# ── ALPACA: RELATIVE VOLUME ───────────────────────────────────────────────────
def get_relative_volume(symbol: str, current_volume: int) -> float | None:
    """
    Calculate relative volume properly:
    Compare current volume vs expected volume at this time of day
    based on 30-day average daily volume.

    Formula:
        expected_volume = avg_daily_volume * (minutes_since_open / 390)
        rel_vol = current_volume / expected_volume
    """
    try:
        end   = datetime.now(timezone.utc).date()
        start = end - timedelta(days=40)  # ~30 trading days

        resp = requests.get(
            f"{ALPACA_DATA_BASE_URL}/v2/stocks/{symbol}/bars",
            headers=alpaca_headers(),
            params={
                "timeframe": "1Day",
                "start":     start.isoformat(),
                "end":       end.isoformat(),
                "limit":     30,
                "feed":      "iex",
            },
            timeout=15,
        )

        if resp.status_code != 200:
            return None

        bars = resp.json().get("bars") or []
        if len(bars) < 5:  # not enough history
            return None

        avg_daily_volume = sum(b["v"] for b in bars) / len(bars)
        if avg_daily_volume == 0:
            return None

        # Normalize: how much of the trading day has elapsed?
        now_et = datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=-4))  # EDT; use -5 for EST
        )
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_elapsed = max((now_et - market_open).total_seconds() / 60, 1)
        minutes_elapsed = min(minutes_elapsed, 390)  # cap at full day

        expected_volume = avg_daily_volume * (minutes_elapsed / 390)
        rel_vol = current_volume / expected_volume

        return round(rel_vol, 2)

    except Exception as e:
        print(f"[REL_VOL] {symbol} error: {e}")
        return None


# ── CORE CLASSIFIER ───────────────────────────────────────────────────────────
def classify_stock(symbol: str, snap: dict) -> dict | None:
    latest_trade = snap.get("latestTrade") or {}
    latest_quote = snap.get("latestQuote") or {}
    daily_bar    = snap.get("dailyBar")    or {}
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

    # ── HARD GATES — fail any one → REJECT immediately ────────────────────────
    # These are NOT negotiable per the Warrior Trading system.
    # Do not convert these into scoring adjustments.

    if not (MIN_PRICE <= price <= MAX_PRICE):
        return None  # outside tradeable price range

    if percent_change < MIN_PERCENT_CHANGE:
        return None  # not moving enough

    if volume < WATCH_MIN_VOLUME:
        return None  # no liquidity at all — skip entirely

    if spread_pct is not None and spread_pct > MAX_SPREAD_PCT:
        return None  # untradeable spread — hard reject

    # ── RELATIVE VOLUME (5x minimum — hard gate per PDFs) ────────────────────
    rel_vol = get_relative_volume(symbol, volume)
    rel_vol_ok = rel_vol is not None and rel_vol >= MIN_REL_VOLUME

    # ── TIER LOGIC ────────────────────────────────────────────────────────────
    #
    # A_SETUP:  ALL of the following must be true
    #   - price $1-$20            ✓ (already gated above)
    #   - % change >= 20%         (preferred threshold)
    #   - volume >= 1M            (confirmed liquid)
    #   - rel_vol >= 5x           (confirmed unusual activity)
    #   - spread <= 1.5%          ✓ (already gated above)
    #
    # WATCH:    Meets all hard gates but one soft criteria is marginal
    #   - % change 10-19%
    #   - OR volume 250K-999K
    #   - OR rel_vol unknown/below 5x
    #
    # Anything else was already rejected above.

    volume_ok        = volume >= MIN_VOLUME
    strong_momentum  = percent_change >= PREF_PERCENT_CHANGE

    is_a_setup = (
        strong_momentum
        and volume_ok
        and rel_vol_ok
    )

    is_watch = (
        not is_a_setup
        and (volume >= WATCH_MIN_VOLUME)
        and percent_change >= MIN_PERCENT_CHANGE
    )

    if is_a_setup:
        scanner_tier = "A_SETUP"
    elif is_watch:
        scanner_tier = "WATCH"
    else:
        return None  # doesn't qualify for either tier

    # ── BUILD REASON STRING ───────────────────────────────────────────────────
    reasons = []
    reasons.append(f"chg:{round(percent_change, 1)}%")
    reasons.append(f"vol:{volume:,}")
    if rel_vol is not None:
        reasons.append(f"rvol:{rel_vol}x")
    else:
        reasons.append("rvol:unknown")
    if spread_pct is not None:
        reasons.append(f"spread:{round(spread_pct, 2)}%")

    return {
        "symbol":           symbol,
        "price":            round(price, 4),
        "prev_close":       round(prev_close, 4),
        "percent_change":   round(percent_change, 2),
        "volume":           volume,
        "rel_vol":          rel_vol,
        "spread_pct":       round(spread_pct, 3) if spread_pct else None,
        "scanner_tier":     scanner_tier,
        "reason":           " | ".join(reasons),
        "passed_core_filter": is_a_setup,
        "trading_mode":     TRADING_MODE,
        "scanned_at":       datetime.now(timezone.utc).isoformat(),
    }


# ── MAIN SCANNER RUN ──────────────────────────────────────────────────────────
def get_market_movers() -> list[dict]:
    symbols = get_top_mover_symbols()
    if not symbols:
        print("[SCAN] No mover symbols loaded.")
        return []

    snapshots = get_snapshots(symbols)
    results   = []

    for symbol, snap in snapshots.items():
        if not is_tradeable_symbol(symbol):
            continue

        classified = classify_stock(symbol, snap)
        if classified:
            results.append(classified)

    # Sort: A_SETUP first, then by rel_vol desc, then % change desc
    results.sort(
        key=lambda x: (
            x["scanner_tier"] == "A_SETUP",
            x["rel_vol"] or 0,
            x["percent_change"],
        ),
        reverse=True,
    )

    # Only surface top 10 — focus on the obvious setups
    return results[:10]


# ── SUPABASE UPSERT ───────────────────────────────────────────────────────────
def save_watchlist(rows: list[dict]):
    """
    Upsert on symbol so we don't accumulate duplicates every scan cycle.
    Latest scan data wins for each symbol.
    Requires bot_watchlist to have `symbol` as a unique column.
    """
    if not rows:
        print("[DB] No rows to save.")
        return
    try:
        supabase.table("bot_watchlist").upsert(
            rows,
            on_conflict="symbol"
        ).execute()
        print(f"[DB] Upserted {len(rows)} rows.")
    except Exception as e:
        print(f"[DB] Upsert error: {e}")


# ── FAST WATCHER (A_SETUP only) ───────────────────────────────────────────────
def fast_watch_a_setups(a_setup_symbols: list[str]):
    """
    Runs a tighter loop on confirmed A_SETUP stocks only.
    Updates their latest price/volume in the watchlist every ~20s.
    This is the feed that your pattern bot (Bot #3) should consume.
    """
    if not a_setup_symbols:
        return

    snaps = get_snapshots(a_setup_symbols)
    updates = []

    for symbol, snap in snaps.items():
        classified = classify_stock(symbol, snap)
        if classified:
            updates.append(classified)

    if updates:
        save_watchlist(updates)
        print(f"[FAST_WATCH] Refreshed {len(updates)} A_SETUP symbols.")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Trading War Room — Scanner Bot")
    print(f"  Mode:          {TRADING_MODE}")
    print(f"  Scan interval: {SCAN_INTERVAL}s (slow loop)")
    print(f"  Fast interval: {FAST_WATCH_INTERVAL}s (A_SETUP watch)")
    print(f"  Hard gates:    price ${MIN_PRICE}-${MAX_PRICE} | "
          f"chg >={MIN_PERCENT_CHANGE}% | "
          f"vol >={MIN_VOLUME:,} | "
          f"rvol >={MIN_REL_VOLUME}x | "
          f"spread <={MAX_SPREAD_PCT}%")
    print("=" * 60)

    last_slow_scan = 0
    last_fast_scan = 0
    current_a_setups: list[str] = []

    while True:
        now = time.time()

        # ── SLOW LOOP: full scan every SCAN_INTERVAL seconds ──────────────────
        if now - last_slow_scan >= SCAN_INTERVAL:
            try:
                movers = get_market_movers()

                print("\n───── SCANNER OUTPUT ─────────────────────────────────")
                if not movers:
                    print("  No qualifying candidates this cycle.")
                for m in movers:
                    tier_tag = "🔥 A_SETUP" if m["scanner_tier"] == "A_SETUP" else "👀 WATCH"
                    print(
                        f"  {tier_tag} | {m['symbol']:<6} | "
                        f"{m['percent_change']:>6.1f}% | "
                        f"${m['price']:<7.2f} | "
                        f"vol:{m['volume']:>10,} | "
                        f"{m['reason']}"
                    )
                print("──────────────────────────────────────────────────────\n")

                save_watchlist(movers)

                # Update the fast-watch list with current A_SETUPs
                current_a_setups = [
                    m["symbol"] for m in movers
                    if m["scanner_tier"] == "A_SETUP"
                ]

            except Exception as e:
                print(f"[SLOW_LOOP] Error: {e}")

            last_slow_scan = now

        # ── FAST LOOP: re-check A_SETUPs every FAST_WATCH_INTERVAL seconds ───
        if current_a_setups and (now - last_fast_scan >= FAST_WATCH_INTERVAL):
            try:
                fast_watch_a_setups(current_a_setups)
            except Exception as e:
                print(f"[FAST_LOOP] Error: {e}")
            last_fast_scan = now

        time.sleep(5)  # tight outer loop — controls both timers


if __name__ == "__main__":
    main()
