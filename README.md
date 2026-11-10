# ReaderRadar — Rust DMA Radar Framework

A DMA-based, read-only radar overlay for Rust (private server). Browser-based — no client download required for viewers.

## Architecture

```
Gaming PC (Rust running)
      ↕ PCIe DMA (read-only, Captain DMA / FTDI FT601)
Reader Laptop
  ├── rust_probe.py       → live MetadataRegistration scan → dump_fields.json
  ├── rust_snapshot.py    → walks entities ~10 Hz → %TEMP%/ReaderRadar-Rust-live.json
  ├── web_radar_push.py   → watches JSON → POST /push
  └── web_radar_server.py (FastAPI :8080)
            ↕ WebSocket
  ┌──────────────────────────────────────┐
  │  Browsers (Tailscale / LAN)          │
  │  /radar   — full interactive map     │
  │  /overlay — compact ESP float window │
  │  /admin   — user management          │
  └──────────────────────────────────────┘
```

## Quick start (reader laptop)

```powershell
# Optional — point at your MemProcFS / PCILeech folder
$env:MEMPROCFS_DIR = "C:\path\to\MemProcFS"
$env:RUST_PROCESS  = "RustClient.exe"

python rust_probe.py
python rust_snapshot.py

# In another terminal — after first server run creates web_radar_config.json
$env:WEB_RADAR_KEY = "<push_key from web_radar_config.json>"
python web_radar_server.py
python web_radar_push.py
```

Open `http://<reader-ip>:8080/radar` (login required).

## Configuration (`dma_config.py`)

| Env var | Default | Purpose |
|---------|---------|--------|
| `VMM_DLL_PATH` | (search) | Full path to `vmm.dll` |
| `MEMPROCFS_DIR` | (search) | Directory containing `vmm.dll` |
| `VMM_DEVICE` | `fpga` | MemProcFS `-device` value |
| `RUST_PROCESS` | `RustClient.exe` | Target process name |
| `SNAPSHOT_PATH` | `%TEMP%\ReaderRadar-Rust-live.json` | Live entity JSON |
| `WEB_RADAR_URL` | `http://localhost:8080` | FastAPI base URL |
| `WEB_RADAR_KEY` | (empty) | `X-Push-Key` for `/push` |
| `RUST_WORLD_SIZE` | `3500` | Server map size in metres |

If neither `VMM_DLL_PATH` nor `MEMPROCFS_DIR` is set, common Download/Tools paths are tried.

## Files

| File | Purpose |
|------|--------|
| `dma_config.py` | Shared env-based config for probe / snapshot / push |
| `rust_probe.py` | Live IL2CPP MetadataRegistration scan → `dump_fields.json` |
| `rust_snapshot.py` | Entity walk + live JSON writer (`--local-server` for debug UI) |
| `web_radar_server.py` | FastAPI — auth, WebSocket broadcast, push endpoint |
| `web_radar_push.py` | Watches snapshot JSON, POSTs to server |
| `web_static/radar.html` | Full interactive radar |
| `web_static/overlay.html` | Compact ESP overlay |
| `web_static/login.html` | Login page |
| `web_static/admin.html` | User management |
| `rust_offsets_schema.json` | Offset template (future dump.cs path) |
| `skeld_*.py` / `radar_desktop.py` | Among Us PoC reference |
| `requirements_web.txt` | Python deps for web server |

## Radar features

- Interactive map — pan, zoom, click to inspect
- Entity labels, loot filters, locked-crate timers
- Optional ESP lines and compact `/overlay` window

## Overlay (`/overlay`)

- Compact entity list sorted by distance
- Draggable, opacity slider, `H` to hide/show
- Example: `start chrome --app="http://<ip>:8080/overlay" --window-size=330,640 --always-on-top`

## Offset discovery

1. Run `rust_probe.py` while the game is running (DMA attached).
2. It walks MetadataRegistration, locates `clientEntities`, resolves field offsets, writes `dump_fields.json`.
3. `rust_snapshot.py` loads that file and starts the live loop.
4. Validate on a private Freeplay session (health, ammo, flags, etc.).

Offline `dump.cs` / Il2CppDumper support remains a planned path via `rust_offsets_schema.json`.

## Security constraints

- **Read-only** — no memory writes, no calls into the game process
- **Private server only** — no public match use
- No firmware / driver / Defender / BIOS changes
- No aimbot, chams, recoil reduction, or any write-based feature

## Snapshot output schema

`rust_snapshot.py` writes this JSON every ~100 ms (10 Hz):

```jsonc
{
  "ts":           1700000000.0,     // Unix timestamp
  "world_size":   3500,             // from RUST_WORLD_SIZE env
  "local_player": {                 // null when not detected
    "position": {"x": 0, "y": 0, "z": 0},
    "health": 85.5
  },
  "entities": [
    {
      "category": "player",         // see entity categories below
      "position": {"x": 100, "y": 5, "z": 200},
      "meta": {
        "name":          "Wolfpack",
        "health":        72.0,
        "held":          "rifle.ak",   // null when nothing held / unknown
        "ammo_contents": 0,            // best-effort; 0 when unresolved
        "ammo_capacity": 0,
        "alive":         true
      }
    }
  ]
}
```

**Entity categories** (`category` field): `player`, `player-sleeping`, `player-npc`,
`animal`, `animal-bear`, `animal-wolf`, `ore-stone`, `ore-metal`, `ore-sulfur`, `ore-hqm`,
`loot-basic`, `loot-military`, `loot-elite`, `loot-barrel`, `supply-drop`, `vehicle`,
`bradley`, `helicopter`, `pickup`, `hemp`, `berry`, `food`, `base`, `dropped`,
`locked-crate`, `locked-crate-ent`.

## TODO

- [ ] Ammo reading (requires `BaseProjectile.primaryMagazine` offset from probe)
- [ ] Inventory slot reading (belt, wear, main) — offsets not yet probed
- [ ] Optional offline `dump.cs` / Il2CppDumper parser path
- [ ] Populate and version `rust_offsets_schema.json` per game build
- [ ] Validation session checklist on private server
