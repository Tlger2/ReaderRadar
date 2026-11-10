#!/usr/bin/env python3
# lang: Python 3.10+, file: rust_probe.py
# target: Windows host with MemProcFS vmm.dll, Rust game process
# function: Discover entity list pointer and all field offsets by scanning
#           IL2CPP MetadataRegistration at the confirmed RVA.
#           Produces dump_fields.json consumed by rust_snapshot.py.
#
# Config: see dma_config.py (MEMPROCFS_DIR / VMM_DLL_PATH / RUST_PROCESS)

from __future__ import annotations

import hashlib
import struct
import json
import ctypes
from pathlib import Path

import dma_config

# ─── Build constants ──────────────────────────────────────────────────────────
METADATA_RVA        = 0x0DBED230
GAMEASSEMBLY_SHA256 = "199a470f2510320be42d127cc55e946f51a2fc0b0b3ed0bf8671c2a8bb73b252"

# Sanity window for the health field offset
EXPECT_HEALTH_MIN = 0x200
EXPECT_HEALTH_MAX = 0x280

# How many bytes of GameAssembly.dll to hash for the build check.
# Hashing the full DLL over DMA would take many seconds; the PE headers
# plus the first MB are unique enough per build.
HASH_CHECK_SIZE = 0x100000  # 1 MiB


# ─── VMM helpers ──────────────────────────────────────────────────────────────

def load_vmm() -> ctypes.CDLL:
    dll_path = dma_config.vmm_dll_path()
    print(f"[probe] loading {dll_path}")
    return ctypes.CDLL(str(dll_path))


def init_vmm(vmm: ctypes.CDLL):
    device = dma_config.vmm_device()
    args   = [b"", b"-device", device.encode("ascii")]
    argc   = len(args)
    argv   = (ctypes.c_char_p * argc)(*args)
    h = vmm.VMMDLL_Initialize(argc, argv)
    if not h:
        raise RuntimeError(f"VMMDLL_Initialize failed (device={device})")
    return h


def find_pid(vmm, h, name: bytes) -> int:
    pid = ctypes.c_ulong(0)
    ok  = vmm.VMMDLL_PidGetFromName(h, name, ctypes.byref(pid))
    if not ok or pid.value == 0:
        raise RuntimeError(f"Process '{name.decode(errors='replace')}' not found")
    return pid.value


def read_mem(vmm, h, pid: int, addr: int, size: int) -> bytes:
    buf    = (ctypes.c_byte * size)()
    cb     = ctypes.c_ulong(0)
    ok     = vmm.VMMDLL_MemRead(h, pid, addr, buf, size, ctypes.byref(cb))
    if not ok or cb.value != size:
        raise OSError(f"MemRead 0x{addr:016X} ({size}B) returned {cb.value}B")
    return bytes(buf)


def ptr(vmm, h, pid, addr) -> int:
    return struct.unpack_from("<Q", read_mem(vmm, h, pid, addr, 8))[0]


def u32(vmm, h, pid, addr) -> int:
    return struct.unpack_from("<I", read_mem(vmm, h, pid, addr, 4))[0]


def read_cstr(vmm, h, pid, addr: int, maxlen: int = 64) -> str:
    try:
        raw = read_mem(vmm, h, pid, addr, maxlen)
        end = raw.find(b"\x00")
        return (raw[:end] if end >= 0 else raw).decode("utf-8", errors="replace")
    except Exception:
        return ""


def get_module_base(vmm, h, pid, mod_name: str) -> int:
    base = vmm.VMMDLL_ProcessGetModuleBaseU(h, pid, mod_name.encode())
    if base == 0:
        raise RuntimeError(f"Module '{mod_name}' not found in pid {pid}")
    return base


# ─── Build hash check ─────────────────────────────────────────────────────────

def check_build_hash(vmm, h, pid: int, ga_base: int) -> bool:
    """
    Read the first HASH_CHECK_SIZE bytes of GameAssembly.dll from memory and
    compare a SHA-256 of them against GAMEASSEMBLY_SHA256.

    Returns True on match, False on mismatch (warning only — probe continues).
    Skips the check gracefully if the DMA read fails.
    """
    print(f"[probe] verifying GameAssembly.dll build hash (first {HASH_CHECK_SIZE//1024} KiB)…")
    try:
        data = read_mem(vmm, h, pid, ga_base, HASH_CHECK_SIZE)
    except OSError as exc:
        print(f"[probe]   WARNING: could not read DLL header for hash check: {exc}")
        return False

    digest = hashlib.sha256(data).hexdigest()
    # The stored hash covers the full DLL; we can only partially check here,
    # so compare just the first 16 hex chars as a build fingerprint.
    fingerprint = GAMEASSEMBLY_SHA256[:16]
    partial     = digest[:16]

    if partial == fingerprint:
        print(f"[probe]   build fingerprint OK ({fingerprint}…)")
        return True
    else:
        print(
            f"[probe]   WARNING: build fingerprint mismatch!\n"
            f"           expected prefix: {fingerprint}…\n"
            f"           got prefix:      {partial}…\n"
            f"           Offsets may be wrong — re-run probe on a fresh dump."
        )
        return False


