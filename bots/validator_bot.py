import os
import time
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
TRADING_MODE = os.getenv("TRADING_MODE", "paper")
VALIDATOR_INTERVAL = int(os.getenv("VALIDATOR_INTERVAL", 30))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def validate_candidate(row):
    score = 0
    reasons = []

    percent_change = float(row.get("percent_change") or 0)
    rel_vol = float(row.get("rel_vol") or 0)
    volume = int(row.get("volume") or 0)
    spread = float(row.get("spread_pct") or 999)
    tier = row.get("scanner_tier")

    # Momentum
    if percent_change >= 20:
        score += 25
        reasons.append("strong % change")
    elif percent_change >= 10:
        score += 15
        reasons.append("ok % change")
    else:
        reasons.append("weak % change")

    # RVOL
    if rel_vol >= 5:
        score += 25
        reasons.append("strong rvol")
    else:
        reasons.append("weak rvol")

    # Volume
    if volume >= 1_000_000:
        score += 20
        reasons.append("strong volume")
    else:
        reasons.append("low volume")

    # Spread
    if spread <= 1.5:
        score += 20
        reasons.append("tight spread")
    else:
        reasons.append("wide spread")

    # Scanner bias
    if tier == "A_SETUP":
        score += 10
        reasons.append("A_SETUP")

    status = "VALIDATED" if score >= 75 else "REJECTED_BY_VALIDATOR"

    return {
        "watchlist_id": row["id"],
        "symbol": row["symbol"],
        "validator_status": status,
        "validator_score": score,
        "reason": " | ".join(reasons),
        "price": row.get("price"),
        "percent_change": percent_change,
        "volume": volume,
        "rel_vol": rel_vol,
        "spread_pct": spread,
        "scanner_tier": tier,
        "trading_mode": TRADING_MODE,
    }


def run_validator():
    print("============================================================")
    print("  Trading War Room — Validator Bot")
    print(f"  Mode: {TRADING_MODE}")
    print("============================================================")

    response = (
        supabase.table("bot_watchlist")
        .select("*")
        .in_("scanner_tier", ["A_SETUP", "WATCH"])
        .eq("trading_mode", TRADING_MODE)
        .order("created_at", desc=True)
        .limit(25)
        .execute()
    )

    rows = response.data

    if not rows:
        print("[VALIDATOR] No candidates found.")
        return

    results = [validate_candidate(r) for r in rows]

    print("───── VALIDATOR OUTPUT ─────────────────────────────")
    for r in results:
        print(f"{r['symbol']} | {r['validator_status']} | score={r['validator_score']} | {r['reason']}")
    print("────────────────────────────────────────────────────")

    supabase.table("bot_validations") \
        .upsert(results, on_conflict="watchlist_id") \
        .execute()

    print(f"[DB] Saved {len(results)} validation rows.")


def main():
    while True:
        try:
            run_validator()
        except Exception as e:
            print("[ERROR]", str(e))

        print(f"[LOOP] Sleeping {VALIDATOR_INTERVAL}s...\n")
        time.sleep(VALIDATOR_INTERVAL)


if __name__ == "__main__":
    main()
