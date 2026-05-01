import os
import time
import math
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
ENTRY_INTERVAL = int(os.getenv("ENTRY_INTERVAL", 30))

RISK_DOLLARS = float(os.getenv("RISK_DOLLARS", 50))
MAX_TRADE_DOLLARS = float(os.getenv("MAX_TRADE_DOLLARS", 1000))

STOP_PERCENT = float(os.getenv("STOP_PERCENT", 5)) / 100
MIN_VALIDATOR_SCORE = float(os.getenv("MIN_VALIDATOR_SCORE", 75))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

control = supabase.table("bot_control") \
    .select("*") \
    .eq("trading_mode", TRADING_MODE) \
    .limit(1) \
    .execute()

if control.data and not control.data[0]["is_enabled"]:
    print("[ENTRY] Trading disabled by Overseer.")
    return

def build_paper_trade(row):
    entry_price = float(row.get("price") or 0)

    if entry_price <= 0:
        return None

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
        "symbol": row["symbol"],

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
        "entry_reason": row.get("reason"),

        "trading_mode": TRADING_MODE,
    }


def run_entry_bot():
    print("============================================================")
    print("  Trading War Room — Entry Bot")
    print(f"  Mode:          {TRADING_MODE}")
    print(f"  Risk:          ${RISK_DOLLARS} max risk")
    print(f"  Buying Power:  ${MAX_TRADE_DOLLARS} max per paper trade")
    print(f"  Stop:          {STOP_PERCENT * 100}%")
    print("============================================================")

    response = (
        supabase.table("bot_validations")
        .select("*")
        .eq("validator_status", "VALIDATED")
        .eq("trading_mode", TRADING_MODE)
        .gte("validator_score", MIN_VALIDATOR_SCORE)
        .order("created_at", desc=True)
        .limit(25)
        .execute()
    )

    rows = response.data or []

    if not rows:
        print("[ENTRY] No validated setups found.")
        return

    trades = []

    for row in rows:
        trade = build_paper_trade(row)

        if trade:
            trades.append(trade)

    if not trades:
        print("[ENTRY] No valid paper trades to create.")
        return

    print("───── ENTRY OUTPUT ─────────────────────────────────")

    for t in trades:
        print(
            f"{t['symbol']} | ENTRY={t['entry_price']} | STOP={t['stop_price']} | "
            f"TARGET={t['target_price']} | SHARES={t['shares']} | RISK=${t['risk_dollars']}"
        )

    print("────────────────────────────────────────────────────")

    result = (
        supabase.table("bot_trades")
        .upsert(trades, on_conflict="validation_id")
        .execute()
    )

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
