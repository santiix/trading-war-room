import os
import time
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
OVERSEER_INTERVAL = int(os.getenv("OVERSEER_INTERVAL", 30))

MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", -100))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", 3))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def run_overseer():
    print("============================================================")
    print("  Trading War Room — Overseer Bot")
    print(f"  Mode: {TRADING_MODE}")
    print("============================================================")

    trades = supabase.table("bot_trades") \
        .select("*") \
        .eq("trading_mode", TRADING_MODE) \
        .execute().data or []

    if not trades:
        print("[OVERSEER] No trades yet.")
        return

    # Daily PnL (simple version)
    total_pnl = sum([t.get("pnl") or 0 for t in trades if t["trade_status"] == "CLOSED"])

    # Consecutive losses
    closed = [t for t in trades if t["trade_status"] == "CLOSED"]
    closed.sort(key=lambda x: x["created_at"], reverse=True)

    consecutive_losses = 0
    for t in closed:
        if (t.get("pnl") or 0) < 0:
            consecutive_losses += 1
        else:
            break

    print(f"[OVERSEER] PnL=${total_pnl} | Consecutive losses={consecutive_losses}")

    disable = False
    reason = None

    if total_pnl <= MAX_DAILY_LOSS:
        disable = True
        reason = "MAX_DAILY_LOSS"

    elif consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        disable = True
        reason = "TOO_MANY_LOSSES"

    if disable:
        supabase.table("bot_control").update({
            "is_enabled": False,
            "status": "HALTED",
            "reason": reason
        }).eq("trading_mode", TRADING_MODE).execute()

        print(f"[OVERSEER] TRADING HALTED → {reason}")

    else:
        supabase.table("bot_control").update({
            "is_enabled": True,
            "status": "ACTIVE",
            "reason": None
        }).eq("trading_mode", TRADING_MODE).execute()

        print("[OVERSEER] Trading ACTIVE")


def main():
    while True:
        try:
            run_overseer()
        except Exception as e:
            print("[ERROR]", str(e))

        print(f"[LOOP] Sleeping {OVERSEER_INTERVAL}s...\n")
        time.sleep(OVERSEER_INTERVAL)


if __name__ == "__main__":
    main()