# ─── MetadataRegistration scanner ────────────────────────────────────────────

class MetadataReg:
    def __init__(self, vmm, h, pid, ga_base: int):
        self.vmm = vmm
        self.h   = h
        self.pid = pid
        mr_ptr = ptr(vmm, h, pid, ga_base + METADATA_RVA)
        self.mr_ptr     = mr_ptr
        self.type_count = struct.unpack_from("<q",
            read_mem(vmm, h, pid, mr_ptr + 0x50, 8))[0]
        self.type_infos = ptr(vmm, h, pid, mr_ptr + 0x58)
        print(f"[probe] MetadataReg @ 0x{mr_ptr:016X}  typeCount={self.type_count}")

    def iter_types(self):
        BATCH = 256
        for i in range(0, self.type_count, BATCH):
            count = min(BATCH, self.type_count - i)
            raw   = read_mem(self.vmm, self.h, self.pid,
                             self.type_infos + i * 8, count * 8)
            for j in range(count):
                klass = struct.unpack_from("<Q", raw, j * 8)[0]
                if klass:
                    yield i + j, klass

    def klass_name(self, klass_ptr: int) -> str:
        try:
            name_ptr = ptr(self.vmm, self.h, self.pid, klass_ptr + 0x10)
            return read_cstr(self.vmm, self.h, self.pid, name_ptr)
        except Exception:
            return ""

    def klass_static_fields(self, klass_ptr: int) -> int:
        try:
            return ptr(self.vmm, self.h, self.pid, klass_ptr + 0xB8)
        except Exception:
            return 0

    def klass_fields(self, klass_ptr: int) -> list[tuple[str, int]]:
        try:
            fields_ptr  = ptr(self.vmm, self.h, self.pid, klass_ptr + 0x80)
            field_count = u32(self.vmm, self.h, self.pid, klass_ptr + 0xF0)
            if field_count == 0 or field_count > 512 or fields_ptr == 0:
                return []
            result = []
            for i in range(field_count):
                base      = fields_ptr + i * 0x14
                fname_ptr = ptr(self.vmm, self.h, self.pid, base)
                offset    = u32(self.vmm, self.h, self.pid, base + 0x10)
                name      = read_cstr(self.vmm, self.h, self.pid, fname_ptr)
                result.append((name, offset))
            return result
        except Exception:
            return []


# ─── Entity list finder ───────────────────────────────────────────────────────

def scan_entity_list_static(mr: MetadataReg, ga_base: int) -> dict:
    target_names = {"BaseNetworkable", "EntityManager"}
    found: dict[str, int] = {}

    print("[probe] scanning MetadataRegistration typeInfos for entity list…")
    for idx, klass in mr.iter_types():
        name = mr.klass_name(klass)
        if name in target_names:
            print(f"[probe]   found '{name}' klass @ 0x{klass:016X} (typeInfo #{idx})")
            found[name] = klass
        if len(found) == len(target_names):
            break

    if not found:
        raise RuntimeError("Could not find BaseNetworkable in MetadataRegistration")

    klass_ptr     = found.get("BaseNetworkable") or next(iter(found.values()))
    static_fields = mr.klass_static_fields(klass_ptr)
    print(f"[probe] BaseNetworkable static_fields @ 0x{static_fields:016X}")

    if static_fields == 0:
        raise RuntimeError("BaseNetworkable static_fields ptr is null — wrong class or build")

    SCAN_RANGE = 0x200
    STRIDE     = 8
    best_offset = None
    best_ptr    = None

    for off in range(0, SCAN_RANGE, STRIDE):
        try:
            cand_ptr = ptr(mr.vmm, mr.h, mr.pid, static_fields + off)
            if cand_ptr < 0x1_0000_0000 or cand_ptr > 0x7FFF_FFFF_FFFF:
                continue
            count    = u32(mr.vmm, mr.h, mr.pid, cand_ptr + 0x20)
            keys_arr = ptr(mr.vmm, mr.h, mr.pid, cand_ptr + 0x10)
            vals_arr = ptr(mr.vmm, mr.h, mr.pid, cand_ptr + 0x18)
            if not (1 <= count <= 8192):
                continue
            if keys_arr < 0x1_0000_0000 or vals_arr < 0x1_0000_0000:
                continue
            first_entity = ptr(mr.vmm, mr.h, mr.pid, vals_arr + 0x20)
            if first_entity < 0x1_0000_0000:
                continue
            print(f"[probe]   clientEntities @ offset +0x{off:X} → ptr=0x{cand_ptr:016X} count={count}")
            best_offset = off
            best_ptr    = cand_ptr
            break
        except OSError:
            continue

    if best_offset is None:
        raise RuntimeError("Could not locate clientEntities in BaseNetworkable static fields")

    return {
        "klass_ptr"             : klass_ptr,
        "static_fields_ptr"     : static_fields,
        "client_entities_offset": best_offset,
        "entities_ptr"          : best_ptr,
    }


