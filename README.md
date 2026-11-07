# ReaderRadar — Rust DMA Radar Framework

A DMA-based, read-only radar overlay for Rust (private server). Browser-based — no client download required for viewers.

## Architecture

```
Gaming PC (Rust running)
      ↕ PCIe DMA (read-only, Captain DMA / FTDI FT601)
Reader Laptop
  ├── dma_probe.py       → parses dump.cs + pattern-scans statics → rust_offsets_schema.json
  ├── skeld_snapshot.py  → reads entities via MemProcFS every ~100ms → writes JSON to %TEMP%
  ├── web_radar_push.py  → watches JSON file → POSTs to web server
  └── web_radar_server.py (FastAPI :8080)
            ↕ WebSocket
  ┌──────────────────────────────────────┐
  │  Browsers (Tailscale / LAN)          │
  │  /radar   — full interactive map     │
  │  /overlay — compact ESP float window │
  │  /admin   — user management          │
  └──────────────────────────────────────┘
```

## Files

| File | Purpose |
|------|---------|
| `web_radar_server.py` | FastAPI server — auth, WebSocket broadcast, push endpoint |
| `web_radar_push.py` | Watches snapshot JSON, POSTs to server |
| `web_static/radar.html` | Full interactive radar (AtomicDMA-style layout) |
| `web_static/overlay.html` | Compact ESP overlay for gaming PC browser |
| `web_static/login.html` | Login page |
| `web_static/admin.html` | User management (add/revoke/reset-pw) |
| `rust_offsets_schema.json` | Offset template — null values filled by dma_probe.py |
| `skeld_probe.py` | Among Us proof-of-concept probe (reference) |
| `skeld_snapshot.py` | Among Us proof-of-concept reader (reference) |
| `radar_desktop.py` | Among Us desktop overlay (reference) |
| `Start-Skeld.ps1` | One-shot launcher (probe → snapshot → web stack → browser) |
| `requirements_web.txt` | Python deps for web server |

## Radar Features

- **Interactive map** — pan, zoom, click to inspect, double-click to zoom to entity
- **Entity labels** — `Name (HP) DISTm` / `Weapon 28/30 (ammo type)`
- **Loot Filter** — per-category toggles with live counts
- **Dropped Items** — per-item mark list with magenta glow on canvas
- **Locked crate timers** — MM:SS countdown, red urgent under 2 min, green when open
- **ESP lines** — optional lines from local player to entities
- **ESP Widget** — draggable mini-map overlay
- **Click-to-inspect** — HP bar, ammo progress bar, inventory grid

## Overlay Features (`/overlay`)

- Compact entity list sorted by distance
- Ammo count color-coded (green → orange → red when low)
- Locked crate timers with progress bar
- Draggable, opacity slider, `H` key to hide/show
- Launch on gaming PC: `start chrome --app="http://<ip>:8080/overlay" --window-size=330,640 --always-on-top`

## Offset Discovery Workflow

1. **Offline** — Run [Il2CppDumper](https://github.com/Perfare/Il2CppDumper) on `GameAssembly.dll` + `global-metadata.dat` → produces `dump.cs`
2. **Automated** — `dma_probe.py` parses `dump.cs` for field offsets, pattern-scans for entity list static → populates `rust_offsets_schema.json`
3. **Manual validation** (~16 min, one Freeplay session) — take damage (health), fire weapon (ammo), mine ores (resource_type), etc.

### Validation checklist
- [ ] `health` — take damage
- [ ] `ammo_contents/capacity` — fire rounds
- [ ] `ammo_type` — load HV ammo
- [ ] `held_entity` shortname — swap 3 weapons
- [ ] `flags` sleeping bit — `/sleep` console command
- [ ] `hackSeconds` — start hacking a locked crate
- [ ] Ore `resource_type` enum — hit stone, metal, sulfur nodes
- [ ] Loot `container_type` enum — open barrel, basic, military crate
- [ ] Inventory chain — put known items in belt/main/wear
- [ ] Animal `sub_type` — find bear and wolf
- [ ] `vehicle.occupied` — get in/out of car

## Snapshot Format

```json
{
  "ts": 1234567890,
  "world_size": 3500,
  "local_player": {
    "position": {"x": 100.0, "y": 5.0, "z": 200.0},
    "health": 85.0
  },
  "entities": [
    {
      "category": "player",
      "position": {"x": 150.0, "y": 5.0, "z": 220.0},
      "meta": {
        "name": "PlayerName",
        "health": 72.0,
        "held": "rifle.ak",
        "ammo_contents": 28,
        "ammo_capacity": 30,
        "ammo_type": "ammo.rifle"
      }
    }
  ]
}
```

## Entity Categories

| Category | Color | Shape |
|----------|-------|-------|
| `player` | Red | Circle |
| `player-sleeping` | Dark maroon | Square |
| `player-npc` | Yellow | Circle |
| `animal-bear` | Magenta | Circle |
| `animal-wolf` | Magenta | Circle |
| `ore-sulfur` | Yellow | Diamond |
| `ore-metal` | Blue | Diamond |
| `ore-stone` | White | Diamond |
| `loot-*` | Gold | Square |
| `locked-crate` | Cyan | Square |
| `supply-drop` | Red-magenta | Triangle |
| `vehicle` | Purple | Circle |
| `dropped` (marked) | Pink glow | Diamond |

## Security Constraints

- **Read-only** — no memory writes, no calls into the game process
- **Private server only** — no public match use
- No firmware/driver/Defender/BIOS changes
- No aimbot, chams, recoil reduction, or any write-based feature

## TODO

- [ ] `dma_probe.py` — dump.cs parser + MemProcFS pattern scanner
- [ ] `rust_snapshot.py` — main Rust DMA entity reader loop (replaces skeld_snapshot.py)
- [ ] Populate `rust_offsets_schema.json` with real offsets after Il2CppDumper run
- [ ] Run validation session on private server
