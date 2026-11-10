#!/usr/bin/env python3
# lang: Python 3.10+, file: rust_probe.py
# target: Windows host with MemProcFS vmm.dll, Rust game process
# function: Discover entity list pointer and field offsets by scanning
#           IL2CPP MetadataRegistration at the confirmed RVA.
#           Produces dump_fields.json consumed by rust_snapshot.py.
#
# Config: see dma_config.py (MEMPROCFS_DIR / VMM_DLL_PATH / RUST_PROCESS)

from __future__ import annotations

import struct
import json
import ctypes
from pathlib import Path

import dma_config

# ─── Confirmed build constants ─────────────────────────────────────────────────
METADATA_RVA        = 0x0DBED230
GAMEASSEMBLY_SHA256 = "199a470f2510320be42d127cc55e946f51a2fc0b0b3ed0bf8671c2a8bb73b252"

EXPECT_HEALTH_MIN  = 0x200
EXPECT_HEALTH_MAX  = 0x280


def load_vmm():
    dll_path = dma_config.vmm_dll_path()
    print(f"[probe] loading {dll_path}")
    return ctypes.CDLL(str(dll_path))


def init_vmm(vmm):
    device = dma_config.vmm_device()
    args = [b"", b"-device", device.encode("ascii")]
    argc = len(args)
    argv_type = ctypes.c_char_p * argc
    argv = argv_type(*args)
    handle = vmm.VMMDLL_Initialize(argc, argv)
    if not handle:
        raise RuntimeError(f"VMMDLL_Initialize failed (device={device})")
    return handle


def find_pid(vmm, handle, name: bytes) -> int:
    pid = ctypes.c_ulong(0)
    ok = vmm.VMMDLL_PidGetFromName(handle, name, ctypes.byref(pid))
    if not ok or pid.value == 0:
        raise RuntimeError(f"Process '{name.decode(errors='replace')}' not found")
    return pid.value


def read_mem(vmm, handle, pid: int, addr: int, size: int) -> bytes:
    buf = (ctypes.c_byte * size)()
    cb_read = ctypes.c_ulong(0)
    ok = vmm.VMMDLL_MemRead(handle, pid, addr, buf, size, ctypes.byref(cb_read))
    if not ok or cb_read.value != size:
        raise OSError(f"MemRead failed at 0x{addr:016X} (got {cb_read.value}/{size})")
    return bytes(buf)


def ptr(vmm, handle, pid, addr):
    return struct.unpack_from("<Q", read_mem(vmm, handle, pid, addr, 8))[0]


def u32(vmm, handle, pid, addr):
    return struct.unpack_from("<I", read_mem(vmm, handle, pid, addr, 4))[0]


def read_cstr(vmm, handle, pid, addr, maxlen=64) -> str:
    try:
        raw = read_mem(vmm, handle, pid, addr, maxlen)
        end = raw.find(b"\x00")
        return raw[:end].decode("utf-8", errors="replace") if end >= 0 else raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def get_module_base(vmm, handle, pid, mod_name: str) -> int:
    base = vmm.VMMDLL_ProcessGetModuleBaseU(handle, pid, mod_name.encode())
    if base == 0:
        raise RuntimeError(f"Module '{mod_name}' not found in pid {pid}")
    return base


# =============================================================================
# MetadataRegistration scanner
# =============================================================================

class MetadataReg:
    def __init__(self, vmm, handle, pid, ga_base):
        self.vmm = vmm
        self.handle = handle
        self.pid = pid
        mr_ptr = ptr(vmm, handle, pid, ga_base + METADATA_RVA)
        self.mr_ptr      = mr_ptr
        self.type_count  = struct.unpack_from("<q", read_mem(vmm, handle, pid, mr_ptr + 0x50, 8))[0]
        self.type_infos  = ptr(vmm, handle, pid, mr_ptr + 0x58)
        print(f"[probe] MetadataReg @ 0x{mr_ptr:016X}  typeCount={self.type_count}")

    def iter_types(self):
        BATCH = 256
        for i in range(0, self.type_count, BATCH):
            count = min(BATCH, self.type_count - i)
            raw = read_mem(
                self.vmm, self.handle, self.pid,
                self.type_infos + i * 8,
                count * 8
            )
            for j in range(count):
                klass = struct.unpack_from("<Q", raw, j * 8)[0]
                if klass:
                    yield i + j, klass

    def klass_name(self, klass_ptr) -> str:
        try:
            name_ptr = ptr(self.vmm, self.handle, self.pid, klass_ptr + 0x10)
            return read_cstr(self.vmm, self.handle, self.pid, name_ptr)
        except Exception:
            return ""

    def klass_static_fields(self, klass_ptr) -> int:
        try:
            return ptr(self.vmm, self.handle, self.pid, klass_ptr + 0xB8)
        except Exception:
            return 0

    def klass_fields(self, klass_ptr):
        try:
            fields_ptr = ptr(self.vmm, self.handle, self.pid, klass_ptr + 0x80)
            field_count = u32(self.vmm, self.handle, self.pid, klass_ptr + 0xF0)
            if field_count == 0 or field_count > 512 or fields_ptr == 0:
                return []
            result = []
            for i in range(field_count):
                field_base = fields_ptr + i * 0x14
                fname_ptr = ptr(self.vmm, self.handle, self.pid, field_base)
                offset    = u32(self.vmm, self.handle, self.pid, field_base + 0x10)
                name      = read_cstr(self.vmm, self.handle, self.pid, fname_ptr)
                result.append((name, offset))
            return result
        except Exception:
            return []


