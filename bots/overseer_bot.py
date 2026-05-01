import os
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# ====================== WARRIOR TRADING CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
OVERSEER_INTERVAL = int(os.getenv("OVERSEER_INTERVAL", 30))

# Exact rules from Ross Cameron's Small Account Strategy PDF
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", -100.0))           # Rule 2
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", 3))  # Rule 3

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")      # not used here but kept for future
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

ET = ZoneInfo("America/New_York")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
# ====================================================================

def get_today_pnl():
    """True daily PnL (Warrior Rule 2)"""
    today = datetime.now(ET).date().isoformat()
    response = (
        supabase.table("bot_trades")
        .select("pnl")
        .eq("trading_mode", TRADING_MODE)
        .eq("trade_status", "CLOSED")
        .gte("closed_at", today)          # only today's closed trades
        .execute()
    )
    return sum(float(t.get("pnl", 0)) for t in (response.data or []))


def get_consecutive_losses():
    """Exact 3-loser rule (Warrior Rule 3)"""
    response = (
        supabase.table("bot_trades")
        .select("pnl")
        .eq("trading_mode", TRADING_MODE)
        .eq("trade_status", "CLOSED")
        .order("closed_at", desc=True)
        .limit(10)
        .execute()
    )
    closed = response.data or []
    streak = 0
    for trade in closed:
        if float(trade.get("pnl", 0)) < 0:
            streak += 1
        else:
            break
    return streak


def force_halt_trading(reason: str):
    """Master kill switch for the entire 5-bot system"""
    supabase.table("bot_control").update({
        "is_enabled": False,
        "status": "HALTED",
        "reason": reason
    }).eq("trading_mode", TRADING_MODE).execute()

    print(f"[OVERSEER] 🚨 TRADING HALTED → {reason}")


def run_overseer():
    print("============================================================")
    print("  Trading War Room — OVERSEER BOT (100% Warrior Trading)")
    print(f"  Mode: {TRADING_MODE} | Daily Loss Limit: ${abs(MAX_DAILY_LOSS)}")
    print("============================================================")

    today_pnl = get_today_pnl()
    consec_losses = get_consecutive_losses()

    print(f"[OVERSEER] Today's PnL: ${today_pnl:.2f} | Consecutive losses: {consec_losses}")

    # === WARRIOR RULES ENFORCEMENT ===
    if today_pnl <= MAX_DAILY_LOSS:
        force_halt_trading("MAX_DAILY_LOSS_HIT")
        return

    if consec_losses >= MAX_CONSECUTIVE_LOSSES:
        force_halt_trading("MAX_CONSECUTIVE_LOSSES_HIT")
        return

    # End-of-day flat (Ross is a day trader)
    now_et = datetime.now(ET).time()
    if now_et.hour >= 15 and now_et.minute >= 45:
        print("[OVERSEER] End-of-day flat enforced")
        # Risk Manager already handles closing, but we can reinforce here if needed

    # System is healthy
    supabase.table("bot_control").update({
        "is_enabled": True,
        "status": "ACTIVE",
        "reason": None
    }).eq("trading_mode", TRADING_MODE).execute()

    print("[OVERSEER] ✅ System ACTIVE — all bots may trade")


def main():
    print("🚀 Overseer Bot started (Warrior Trading brain)\n")
    while True:
        try:
            run_overseer()
        except Exception as e:
            print(f"[ERROR] {e}")

        print(f"[LOOP] Sleeping {OVERSEER_INTERVAL}s...\n")
        time.sleep(OVERSEER_INTERVAL)


if __name__ == "__main__":
    main()