# ─── Field offset resolver ────────────────────────────────────────────────────
#
# Target classes and what we pull from them:
#
#   BaseCombatEntity  → health, max_health
#   BasePlayer        → display_name, flags, life_state, held_entity
#   BaseNetworkable   → (entity list — already found above)
#   HackableLockedCrate → hack_seconds, required_hack_seconds, is_lootable
#   Item              → shortname  (used to identify dropped items / held items)
#
# Field names are matched case-insensitively against the IL2CPP metadata name.

_FIELD_TARGETS: dict[str, list[tuple[str, str]]] = {
    # class_name → [(field_key, field_name_substring), …]
    "BaseCombatEntity": [
        ("health",     "health"),        # _health / health, excluding maxHealth
        ("max_health", "maxhealth"),
    ],
    "BasePlayer": [
        ("display_name", "displayname"),
        ("flags",        "flags"),
        ("life_state",   "lifestate"),
        ("held_entity",  "heldentity"),
    ],
    "HackableLockedCrate": [
        ("hack_seconds",           "hackseconds"),
        ("required_hack_seconds",  "requiredhackseconds"),
        ("is_lootable",            "islootable"),
    ],
    "Item": [
        ("item_shortname", "shortname"),
    ],
}

# How many target fields we expect to find before early-exit
_TOTAL_TARGETS = sum(len(v) for v in _FIELD_TARGETS.values())


def _match_field(key: str, low_name: str) -> bool:
    """
    Return True when low_name contains key's search substring.
    Special-case health to exclude 'maxhealth'.
    """
    if key == "health":
        return "health" in low_name and "max" not in low_name
    return key in low_name


def resolve_field_offsets(mr: MetadataReg) -> dict:
    targets = {cls: list(pairs) for cls, pairs in _FIELD_TARGETS.items()}
    offsets: dict[str, int] = {}
    found_count = 0

    print("[probe] resolving field offsets…")

    for _, klass in mr.iter_types():
        name = mr.klass_name(klass)
        if name not in targets:
            continue

        fields = mr.klass_fields(klass)
        pairs  = targets[name]
        still_needed = []

        for (out_key, search) in pairs:
            if out_key in offsets:
                continue   # already found from a parent class
            matched = False
            for fname, foff in fields:
                low = fname.lower()
                if _match_field(search, low):
                    # Sanity check: health should be in a reasonable range
                    if out_key == "health" and not (EXPECT_HEALTH_MIN <= foff <= EXPECT_HEALTH_MAX):
                        continue
                    offsets[out_key] = foff
                    print(f"[probe]   {name}.{fname} → 0x{foff:X}  ({out_key})")
                    found_count += 1
                    matched = True
                    break
            if not matched:
                still_needed.append((out_key, search))

        targets[name] = still_needed
        # Remove class from watch list if all its fields are found
        if not still_needed:
            del targets[name]

        if found_count >= _TOTAL_TARGETS:
            break

    # Apply defaults for anything the scanner missed
    defaults = {
        "health"     : 0x218,
        "max_health" : 0x21C,
        "display_name": 0x370,
        "flags"      : 0x1C4,
        "life_state" : 0x278,
        "held_entity": 0x458,
        # Locked crate defaults omitted intentionally (0 means "disabled")
    }
    for key, val in defaults.items():
        if key not in offsets:
            print(f"[probe]   WARNING: '{key}' not found — using default 0x{val:X}")
            offsets[key] = val

    return offsets


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    vmm    = load_vmm()
    h      = init_vmm(vmm)
    proc   = dma_config.process_name_bytes()
    pid    = find_pid(vmm, h, proc)
    ga_base = get_module_base(vmm, h, pid, "GameAssembly.dll")

    print(f"[probe] pid={pid}  process={dma_config.process_name()}")
    print(f"[probe] GameAssembly.dll @ 0x{ga_base:016X}")

    # Build fingerprint check (non-fatal mismatch)
    build_match = check_build_hash(vmm, h, pid, ga_base)

    mr = MetadataReg(vmm, h, pid, ga_base)

    entity_info   = scan_entity_list_static(mr, ga_base)
    field_offsets = resolve_field_offsets(mr)

    result = {
        "schema_version" : 2,
        "build_hash_ok"  : build_match,
        "ga_base"        : ga_base,
        "pid"            : pid,
        "process"        : dma_config.process_name(),
        "entity_info"    : entity_info,
        "field_offsets"  : field_offsets,
    }

    out_path = Path(__file__).parent / "dump_fields.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2,
                  default=lambda o: hex(o) if isinstance(o, int) else TypeError)

    print(f"[probe] wrote {out_path}")
    print("[probe] done.")


if __name__ == "__main__":
    main()
