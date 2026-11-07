"""
web_radar_push.py  —  watches the live snapshot JSON and pushes it to the web server.

Run alongside skeld_snapshot.py:
  python web_radar_push.py

Configure WEB_SERVER and PUSH_KEY below, or set env vars:
  WEB_RADAR_URL   e.g. http://your-vps:8080
  WEB_RADAR_KEY   push key from web_radar_config.json
"""

import json
import os
import time
import urllib.request
from pathlib import Path

SNAP_FILE  = Path(os.environ.get("TEMP", "/tmp")) / "ReaderRadar-Skeld-live.json"
WEB_SERVER = os.environ.get("WEB_RADAR_URL", "http://localhost:8080")
PUSH_KEY   = os.environ.get("WEB_RADAR_KEY", "PASTE_YOUR_PUSH_KEY_HERE")
INTERVAL   = 1.0   # seconds between pushes

def push(payload: bytes) -> bool:
    req = urllib.request.Request(
        f"{WEB_SERVER}/push",
        data=payload,
        headers={"Content-Type": "application/json",
                 "X-Push-Key": PUSH_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[push] {e}")
        return False

def main():
    print(f"[push] Watching {SNAP_FILE}")
    print(f"[push] Server  {WEB_SERVER}")
    last_mtime = 0.0
    consecutive_errors = 0

    while True:
        try:
            mtime = SNAP_FILE.stat().st_mtime if SNAP_FILE.exists() else 0.0
            if mtime != last_mtime and mtime > 0:
                raw = SNAP_FILE.read_bytes()
                json.loads(raw)         # validate before sending
                if push(raw):
                    last_mtime = mtime
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors % 10 == 1:
                print(f"[push] error: {e}")
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