def scan_entity_list_static(mr: MetadataReg, ga_base: int) -> dict:
    target_names = {"BaseNetworkable", "EntityManager"}
    found = {}

    print("[probe] scanning MetadataRegistration typeInfos for entity class...")
    for idx, klass in mr.iter_types():
        name = mr.klass_name(klass)
        if name in target_names:
            print(f"[probe]   found '{name}' klass @ 0x{klass:016X} (typeInfo #{idx})")
            found[name] = klass
        if len(found) == len(target_names):
            break

    if not found:
        raise RuntimeError("Could not find BaseNetworkable in MetadataRegistration")

    klass_ptr = found.get("BaseNetworkable") or next(iter(found.values()))
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
            cand_ptr = ptr(mr.vmm, mr.handle, mr.pid, static_fields + off)
            if cand_ptr < 0x1_0000_0000 or cand_ptr > 0x7FFF_FFFF_FFFF:
                continue
            count    = u32(mr.vmm, mr.handle, mr.pid, cand_ptr + 0x20)
            keys_arr = ptr(mr.vmm, mr.handle, mr.pid, cand_ptr + 0x10)
            vals_arr = ptr(mr.vmm, mr.handle, mr.pid, cand_ptr + 0x18)
            if not (1 <= count <= 8192):
                continue
            if keys_arr < 0x1_0000_0000 or vals_arr < 0x1_0000_0000:
                continue
            first_entity = ptr(mr.vmm, mr.handle, mr.pid, vals_arr + 0x20)
            if first_entity < 0x1_0000_0000:
                continue
            print(f"[probe]   candidate clientEntities @ offset +0x{off:X} → ptr=0x{cand_ptr:016X} count={count}")
            best_offset = off
            best_ptr    = cand_ptr
            break
        except OSError:
            continue

    if best_offset is None:
        raise RuntimeError("Could not locate clientEntities in BaseNetworkable static fields")

    return {
        "klass_ptr"               : klass_ptr,
        "static_fields_ptr"       : static_fields,
        "client_entities_offset"  : best_offset,
        "entities_ptr"            : best_ptr,
    }


def resolve_field_offsets(mr: MetadataReg) -> dict:
    target_classes = [
        "BaseCombatEntity",
        "BasePlayer",
        "BaseNetworkable",
    ]

    offsets = {}
    print("[probe] resolving field offsets...")

    for idx, klass in mr.iter_types():
        name = mr.klass_name(klass)
        if name not in target_classes:
            continue
        fields = mr.klass_fields(klass)
        for fname, foff in fields:
            lower = fname.lower()
            if "health" in lower and "max" not in lower:
                offsets["health"] = foff
                print(f"[probe]   {name}.{fname} → 0x{foff:X}  (health)")
            elif "displayname" in lower or lower == "_displayname":
                offsets["display_name"] = foff
                print(f"[probe]   {name}.{fname} → 0x{foff:X}  (display_name)")
            elif lower in ("flags", "_flags", "entityflags"):
                offsets["flags"] = foff
                print(f"[probe]   {name}.{fname} → 0x{foff:X}  (flags)")
            elif "heldentity" in lower or lower == "_heldentity":
                offsets["held_entity"] = foff
                print(f"[probe]   {name}.{fname} → 0x{foff:X}  (held_entity)")
            elif lower in ("lifestate", "_lifestate"):
                offsets["life_state"] = foff
                print(f"[probe]   {name}.{fname} → 0x{foff:X}  (life_state)")

        if len(offsets) >= 5:
            break

    defaults = {
        "health"      : 0x218,
        "display_name": 0x370,
        "flags"       : 0x1C4,
        "life_state"  : 0x278,
        "held_entity" : 0x458,
    }
    for key, val in defaults.items():
        if key not in offsets:
            print(f"[probe]   WARNING: {key} not found by scanner — using default 0x{val:X}")
            offsets[key] = val

    return offsets


def main():
    vmm = load_vmm()
    handle = init_vmm(vmm)
    proc = dma_config.process_name_bytes()
    pid = find_pid(vmm, handle, proc)
    ga_base = get_module_base(vmm, handle, pid, "GameAssembly.dll")
    print(f"[probe] pid={pid}  process={dma_config.process_name()}  GameAssembly.dll base=0x{ga_base:016X}")

    mr = MetadataReg(vmm, handle, pid, ga_base)

    entity_info = scan_entity_list_static(mr, ga_base)
    field_offsets = resolve_field_offsets(mr)

    result = {
        "ga_base"              : ga_base,
        "pid"                  : pid,
        "process"              : dma_config.process_name(),
        "entity_info"          : entity_info,
        "field_offsets"        : field_offsets,
    }

    out_path = Path(__file__).parent / "dump_fields.json"
    with open(out_path, "w", encoding="utf-8") as f:
        def default(o):
            if isinstance(o, int):
                return hex(o)
            raise TypeError
        json.dump(result, f, indent=2, default=default)

    print(f"[probe] wrote {out_path}")
    print("[probe] done.")


if __name__ == "__main__":
    main()
