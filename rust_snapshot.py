#!/usr/bin/env python3
# lang: Python 3.10+, file: rust_snapshot.py
# target: Windows host with MemProcFS vmm.dll, Rust game process
# function: Walk clientEntities, classify entities, read world positions
#           via Unity 6 transform hierarchy, write live JSON for the web stack.
#
# Config: see dma_config.py (MEMPROCFS_DIR / VMM_DLL_PATH / RUST_PROCESS / SNAPSHOT_PATH)

from __future__ import annotations

import argparse
import struct
import json
import math
import ctypes
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import dma_config

# ─── Probe data (populated by load_probe_data) ────────────────────────────────
GA_BASE           = 0
PID               = 0
ENTITIES_PTR      = 0
FIELD_HEALTH      = 0x218
FIELD_DISPLAY_NAME= 0x370
FIELD_FLAGS       = 0x1C4
FIELD_LIFE_STATE  = 0x278
FIELD_HELD_ENTITY = 0x458

LD_KEYS_OFF  = 0x10
LD_VALS_OFF  = 0x18
LD_COUNT_OFF = 0x20
LD_ELEM_BASE = 0x20
LD_ELEM_SIZE = 8

_radar_state = {"entities": [], "ts": 0}
_radar_lock  = threading.Lock()


def load_vmm():
    dll_path = dma_config.vmm_dll_path()
    print(f"[snap] loading {dll_path}")
    return ctypes.CDLL(str(dll_path))


def init_vmm(vmm):
    device = dma_config.vmm_device()
    args = [b"", b"-device", device.encode("ascii")]
    argc = len(args)
    argv = (ctypes.c_char_p * argc)(*args)
    h = vmm.VMMDLL_Initialize(argc, argv)
    if not h:
        raise RuntimeError(f"VMMDLL_Initialize failed (device={device})")
    return h


def find_pid(vmm, h, name: bytes) -> int:
    pid = ctypes.c_ulong(0)
    if not vmm.VMMDLL_PidGetFromName(h, name, ctypes.byref(pid)) or not pid.value:
        raise RuntimeError(f"Process '{name.decode(errors='replace')}' not found")
    return pid.value


def _read(vmm, h, pid, addr, size) -> bytes:
    buf = (ctypes.c_byte * size)()
    cb  = ctypes.c_ulong(0)
    ok  = vmm.VMMDLL_MemRead(h, pid, addr, buf, size, ctypes.byref(cb))
    if not ok or cb.value != size:
        raise OSError(f"MemRead 0x{addr:016X} ({size}B) got {cb.value}")
    return bytes(buf)


def _ptr(vmm, h, pid, addr) -> int:
    return struct.unpack_from("<Q", _read(vmm, h, pid, addr, 8))[0]


def _u32(vmm, h, pid, addr) -> int:
    return struct.unpack_from("<I", _read(vmm, h, pid, addr, 4))[0]


def _f32(vmm, h, pid, addr) -> float:
    return struct.unpack_from("<f", _read(vmm, h, pid, addr, 4))[0]


def _cstr(vmm, h, pid, addr, maxlen=64) -> str:
    try:
        raw = _read(vmm, h, pid, addr, maxlen)
        end = raw.find(b"\x00")
        return raw[:end].decode("utf-8", errors="replace")
    except Exception:
        return ""


def _managed_str(vmm, h, pid, str_obj) -> str:
    if not str_obj:
        return ""
    try:
        length = _u32(vmm, h, pid, str_obj + 0x10)
        if length == 0 or length > 256:
            return ""
        raw = _read(vmm, h, pid, str_obj + 0x14, length * 2)
        return raw.decode("utf-16-le", errors="replace")
    except Exception:
        return ""


def world_position(vmm, h, pid, native_transform) -> tuple[float, float, float]:
    hierarchy = _ptr(vmm, h, pid, native_transform + 0x38)
    index_raw = _read(vmm, h, pid, native_transform + 0x40, 4)
    index = struct.unpack_from("<i", index_raw)[0]

    records = _ptr(vmm, h, pid, hierarchy + 0x18)
    parents = _ptr(vmm, h, pid, hierarchy + 0x20)

    if not (0 <= index < 65536):
        raise ValueError(f"transform index out of range: {index}")

    def read_local(idx):
        base = records + idx * 0x30
        raw = _read(vmm, h, pid, base, 12)
        return struct.unpack_from("<3f", raw)

    chain = []
    cur = index
    seen = set()
    for _ in range(64):
        if cur in seen:
            break
        seen.add(cur)
        chain.append(cur)
        p_raw = _read(vmm, h, pid, parents + cur * 4, 4)
        parent = struct.unpack_from("<i", p_raw)[0]
        if parent < 0:
            break
        cur = parent

    wx, wy, wz = 0.0, 0.0, 0.0
    for idx in reversed(chain):
        lx, ly, lz = read_local(idx)
        wx += lx; wy += ly; wz += lz

    if not all(math.isfinite(v) for v in (wx, wy, wz)):
        raise ValueError("non-finite world position")
    return wx, wy, wz


