import os
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# ====================== WARRIOR TRADING CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
OVERSEER_INTERVAL = int(os.getenv("OVERSEER_INTERVAL", 30))

# Ross Cameron Small Account Rules
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", -100.0))
MAX_CONSECUTIVE_LOSERS = int(os.getenv("MAX_CONSECUTIVE_LOSERS", 3))

# End-of-day flat (Ross is a strict day trader)
END_OF_DAY_HOUR = 15
END_OF_DAY_MINUTE = 45

ET = ZoneInfo("America/New_York")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
# ===================================================

def get_today_pnl():
    """True daily realized PnL (Warrior Rule 2)"""
    today = datetime.now(ET).date().isoformat()
    response = supabase.table("bot_trades") \
        .select("pnl") \
        .eq("trading_mode", TRADING_MODE) \
        .eq("trade_status", "CLOSED") \
        .gte("closed_at", today) \
        .execute()
    return sum(float(t.get("pnl", 0)) for t in (response.data or []))


def get_consecutive_losers():
    """Exact 3-loser rule (Warrior Rule 3)"""
    response = supabase.table("bot_trades") \
        .select("pnl") \
        .eq("trading_mode", TRADING_MODE) \
        .eq("trade_status", "CLOSED") \
        .order("closed_at", desc=True) \
        .limit(10) \
        .execute()
    streak = 0
    for trade in (response.data or []):
        if float(trade.get("pnl", 0)) < 0:
            streak += 1
        else:
            break
    return streak


def update_control_status(enabled: bool, status: str, reason: str = None):
    """Master control for the entire 5-bot system"""
    supabase.table("bot_control").update({
        "is_enabled": enabled,
        "status": status,
        "reason": reason,
        "updated_at": datetime.now(ET).isoformat()
    }).eq("trading_mode", TRADING_MODE).execute()


def run_overseer():
    print("============================================================")
    print("  Trading War Room — OVERSEER BOT (100% Warrior Trading)")
    print(f"  Mode: {TRADING_MODE} | Daily Loss Limit: ${abs(DAILY_LOSS_LIMIT)}")
    print("============================================================")

    today_pnl = get_today_pnl()
    consec_losers = get_consecutive_losers()

    print(f"[OVERSEER] Today's PnL: ${today_pnl:.2f} | Consecutive Losers: {consec_losers}")

    # === WARRIOR RULES ENFORCEMENT ===
    if today_pnl <= DAILY_LOSS_LIMIT:
        update_control_status(False, "HALTED", "DAILY_LOSS_LIMIT_HIT")
        print("🚨 OVERSEER: TRADING HALTED — Daily loss limit reached")
        return

    if consec_losers >= MAX_CONSECUTIVE_LOSERS:
        update_control_status(False, "HALTED", "MAX_CONSECUTIVE_LOSERS_HIT")
        print("🚨 OVERSEER: TRADING HALTED — 3 consecutive losers")
        return

    # End-of-day flat (Ross never holds overnight)
    now_et = datetime.now(ET).time()
    if now_et.hour >= END_OF_DAY_HOUR and now_et.minute >= END_OF_DAY_MINUTE:
        update_control_status(False, "HALTED", "END_OF_DAY_FLAT")
        print("🚨 OVERSEER: End-of-day flat enforced")
        return

    # System is healthy
    update_control_status(True, "ACTIVE", None)
    print("✅ OVERSEER: System ACTIVE — all bots cleared to trade")


def main():
    print("🚀 Warrior Trading Overseer Bot started (master brain)\n")
    while True:
        try:
            run_overseer()
        except Exception as e:
            print(f"[ERROR] {e}")

        print(f"[LOOP] Sleeping {OVERSEER_INTERVAL}s...\n")
        time.sleep(OVERSEER_INTERVAL)


if __name__ == "__main__":
    main()
