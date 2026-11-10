"""
web_radar_push.py — watches the live snapshot JSON and POSTs it to the web server.

Recommended stack:
  1. python rust_probe.py
  2. python rust_snapshot.py
  3. python web_radar_server.py   (or uvicorn ...)
  4. python web_radar_push.py

Env / dma_config:
  SNAPSHOT_PATH   live JSON path (default %TEMP%/ReaderRadar-Rust-live.json)
  WEB_RADAR_URL   e.g. http://127.0.0.1:8080
  WEB_RADAR_KEY   push key from web_radar_config.json
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import dma_config

INTERVAL = 0.25  # seconds between mtime checks


def push(url: str, key: str, payload: bytes) -> bool:
    req = urllib.request.Request(
        f"{url}/push",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Push-Key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        print(f"[push] HTTP {e.code}: {e.reason}")
        return False
    except Exception as e:
        print(f"[push] {e}")
        return False


def main():
    snap = dma_config.snapshot_path()
    server = dma_config.web_radar_url()
    key = dma_config.web_radar_key()

    if not key or key == "PASTE_YOUR_PUSH_KEY_HERE":
        print("[push] WARNING: WEB_RADAR_KEY is empty or placeholder.")
        print("[push] Set WEB_RADAR_KEY to the push_key from web_radar_config.json")

    print(f"[push] Watching {snap}")
    print(f"[push] Server  {server}")
    last_mtime = 0.0
    consecutive_errors = 0

    while True:
        try:
            if not snap.exists():
                time.sleep(INTERVAL)
                continue
            mtime = snap.stat().st_mtime
            if mtime != last_mtime and mtime > 0:
                raw = snap.read_bytes()
                json.loads(raw)  # validate before sending
                if push(server, key, raw):
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
