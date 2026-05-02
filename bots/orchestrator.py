import os
import time
import subprocess
import signal
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# List of bots in the exact order we want them to start
BOTS = [
    ("Scanner",     "python bots/scanner_bot.py"),
    ("Validator",   "python bots/validator_bot.py"),
    ("Entry",       "python bots/entry_bot.py"),
    ("Risk Manager","python bots/risk_manager_bot.py"),
    ("Overseer",    "python bots/overseer_bot.py"),
]

processes = []

def start_bots():
    print(f"🚀 Starting Trading War Room at {datetime.now(ET).strftime('%H:%M ET')}")
    for name, cmd in BOTS:
        print(f"   Starting {name}...")
        p = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append((name, p))
        time.sleep(3)  # small delay between starting bots

def stop_bots():
    print(f"\n🛑 Stopping all bots at {datetime.now(ET).strftime('%H:%M ET')}")
    for name, p in processes:
        print(f"   Stopping {name}...")
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    print("✅ All bots stopped.")

def main():
    start_bots()

    # Run until 10:15 AM ET
    while True:
        now = datetime.now(ET)
        if now.hour == 10 and now.minute >= 15:
            stop_bots()
            break
        time.sleep(60)  # check every minute

    # Keep the orchestrator alive for a bit after shutdown
    time.sleep(300)
    print("Orchestrator finished for the day.")

if __name__ == "__main__":
    main()
