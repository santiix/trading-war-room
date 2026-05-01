import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

load_dotenv()

# ====================== WARRIOR TRADING CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
OVERSEER_INTERVAL = int(os.getenv("OVERSEER_INTERVAL", 30))

DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", -100.0))
MAX_CONSECUTIVE_LOSERS = int(os.getenv("MAX_CONSECUTIVE_LOSERS", 3))

# Market regime settings (Ross "be present" rule)
MIN_VIX_FOR_TRADING = float(os.getenv("MIN_VIX_FOR_TRADING", 15.0))   # only trade when VIX is decent

ET = ZoneInfo("America/New_York")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
alpaca_data = StockHistoricalDataClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY")
)
# ===================================================

def get_today_pnl():
    today = datetime.now(ET).date().isoformat()
    response = supabase.table("bot_trades") \
        .select("pnl") \
        .eq("trading_mode", TRADING_MODE) \
        .eq("trade_status", "CLOSED") \
        .gte("closed_at", today) \
        .execute()
    return sum(float(t.get("pnl", 0)) for t in (response.data or []))

def get_consecutive_losers():
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

def get_current_vix():
    """Simple market regime check using VIX"""
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols="VIX")
        quotes = alpaca_data.get_stock_latest_quote(req)
        quote = quotes.get("VIX")
        return float(quote.ask_price) if quote and quote.ask_price else 15.0
    except:
        return 15.0   # default safe

def update_control_status(enabled: bool, status: str, reason: str = None):
    supabase.table("bot_control").update({
        "is_enabled": enabled,
        "status": status,
        "reason": reason,
        "updated_at": datetime.now(ET).isoformat()
    }).eq("trading_mode", TRADING_MODE).execute()

def run_overseer():
    print("============================================================")
    print("  Trading War Room — OVERSEER BOT (100% Warrior Trading)")
    print(f"  Mode: {TRADING_MODE}")
    print("============================================================")

    today_pnl = get_today_pnl()
    consec_losers = get_consecutive_losers()
    vix = get_current_vix()

    print(f"[OVERSEER] PnL Today: ${today_pnl:.2f} | Consec Losers: {consec_losers} | VIX: {vix:.1f}")

    # === WARRIOR RULES ===
    if today_pnl <= DAILY_LOSS_LIMIT:
        update_control_status(False, "HALTED", "DAILY_LOSS_LIMIT_HIT")
        print("🚨 TRADING HALTED — Daily loss limit reached")
        return

    if consec_losers >= MAX_CONSECUTIVE_LOSERS:
        update_control_status(False, "HALTED", "MAX_CONSECUTIVE_LOSERS_HIT")
        print("🚨 TRADING HALTED — 3 consecutive losers")
        return

    # Market Regime Check (Ross "be present" rule)
    if vix < MIN_VIX_FOR_TRADING:
        update_control_status(False, "PAUSED", f"Low volatility (VIX {vix:.1f})")
        print(f"⏸️  OVERSEER PAUSED — Market too quiet (VIX {vix:.1f})")
        return

    # End-of-day flat
    now_et = datetime.now(ET).time()
    if now_et.hour >= 15 and now_et.minute >= 45:
        update_control_status(False, "HALTED", "END_OF_DAY_FLAT")
        print("🚨 End-of-day flat enforced")
        return

    # System healthy
    update_control_status(True, "ACTIVE", None)
    print("✅ OVERSEER: System ACTIVE — all bots cleared to trade")


def main():
    print("🚀 Warrior Trading Overseer Bot started (100% compliant)\n")
    while True:
        try:
            run_overseer()
        except Exception as e:
            print(f"[ERROR] {e}")

        print(f"[LOOP] Sleeping {OVERSEER_INTERVAL}s...\n")
        time.sleep(OVERSEER_INTERVAL)


if __name__ == "__main__":
    main()
