import os
import time
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
TRADING_MODE = os.getenv("TRADING_MODE", "paper")
RISK_INTERVAL = int(os.getenv("RISK_INTERVAL", 15))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_current_price(symbol, fallback_price):
    # TEMP: using last known price
    # later replace with real API (Polygon/Alpaca)
    return float(fallback_price or 0)


def run_risk_manager():
    print("============================================================")
    print("  Trading War Room — Risk Manager Bot")
    print(f"  Mode: {TRADING_MODE}")
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
        current_price = get_current_price(
            trade["symbol"], trade["entry_price"]
        )

        stop = float(trade["stop_price"])
        target = float(trade["target_price"])
        entry = float(trade["entry_price"])
        shares = int(trade["shares"])

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
            "exit_reason": exit_reason
        })

        print(
            f"{trade['symbol']} CLOSED | {exit_reason} | "
            f"Exit={current_price} | PnL=${pnl}"
        )

    for u in updates:
        supabase.table("bot_trades").update(u).eq("id", u["id"]).execute()

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
