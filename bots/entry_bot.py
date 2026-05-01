import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce

load_dotenv()

# ====================== WARRIOR TRADING CONFIG ======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
ENTRY_INTERVAL = int(os.getenv("ENTRY_INTERVAL", 10))

# Warrior Small Account Rules
PER_TRADE_RISK_DOLLARS = float(os.getenv("PER_TRADE_RISK_DOLLARS", -50.0))   # Rule 1

# Safety flags
ENABLE_ALPACA_ORDERS = os.getenv("ENABLE_ALPACA_ORDERS", "false").lower() == "true"
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", 3))

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=(TRADING_MODE == "paper"))

ET = ZoneInfo("America/New_York")
# ====================================================================

def get_current_price(symbol):
    try:
        from alpaca.data.requests import StockLatestQuoteRequest
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = data_client.get_stock_latest_quote(req)
        quote = quotes.get(symbol)
        return round(float(quote.ask_price), 4) if quote and quote.ask_price else None
    except Exception as e:
        print(f"[PRICE ERROR] {symbol}: {e}")
        return None


def is_market_open():
    """Strict regular market hours only (Warrior day-trading style)"""
    now_et = datetime.now(ET).time()
    market_open = dt_time(9, 30)
    market_close = dt_time(16, 0)
    return market_open <= now_et < market_close


def trading_enabled():
    """Overseer / bot_control kill switch"""
    try:
        resp = supabase.table("bot_control").select("enabled").eq("bot", "entry").execute()
        return (resp.data and resp.data[0]["enabled"]) or True
    except:
        return True  # default to enabled if table doesn't exist yet


def count_open_trades():
    resp = supabase.table("bot_trades") \
        .select("id", count="exact") \
        .eq("trade_status", "OPEN") \
        .eq("trading_mode", TRADING_MODE) \
        .execute()
    return resp.count or 0


def run_entry_bot():
    print("============================================================")
    print("  Trading War Room — ENTRY BOT (100% Warrior Trading)")
    print(f"  Mode: {TRADING_MODE} | Risk/Trade: ${abs(PER_TRADE_RISK_DOLLARS)} | Alpaca Orders: {ENABLE_ALPACA_ORDERS}")
    print("============================================================")

    if not is_market_open():
        print("[ENTRY] Outside regular market hours — waiting...")
        return

    if not trading_enabled():
        print("[ENTRY] Overseer has disabled trading.")
        return

    if count_open_trades() >= MAX_OPEN_TRADES:
        print(f"[ENTRY] Max open trades ({MAX_OPEN_TRADES}) reached.")
        return

    # === READ ONLY VALIDATED SETUPS (respecting Validator Bot) ===
    response = (
        supabase.table("bot_validations")
        .select("*")
        .eq("scanner_tier", "A_SETUP")
        .eq("validated", True)
        .eq("trading_mode", TRADING_MODE)
        .is_("trade_id", None)          # not yet entered
        .order("validated_at", desc=True)
        .limit(5)
        .execute()
    )

    setups = response.data or []
    if not setups:
        print("[ENTRY] No validated A_SETUPs ready.")
        return

    for setup in setups:
        symbol = setup["symbol"]
        print(f"[ENTRY] Validated A_SETUP → {symbol}")

        current_price = get_current_price(symbol)
        if not current_price:
            continue

        # Warrior risk sizing
        stop_distance_pct = 0.05                     # typical for these momentum stocks
        stop_price = round(current_price * (1 - stop_distance_pct), 4)
        risk_per_share = current_price - stop_price

        if risk_per_share <= 0:
            continue

        shares = int(abs(PER_TRADE_RISK_DOLLARS) / risk_per_share)
        if shares < 1:
            print(f"⚠️  {symbol} too expensive for ${abs(PER_TRADE_RISK_DOLLARS)} risk")
            continue

        target_price = round(current_price + (2 * risk_per_share), 4)   # exact 2:1

        print(f"   → Entry ${current_price} | Stop ${stop_price} | Target ${target_price} | Shares {shares}")

        # === INSERT TRADE RECORD (Risk Manager will monitor) ===
        trade_record = {
            "symbol": symbol,
            "trade_status": "OPEN",
            "trading_mode": TRADING_MODE,
            "entry_price": current_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "shares": shares,
            "created_at": datetime.now(ET).isoformat()
        }

        result = supabase.table("bot_trades").insert(trade_record).execute()
        trade_id = result.data[0]["id"]

        # Link back to validation record
        supabase.table("bot_validations").update({"trade_id": trade_id}).eq("id", setup["id"]).execute()

        # === OPTIONAL: Real order (only if flag is enabled) ===
        if ENABLE_ALPACA_ORDERS:
            try:
                order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=shares,
                    side=OrderSide.BUY,
                    type=OrderType.MARKET,
                    time_in_force=TimeInForce.DAY
                )
                order = trading_client.submit_order(order_data)
                print(f"✅ ALPACA ORDER PLACED → {symbol} {shares} shares | ID: {order.id}")
            except Exception as e:
                print(f"❌ ORDER FAILED {symbol}: {e}")
                supabase.table("bot_trades").delete().eq("id", trade_id).execute()
                continue
        else:
            print(f"   📝 Trade record created (paper mode — Risk Manager will watch)")

        print(f"   ✅ {symbol} handed off to Risk Manager")


def main():
    print("🚀 Warrior Trading Entry Bot started — respects full 5-bot pipeline\n")
    while True:
        try:
            run_entry_bot()
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(ENTRY_INTERVAL)


if __name__ == "__main__":
    main()