def get_native_transform(vmm, h, pid, entity_ptr) -> int:
    native_comp   = _ptr(vmm, h, pid, entity_ptr + 0x10)
    native_go     = _ptr(vmm, h, pid, native_comp + 0x30)
    comp_storage  = _ptr(vmm, h, pid, native_go   + 0x30)
    xform_entry   = _ptr(vmm, h, pid, comp_storage + 0x08)
    managed_xform = _ptr(vmm, h, pid, xform_entry  + 0x28)
    native_xform  = _ptr(vmm, h, pid, managed_xform + 0x10)
    return native_xform


def klass_name_of(vmm, h, pid, entity_ptr) -> str:
    try:
        klass = _ptr(vmm, h, pid, entity_ptr)
        name_ptr = _ptr(vmm, h, pid, klass + 0x10)
        return _cstr(vmm, h, pid, name_ptr)
    except Exception:
        return ""


def is_valid_ptr(p: int) -> bool:
    return 0x1_0000_0000 <= p <= 0x7FFF_FFFF_FFFF


def read_entity(vmm, h, pid, entity_ptr: int) -> dict | None:
    if not is_valid_ptr(entity_ptr):
        return None
    try:
        name = klass_name_of(vmm, h, pid, entity_ptr)
        if not name:
            return None

        classify = "other"
        if "BasePlayer" in name or name == "BasePlayer":
            classify = "player"
        elif "Npc" in name or "Scientist" in name or "Zombie" in name:
            classify = "npc"
        elif "StorageContainer" in name or "LootContainer" in name:
            classify = "loot"
        elif "ResourceEntity" in name or "OreResource" in name:
            classify = "resource"

        native_xform = get_native_transform(vmm, h, pid, entity_ptr)
        x, y, z = world_position(vmm, h, pid, native_xform)

        result = {
            "ptr"     : hex(entity_ptr),
            "class"   : name,
            "type"    : classify,
            "x"       : round(x, 2),
            "y"       : round(y, 2),
            "z"       : round(z, 2),
        }

        if classify == "player":
            try:
                health = _f32(vmm, h, pid, entity_ptr + FIELD_HEALTH)
                result["health"] = round(health, 1) if math.isfinite(health) else 0.0
            except Exception:
                result["health"] = 0.0

            try:
                name_obj = _ptr(vmm, h, pid, entity_ptr + FIELD_DISPLAY_NAME)
                result["name"] = _managed_str(vmm, h, pid, name_obj)
            except Exception:
                result["name"] = ""

            try:
                flags     = _u32(vmm, h, pid, entity_ptr + FIELD_FLAGS)
                life_state = _u32(vmm, h, pid, entity_ptr + FIELD_LIFE_STATE)
                result["flags"]      = flags
                result["life_state"] = life_state
                result["alive"]      = (life_state == 0)
                result["is_local"]   = bool(flags & 0x80)
            except Exception:
                result["alive"]    = True
                result["is_local"] = False

        return result

    except Exception:
        return None


def walk_entities(vmm, h, pid) -> list[dict]:
    try:
        entities_ptr = ENTITIES_PTR
        count = _u32(vmm, h, pid, entities_ptr + LD_COUNT_OFF)
        if count == 0 or count > 8192:
            return []

        vals_arr = _ptr(vmm, h, pid, entities_ptr + LD_VALS_OFF)
        raw = _read(vmm, h, pid, vals_arr + LD_ELEM_BASE, count * LD_ELEM_SIZE)

        entities = []
        for i in range(count):
            ep = struct.unpack_from("<Q", raw, i * 8)[0]
            rec = read_entity(vmm, h, pid, ep)
            if rec:
                entities.append(rec)
        return entities
    except Exception as e:
        print(f"[snap] walk_entities error: {e}")
        return []


def write_snapshot_file(state: dict, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(state, separators=(",", ":")).encode("utf-8")
    tmp.write_bytes(payload)
    tmp.replace(path)


def snapshot_loop(vmm, h, pid, snap_path: Path, interval: float = 0.10):
    global _radar_state
    while True:
        t0 = time.monotonic()
        entities = walk_entities(vmm, h, pid)
        state = {"entities": entities, "ts": time.time()}
        with _radar_lock:
            _radar_state = state
        try:
            write_snapshot_file(state, snap_path)
        except OSError as e:
            print(f"[snap] write {snap_path}: {e}")
        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, interval - elapsed))


class RadarHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/data":
            with _radar_lock:
                payload = json.dumps(_radar_state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path in ("/", "/index.html"):
            html = RADAR_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        else:
            self.send_response(404)
            self.end_headers()


RADAR_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Rust Radar (local debug)</title>
<style>
  body { background:#111; color:#eee; font-family:monospace; margin:0; }
  canvas { display:block; margin:auto; background:#1a1a1a; }
  #info { text-align:center; padding:4px; font-size:12px; color:#888; }
</style>
</head>
<body>
<div id="info">Rust DMA Radar (local) — connecting...</div>
<canvas id="c" width="800" height="800"></canvas>
<script>
const C = document.getElementById('c');
const ctx = C.getContext('2d');
const info = document.getElementById('info');
let data = {entities:[], ts:0};
let scale = 2.0, offX = 0, offY = 0;

function worldToScreen(x, z) {
  return [(x - offX) * scale + C.width/2, (z - offY) * scale + C.height/2];
}

function draw() {
  ctx.clearRect(0, 0, C.width, C.height);
  for (const e of data.entities) {
    const [sx, sy] = worldToScreen(e.x, e.z);
    if (sx < -10 || sx > C.width+10 || sy < -10 || sy > C.height+10) continue;
    if (e.type === 'player') {
      ctx.fillStyle = e.is_local ? '#00ff00' : (e.alive ? '#ff4444' : '#666');
      ctx.beginPath(); ctx.arc(sx, sy, 5, 0, Math.PI*2); ctx.fill();
      if (e.name) {
        ctx.fillStyle = '#fff'; ctx.font = '10px monospace';
        ctx.fillText(e.name + (e.health ? ` ${e.health|0}hp` : ''), sx+7, sy+4);
      }
    } else if (e.type === 'loot') {
      ctx.fillStyle = '#ffdd00';
      ctx.fillRect(sx-3, sy-3, 6, 6);
    } else if (e.type === 'npc') {
      ctx.fillStyle = '#ff8800';
      ctx.beginPath(); ctx.arc(sx, sy, 4, 0, Math.PI*2); ctx.fill();
    } else if (e.type === 'resource') {
      ctx.fillStyle = '#44aaff';
      ctx.fillRect(sx-2, sy-2, 4, 4);
    }
  }
}

async function poll() {
  try {
    const r = await fetch('/data');
    data = await r.json();
    const local = data.entities.find(e => e.is_local);
    if (local) { offX = local.x; offY = local.z; }
    info.textContent = `entities: ${data.entities.length}  ts: ${new Date(data.ts*1000).toLocaleTimeString()}`;
  } catch {}
  draw();
  setTimeout(poll, 100);
}
poll();
</script>
</body>
</html>
"""


def load_probe_data():
    global GA_BASE, PID, ENTITIES_PTR
    global FIELD_HEALTH, FIELD_DISPLAY_NAME, FIELD_FLAGS, FIELD_LIFE_STATE, FIELD_HELD_ENTITY

    path = Path(__file__).parent / "dump_fields.json"
    if not path.exists():
        raise FileNotFoundError("dump_fields.json not found — run rust_probe.py first")

    with open(path, encoding="utf-8") as f:
        d = json.load(f)

    def h(v):
        return int(v, 16) if isinstance(v, str) else v

    GA_BASE       = h(d["ga_base"])
    PID           = h(d["pid"])
    ENTITIES_PTR  = h(d["entity_info"]["entities_ptr"])

    fo = d["field_offsets"]
    FIELD_HEALTH       = h(fo.get("health",       hex(FIELD_HEALTH)))
    FIELD_DISPLAY_NAME = h(fo.get("display_name", hex(FIELD_DISPLAY_NAME)))
    FIELD_FLAGS        = h(fo.get("flags",        hex(FIELD_FLAGS)))
    FIELD_LIFE_STATE   = h(fo.get("life_state",   hex(FIELD_LIFE_STATE)))
    FIELD_HELD_ENTITY  = h(fo.get("held_entity",  hex(FIELD_HELD_ENTITY)))

    print(f"[snap] GA_BASE=0x{GA_BASE:016X}  PID={PID}  entities_ptr=0x{ENTITIES_PTR:016X}")


def main():
    parser = argparse.ArgumentParser(description="Rust DMA entity snapshot loop")
    parser.add_argument("--local-server", action="store_true",
                        help="Also serve a minimal debug radar on :8765")
    parser.add_argument("--interval", type=float, default=0.10,
                        help="Snapshot interval in seconds (default 0.10)")
    args = parser.parse_args()

    load_probe_data()
    vmm = load_vmm()
    h   = init_vmm(vmm)

    pid = find_pid(vmm, h, dma_config.process_name_bytes())
    print(f"[snap] attached pid={pid} process={dma_config.process_name()}")

    snap_path = dma_config.snapshot_path()
    print(f"[snap] writing live JSON to {snap_path}")

    t = threading.Thread(
        target=snapshot_loop,
        args=(vmm, h, pid, snap_path, args.interval),
        daemon=True,
    )
    t.start()

    if args.local_server:
        port = 8765
        srv  = HTTPServer(("0.0.0.0", port), RadarHandler)
        print(f"[snap] local debug radar at http://localhost:{port}/")
        srv.serve_forever()
    else:
        print("[snap] running (Ctrl+C to stop). Prefer web_radar_server + web_radar_push for UI.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("[snap] stopped")


if __name__ == "__main__":
    main()
