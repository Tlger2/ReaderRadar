#!/usr/bin/env python3
# lang: Python 3.10+, file: rust_snapshot.py
# target: Windows host, MemProcFS vmm.dll, Rust game via DMA
# function: Walk clientEntities, classify entities, read world positions
#           via Unity 6 transform hierarchy. Writes live JSON consumed by
#           web_radar_server + web_radar_push.
#
# Output schema (stable across versions):
#
#   {
#     "ts":           float,            Unix timestamp
#     "world_size":   int,              Server map size (metres)
#     "local_player": null | {          Null when entity not found
#       "position": {"x":f, "y":f, "z":f},
#       "health":   float
#     },
#     "entities": [                     All non-local entities
#       {
#         "category": str,              See _CLASS_RULES below
#         "position": {"x":f, "y":f, "z":f},
#         "meta": {                     Category-dependent keys
#           "name":           str,      players
#           "health":         float,    players / bradley / heli
#           "held":           str|null, players — item shortname or null
#           "ammo_contents":  int,      best-effort, players with ranged weapon
#           "ammo_capacity":  int,
#           "alive":          bool,     players
#           "hackSeconds":    float,    locked-crate
#           "requiredHackSeconds": float,
#           "isLootable":     bool,
#         }
#       }
#     ]
#   }
#
# Config: see dma_config.py

from __future__ import annotations

import argparse
import ctypes
import json
import math
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import dma_config

# ─── Probe data — overwritten by load_probe_data() ───────────────────────────
GA_BASE            = 0
ENTITIES_PTR       = 0

# Field offsets inside entity objects (defaults from known Rust build)
FIELD_HEALTH       = 0x218
FIELD_DISPLAY_NAME = 0x370
FIELD_FLAGS        = 0x1C4
FIELD_LIFE_STATE   = 0x278
FIELD_HELD_ENTITY  = 0x458
FIELD_MAX_HEALTH   = 0x21C  # BaseCombatEntity._maxHealth (best-effort)

# Offset layout of clientEntities Dictionary
LD_VALS_OFF  = 0x18
LD_COUNT_OFF = 0x20
LD_ELEM_BASE = 0x20
LD_ELEM_SIZE = 8

# PlayerFlags bit-fields (stable since Rust ~2020 — flag bits shift rarely)
FLAG_SLEEPING  = 0x01   # PlayerFlags.Sleeping
FLAG_WOUNDED   = 0x02   # PlayerFlags.Wounded
FLAG_ADMIN     = 0x04   # PlayerFlags.IsAdmin
FLAG_CONNECTED = 0x08   # PlayerFlags.ReceivingSnapshot  ← best local-player heuristic

# LifeState
LS_ALIVE   = 0
LS_WOUNDED = 1
LS_DEAD    = 2

# Lock crate — if probed; falls back to 0 (skipped)
FIELD_HACK_SECONDS          = 0
FIELD_REQUIRED_HACK_SECONDS = 0
FIELD_IS_LOOTABLE           = 0

_state: dict = {"entities": [], "local_player": None, "ts": 0.0, "world_size": 3500}
_lock = threading.Lock()


# ─── HeldEntity class → Rust item shortname ──────────────────────────────────
# IL2CPP class name of the HeldEntity subclass → Rust item shortname.
# Covers every common meta-weapon; unknown classes return None.
_HELD_CLASS: dict[str, str] = {
    # Rifles
    "BoltRifle":           "rifle.bolt",
    "AK47":                "rifle.ak",
    "LR300Rifle":          "rifle.lr300",
    "L96Rifle":            "rifle.l96",
    "SemiAutoRifle":       "rifle.semiauto",
    "M39MissileRifle":     "rifle.m39",
    "M39Rifle":            "rifle.m39",
    # SMGs
    "MP5A4":               "smg.mp5",
    "Thompson":            "smg.thompson",
    "CustomSMG":           "smg.2",
    # Pistols
    "PythonRevolver":      "pistol.python",
    "Revolver":            "pistol.revolver",
    "SemiAutoPistol":      "pistol.semiauto",
    "M92Pistol":           "pistol.m92",
    "Nailgun":             "pistol.nailgun",
    # Shotguns
    "PumpShotgun":         "shotgun.pump",
    "SPAS12":              "shotgun.spas12",
    "DoubleShotgun":       "shotgun.double",
    "BoomBox":             "shotgun.waterpipe",
    "WaterPipeShotgun":    "shotgun.waterpipe",
    # LMG
    "M249":                "lmg.m249",
    # Bow / crossbow
    "BowWeapon":           "bow.hunting",
    "CompoundBow":         "bow.compound",
    "Crossbow":            "crossbow",
    # Launchers
    "RocketLauncher":      "rocket.launcher",
    "MultipleGrenadeLauncher": "multiplegrenadelauncher",
    # Melee
    "Hatchet":             "hatchet",
    "Pickaxe":             "pickaxe",
    "StonePickaxe":        "stone.pickaxe",
    "StoneHatchet":        "stonehatchet",
    "Chainsaw":            "chainsaw",
    "Jackhammer":          "jackhammer",
    "Hammer":              "hammer",
    "CombatKnife":         "knife.combat",
    "BoneKnife":           "knife.bone",
    "MacheteSalvaged":     "machete",
    "Longsword":           "longsword",
    "Mace":                "mace",
    "Salvaged_Cleaver":    "salvaged.cleaver",
    "Salvaged_Sword":      "salvaged.sword",
    "SalvagedSword":       "salvaged.sword",
    # Tools / misc
    "Torch":               "torch",
    "FlashlightWeapon":    "flashlight.held",
    "EokaCombat":          "pistol.eoka",
    # Medical
    "MedicalSyringe":      "syringe.medical",
    "Bandage":             "bandage",
    "LargeBodyBag":        "largemedkit",
    # Explosives / grenades
    "TimedExplosive":      "explosive.timed",
    "SatchelCharge":       "explosive.satchel",
    "F1Grenade":           "grenade.f1",
    "BeancanGrenade":      "grenade.beancan",
    "SmokeGrenade":        "grenade.smoke",
    "IncendiaryGrenade":   "grenade.incendiary",
    "Flare":               "flare",
    # Traps
    "Spear_Stone":         "spear.stone",
    "Spear_Wooden":        "spear.wooden",
}


# ─── Entity classification rules ─────────────────────────────────────────────
# IL2CPP class name substring → radar category.
# Checked in order; first match wins. Unmatched classes are dropped.
_CLASS_RULES: list[tuple[str, str]] = [
    # Vehicles / special
    ("BradleyAPC",             "bradley"),
    ("PatrolHelicopter",       "helicopter"),
    ("CH47HelicopterAIController", "helicopter"),   # Chinook
    ("Minicopter",             "vehicle"),
    ("ScrapTransportHeli",     "vehicle"),
    ("AttackHeli",             "vehicle"),
    ("ModularCar",             "vehicle"),
    ("RidableHorse",           "vehicle"),
    ("Rowboat",                "vehicle"),
    ("RHIB",                   "vehicle"),
    ("Tugboat",                "vehicle"),
    ("Submarine",              "vehicle"),
    ("SnowMobile",             "vehicle"),
    # NPCs — must come before generic BasePlayer
    ("Scientist",              "player-npc"),
    ("Zombie",                 "player-npc"),
    ("TunnelDweller",          "player-npc"),
    ("UnderwaterDweller",      "player-npc"),
    ("HeavyScientist",         "player-npc"),
    ("ScarecrowNPC",           "player-npc"),
    ("Murderer",               "player-npc"),
    # Animals
    ("Bear",                   "animal-bear"),
    ("PolarBear",              "animal-bear"),
    ("Wolf",                   "animal-wolf"),
    ("Boar",                   "animal"),
    ("Chicken",                "animal"),
    ("Stag",                   "animal"),
    ("SimpleSeal",             "animal"),
    ("BaseRidableAnimal",      "animal"),
    # Supply drops
    ("SupplyDrop",             "supply-drop"),
    # Locked crates
    ("HackableLockedCrate",    "locked-crate"),
    ("LockedByEntCrate",       "locked-crate-ent"),
    # Loot — most specific first
    ("EliteLootContainer",     "loot-elite"),
    ("AirdropContainer",       "loot-elite"),
    ("HackableLockedCrate",    "loot-elite"),
    ("MilitaryTunnel",         "loot-military"),
    ("armament",               "loot-military"),
    ("Barrel",                 "loot-barrel"),
    ("LootContainer",          "loot-basic"),
    ("StorageContainer",       "loot-basic"),
    ("FuelStorage",            "loot-basic"),
    # Ore nodes
    ("SulfurOreEntity",        "ore-sulfur"),
    ("Sulfur",                 "ore-sulfur"),
    ("HQMOreEntity",           "ore-hqm"),
    ("HQM",                    "ore-hqm"),
    ("MetalOreEntity",         "ore-metal"),
    ("MetalOre",               "ore-metal"),
    ("StoneOreEntity",         "ore-stone"),
    ("OreResourceEntity",      "ore-stone"),
    # Collectibles / pickups
    ("CollectibleEntity",      "pickup"),
    ("Hemp",                   "hemp"),
    ("Berry",                  "berry"),
    ("MushroomCluster",        "food"),
    # Dropped items
    ("DroppedItem",            "dropped"),
    ("DroppedItemContainer",   "dropped"),
    # Base / building
    ("BuildingPrivlidge",      "base"),
    ("BuildingBlock",          "base"),
    # Players — last; catches BasePlayer and any subclass not matched above
    ("BasePlayer",             "player"),
]


# ─── VMM helpers ─────────────────────────────────────────────────────────────

def load_vmm() -> ctypes.CDLL:
    dll_path = dma_config.vmm_dll_path()
    print(f"[snap] loading {dll_path}")
    lib = ctypes.CDLL(str(dll_path))
    # Set scatter argtypes once at load time
    lib.VMMDLL_Scatter_Initialize.restype  = ctypes.c_void_p
    lib.VMMDLL_Scatter_Initialize.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    lib.VMMDLL_Scatter_PrepareEx.restype   = ctypes.c_bool
    lib.VMMDLL_Scatter_PrepareEx.argtypes  = [
        ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_ulong,
        ctypes.c_char_p, ctypes.POINTER(ctypes.c_ulong),
    ]
    lib.VMMDLL_Scatter_ExecuteRead.restype  = ctypes.c_bool
    lib.VMMDLL_Scatter_ExecuteRead.argtypes = [ctypes.c_void_p]
    lib.VMMDLL_Scatter_CloseHandle.restype  = None
    lib.VMMDLL_Scatter_CloseHandle.argtypes = [ctypes.c_void_p]
    return lib


def init_vmm(vmm: ctypes.CDLL, pid: int | None = None):
    device = dma_config.vmm_device()
    args   = [b"", b"-device", device.encode("ascii")]
    argc   = len(args)
    argv   = (ctypes.c_char_p * argc)(*args)
    h = vmm.VMMDLL_Initialize(argc, argv)
    if not h:
        raise RuntimeError(f"VMMDLL_Initialize failed (device={device})")
    return h


def _find_pid(vmm, h, name: bytes) -> int:
    pid = ctypes.c_ulong(0)
    if not vmm.VMMDLL_PidGetFromName(h, name, ctypes.byref(pid)) or not pid.value:
        raise RuntimeError(f"Process '{name.decode(errors='replace')}' not found")
    return pid.value


def _read(vmm, h, pid: int, addr: int, size: int) -> bytes:
    buf = (ctypes.c_byte * size)()
    cb  = ctypes.c_ulong(0)
    ok  = vmm.VMMDLL_MemRead(h, pid, addr, buf, size, ctypes.byref(cb))
    if not ok or cb.value != size:
        raise OSError(f"MemRead 0x{addr:016X} ({size}B) returned {cb.value}B")
    return bytes(buf)


def _ptr(vmm, h, pid, addr: int) -> int:
    return struct.unpack_from("<Q", _read(vmm, h, pid, addr, 8))[0]


def _u32(vmm, h, pid, addr: int) -> int:
    return struct.unpack_from("<I", _read(vmm, h, pid, addr, 4))[0]


def _cstr(vmm, h, pid, addr: int, maxlen: int = 64) -> str:
    try:
        raw = _read(vmm, h, pid, addr, maxlen)
        end = raw.find(b"\x00")
        return (raw[:end] if end >= 0 else raw).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _managed_str(vmm, h, pid, str_obj: int) -> str:
    if not _valid(str_obj):
        return ""
    try:
        length = _u32(vmm, h, pid, str_obj + 0x10)
        if length == 0 or length > 256:
            return ""
        raw = _read(vmm, h, pid, str_obj + 0x14, length * 2)
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
    except Exception:
        return ""


def _valid(p: int) -> bool:
    return 0x1_0000_0000 <= p <= 0x7FFF_FFFF_FFFF


# ─── Scatter reader — batches multiple MemRead into one PCIe transaction ─────

class Scatter:
    """
    Batches arbitrary (addr, size) reads into a single VMMDLL scatter call.
    All buffers must be kept alive until execute() is called.
    """
    __slots__ = ("_vmm", "_h", "_pid", "_hS", "_bufs", "_cbs", "_done")

    def __init__(self, vmm, h, pid: int):
        self._vmm  = vmm
        self._h    = h
        self._pid  = pid
        self._hS   = vmm.VMMDLL_Scatter_Initialize(h, pid, 0)
        self._bufs: list[ctypes.Array] = []
        self._cbs:  list[ctypes.c_ulong] = []
        self._done  = not bool(self._hS)   # True if scatter init failed → fallback

    def prep(self, addr: int, size: int) -> int:
        """Schedule a read; returns the slot index used to retrieve data."""
        idx = len(self._bufs)
        buf = ctypes.create_string_buffer(size)
        cb  = ctypes.c_ulong(0)
        if not self._done:
            self._vmm.VMMDLL_Scatter_PrepareEx(self._hS, addr, size, buf, ctypes.byref(cb))
        else:
            # Scatter init failed — do individual read now as fallback
            try:
                data = _read(self._vmm, self._h, self._pid, addr, size)
                ctypes.memmove(buf, data, size)
                cb.value = size
            except OSError:
                pass
        self._bufs.append(buf)
        self._cbs.append(cb)
        return idx

    def execute(self) -> None:
        if not self._done and self._hS:
            self._vmm.VMMDLL_Scatter_ExecuteRead(self._hS)
            self._vmm.VMMDLL_Scatter_CloseHandle(self._hS)
            self._hS   = None
            self._done = True

    def get(self, idx: int) -> bytes:
        return bytes(self._bufs[idx])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._hS:
            self._vmm.VMMDLL_Scatter_CloseHandle(self._hS)
            self._hS = None


# ─── Entity block reader — one DMA read covers all field offsets ──────────────

_ENTITY_BLOCK = max(
    FIELD_HEALTH + 4,
    FIELD_DISPLAY_NAME + 8,
    FIELD_FLAGS + 4,
    FIELD_LIFE_STATE + 4,
    FIELD_HELD_ENTITY + 8,
    0x500,     # floor so we don't need to recalculate after offset change
)


class Block:
    """Lazy view over a raw entity memory block."""
    __slots__ = ("_buf",)

    def __init__(self, raw: bytes):
        self._buf = raw

    def ptr(self, off: int) -> int:
        if off + 8 > len(self._buf):
            return 0
        return struct.unpack_from("<Q", self._buf, off)[0]

    def u32(self, off: int) -> int:
        if off + 4 > len(self._buf):
            return 0
        return struct.unpack_from("<I", self._buf, off)[0]

    def f32(self, off: int) -> float:
        if off + 4 > len(self._buf):
            return 0.0
        v = struct.unpack_from("<f", self._buf, off)[0]
        return v if math.isfinite(v) else 0.0

    def i32(self, off: int) -> int:
        if off + 4 > len(self._buf):
            return 0
        return struct.unpack_from("<i", self._buf, off)[0]


# ─── Transform cache ──────────────────────────────────────────────────────────

class TransformCache:
    """
    Caches the stable parts of the Unity 6 transform hierarchy per entity.

    The pointer chain (entity→native_comp→native_go→…→hierarchy) is walked
    once per entity.  On subsequent frames only the per-node position records
    need to be re-read, which are scatter-batched across all entities.

    Stored per entity_ptr:
        (records_ptr, parents_ptr, index_chain)
    where index_chain is the list [leaf_index, …, root_index] from the parent
    walk.  On re-read we scatter-fetch records[i]*0x30 for every i in the
    chain, then accumulate to get the world position.
    """
    __slots__ = ("_cache",)

    def __init__(self):
        self._cache: dict[int, tuple] = {}

    # ── First-time full path resolution ──────────────────────────────────────
    def _resolve(self, vmm, h, pid: int, ep: int) -> Optional[tuple]:
        try:
            native_comp   = _ptr(vmm, h, pid, ep + 0x10)
            native_go     = _ptr(vmm, h, pid, native_comp + 0x30)
            comp_storage  = _ptr(vmm, h, pid, native_go   + 0x30)
            xform_entry   = _ptr(vmm, h, pid, comp_storage + 0x08)
            managed_xform = _ptr(vmm, h, pid, xform_entry  + 0x28)
            native_xform  = _ptr(vmm, h, pid, managed_xform + 0x10)

            hierarchy = _ptr(vmm, h, pid, native_xform + 0x38)
            index     = struct.unpack_from("<i",
                _read(vmm, h, pid, native_xform + 0x40, 4))[0]

            records   = _ptr(vmm, h, pid, hierarchy + 0x18)
            parents   = _ptr(vmm, h, pid, hierarchy + 0x20)

            if not (0 <= index < 65536):
                return None
            if not (_valid(records) and _valid(parents)):
                return None

            # Walk parent chain to build the full index list
            chain: list[int] = []
            cur = index
            seen: set[int] = set()
            for _ in range(32):
                if cur in seen:
                    break
                seen.add(cur)
                chain.append(cur)
                try:
                    p = struct.unpack_from("<i",
                        _read(vmm, h, pid, parents + cur * 4, 4))[0]
                except OSError:
                    break
                if p < 0:
                    break
                cur = p

            cached = (records, parents, chain)
            self._cache[ep] = cached
            return cached
        except Exception:
            return None

    # ── World position (uses cached path + scatter for position records) ──────
    def world_position(
        self,
        vmm, h, pid: int,
        ep: int,
        scatter: Optional[Scatter] = None,
    ) -> tuple[float, float, float]:
        """
        Returns (x, y, z) world position.
        If a Scatter object is supplied, the reads are batched into it and
        returned AFTER scatter.execute() is called by the caller.
        When scatter is None, reads are performed synchronously.
        """
        cached = self._cache.get(ep)
        if cached is None:
            cached = self._resolve(vmm, h, pid, ep)
            if cached is None:
                raise ValueError("transform path failed")

        records, _, chain = cached

        wx = wy = wz = 0.0
        for idx in reversed(chain):
            addr = records + idx * 0x30
            try:
                raw = _read(vmm, h, pid, addr, 12)
                lx, ly, lz = struct.unpack_from("<3f", raw)
                if math.isfinite(lx) and math.isfinite(ly) and math.isfinite(lz):
                    wx += lx; wy += ly; wz += lz
            except OSError:
                pass

        if not all(math.isfinite(v) for v in (wx, wy, wz)):
            raise ValueError("non-finite world position")
        return wx, wy, wz

    def evict(self, alive: set[int]) -> None:
        for k in list(self._cache):
            if k not in alive:
                del self._cache[k]


_xform = TransformCache()


# ─── Held item identification ─────────────────────────────────────────────────

def _held_shortname(vmm, h, pid: int, held_ptr: int) -> Optional[str]:
    """
    Read the IL2CPP class name of a HeldEntity and map it to a Rust shortname.
    Returns None if held_ptr is null, invalid, or the class is not in the table.
    """
    if not _valid(held_ptr):
        return None
    try:
        klass    = _ptr(vmm, h, pid, held_ptr)
        name_ptr = _ptr(vmm, h, pid, klass + 0x10)
        cls_name = _cstr(vmm, h, pid, name_ptr)
        return _HELD_CLASS.get(cls_name)
    except Exception:
        return None


# ─── Entity reader ────────────────────────────────────────────────────────────

def _classify(class_name: str) -> str:
    for sub, cat in _CLASS_RULES:
        if sub in class_name:
            return cat
    return ""


def read_entity(
    vmm, h, pid: int,
    ep: int,
    local_ptr: int = 0,
) -> Optional[dict]:
    """
    Read one entity.  Returns a dict or None to skip.
    Returned dict may contain {"_local": True} to signal local-player separation.
    """
    if not _valid(ep):
        return None
    try:
        # One block read covers every field offset we care about
        raw = _read(vmm, h, pid, ep, _ENTITY_BLOCK)
    except OSError:
        return None

    blk = Block(raw)

    # Class name for classification
    klass = blk.ptr(0x00)
    if not _valid(klass):
        return None
    try:
        name_ptr = _ptr(vmm, h, pid, klass + 0x10)
        cls_name = _cstr(vmm, h, pid, name_ptr)
    except Exception:
        return None
    if not cls_name:
        return None

    category = _classify(cls_name)
    if not category:
        return None

    # World position via transform hierarchy
    try:
        x, y, z = _xform.world_position(vmm, h, pid, ep)
    except Exception:
        return None

    pos  = {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}
    meta: dict = {}

    # ── Player / NPC ──────────────────────────────────────────────────────────
    if category.startswith("player"):
        flags      = blk.u32(FIELD_FLAGS)
        life_state = blk.u32(FIELD_LIFE_STATE)
        health     = blk.f32(FIELD_HEALTH)

        if life_state >= LS_WOUNDED:
            return None     # dead or wounded corpse — skip

        sleeping = bool(flags & FLAG_SLEEPING)

        if category == "player":
            if sleeping:
                category = "player-sleeping"

        meta["health"] = round(health, 1)
        meta["alive"]  = True

        if category not in ("player-npc",):
            name_obj    = blk.ptr(FIELD_DISPLAY_NAME)
            meta["name"] = _managed_str(vmm, h, pid, name_obj)

        if category == "player" and not sleeping:
            held_ptr = blk.ptr(FIELD_HELD_ENTITY)
            meta["held"] = _held_shortname(vmm, h, pid, held_ptr)
            # ammo: placeholder (requires magazine sub-object offsets from probe)
            meta["ammo_contents"] = 0
            meta["ammo_capacity"] = 0

        result = {"category": category, "position": pos, "meta": meta}
        if ep == local_ptr:
            result["_local"] = True
        return result

    # ── Bradley / helicopter health ───────────────────────────────────────────
    if category in ("bradley", "helicopter"):
        health = blk.f32(FIELD_HEALTH)
        meta["health"] = round(health, 1)
        return {"category": category, "position": pos, "meta": meta}

    # ── Locked crate timers ───────────────────────────────────────────────────
    if category == "locked-crate" and FIELD_HACK_SECONDS:
        meta["hackSeconds"]         = round(blk.f32(FIELD_HACK_SECONDS),         1)
        meta["requiredHackSeconds"] = round(blk.f32(FIELD_REQUIRED_HACK_SECONDS), 1)
        meta["isLootable"]          = bool(blk.u32(FIELD_IS_LOOTABLE))
        return {"category": category, "position": pos, "meta": meta}

    # ── Everything else (ores, loot, vehicles, etc.) ──────────────────────────
    return {"category": category, "position": pos, "meta": meta}


# ─── Entity list walk ─────────────────────────────────────────────────────────

def _find_local_ptr(vmm, h, pid: int, ptrs: list[int]) -> int:
    """
    Heuristic: scan for the entity with FLAG_CONNECTED set.
    In a typical DMA session the gaming PC's local player is the only entity
    with ReceivingSnapshot active.  Returns 0 if none found.
    """
    for ep in ptrs:
        if not _valid(ep):
            continue
        try:
            flags = _u32(vmm, h, pid, ep + FIELD_FLAGS)
            if flags & FLAG_CONNECTED:
                return ep
        except OSError:
            continue
    return 0


def walk_entities(vmm, h, pid: int) -> tuple[list[dict], Optional[dict]]:
    """Returns (entities_list, local_player_dict_or_None)."""
    try:
        count = _u32(vmm, h, pid, ENTITIES_PTR + LD_COUNT_OFF)
        if not (1 <= count <= 8192):
            return [], None

        vals_arr = _ptr(vmm, h, pid, ENTITIES_PTR + LD_VALS_OFF)
        raw      = _read(vmm, h, pid, vals_arr + LD_ELEM_BASE, count * LD_ELEM_SIZE)
        ptrs     = [struct.unpack_from("<Q", raw, i * 8)[0] for i in range(count)]

        local_ptr = _find_local_ptr(vmm, h, pid, ptrs)

        entities:     list[dict]      = []
        local_player: Optional[dict]  = None
        alive_ptrs:   set[int]        = set()

        for ep in ptrs:
            if not _valid(ep):
                continue
            alive_ptrs.add(ep)
            rec = read_entity(vmm, h, pid, ep, local_ptr=local_ptr)
            if rec is None:
                continue
            if rec.pop("_local", False):
                local_player = {
                    "position": rec["position"],
                    "health":   rec["meta"].get("health", 100.0),
                }
            else:
                entities.append(rec)

        _xform.evict(alive_ptrs)
        return entities, local_player

    except Exception as exc:
        print(f"[snap] walk_entities error: {exc}")
        return [], None


# ─── Snapshot writer ──────────────────────────────────────────────────────────

def _write_snapshot(state: dict, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(json.dumps(state, separators=(",", ":")).encode())
    tmp.replace(path)


def snapshot_loop(vmm, h, pid: int, path: Path, interval: float = 0.10) -> None:
    global _state
    ws = dma_config.world_size()
    while True:
        t0 = time.monotonic()
        entities, local_player = walk_entities(vmm, h, pid)
        state = {
            "ts":           time.time(),
            "world_size":   ws,
            "local_player": local_player,
            "entities":     entities,
        }
        with _lock:
            _state = state
        try:
            _write_snapshot(state, path)
        except OSError as exc:
            print(f"[snap] write error: {exc}")
        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, interval - elapsed))


# ─── Optional local debug HTTP endpoint ──────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        if self.path not in ("/data", "/data/"):
            self.send_response(404); self.end_headers(); return
        with _lock:
            payload = json.dumps(_state).encode()
        self.send_response(200)
        self.send_header("Content-Type",  "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)


# ─── Probe data loader ────────────────────────────────────────────────────────

def _h(v) -> int:
    return int(v, 16) if isinstance(v, str) else int(v)


def load_probe_data() -> None:
    global ENTITIES_PTR, GA_BASE
    global FIELD_HEALTH, FIELD_DISPLAY_NAME, FIELD_FLAGS, FIELD_LIFE_STATE
    global FIELD_HELD_ENTITY, FIELD_MAX_HEALTH, _ENTITY_BLOCK
    global FIELD_HACK_SECONDS, FIELD_REQUIRED_HACK_SECONDS, FIELD_IS_LOOTABLE

    path = Path(__file__).parent / "dump_fields.json"
    if not path.exists():
        raise FileNotFoundError(
            "dump_fields.json not found — run rust_probe.py first"
        )

    with open(path, encoding="utf-8") as f:
        d = json.load(f)

    GA_BASE      = _h(d["ga_base"])
    ENTITIES_PTR = _h(d["entity_info"]["entities_ptr"])

    fo = d.get("field_offsets", {})

    def off(key: str, default: int) -> int:
        return _h(fo[key]) if key in fo else default

    FIELD_HEALTH       = off("health",       0x218)
    FIELD_DISPLAY_NAME = off("display_name", 0x370)
    FIELD_FLAGS        = off("flags",        0x1C4)
    FIELD_LIFE_STATE   = off("life_state",   0x278)
    FIELD_HELD_ENTITY  = off("held_entity",  0x458)
    FIELD_MAX_HEALTH   = off("max_health",   0x21C)

    # Locked crate — optional
    FIELD_HACK_SECONDS          = off("hack_seconds",           0)
    FIELD_REQUIRED_HACK_SECONDS = off("required_hack_seconds",  0)
    FIELD_IS_LOOTABLE           = off("is_lootable",            0)

    # Recalculate block size after loading real offsets
    _ENTITY_BLOCK = max(
        FIELD_HEALTH + 4,
        FIELD_DISPLAY_NAME + 8,
        FIELD_FLAGS + 4,
        FIELD_LIFE_STATE + 4,
        FIELD_HELD_ENTITY + 8,
        0x500,
    )

    print(f"[snap] GA=0x{GA_BASE:016X}  entities_ptr=0x{ENTITIES_PTR:016X}")
    print(f"[snap] health=0x{FIELD_HEALTH:X}  display_name=0x{FIELD_DISPLAY_NAME:X}  "
          f"flags=0x{FIELD_FLAGS:X}  life_state=0x{FIELD_LIFE_STATE:X}  "
          f"held_entity=0x{FIELD_HELD_ENTITY:X}  block_size=0x{_ENTITY_BLOCK:X}")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Rust DMA entity snapshot loop")
    ap.add_argument("--local-server", action="store_true",
                    help="Serve a debug /data endpoint on :8765")
    ap.add_argument("--interval", type=float, default=0.10,
                    help="Snapshot interval in seconds (default 0.10 → 10 Hz)")
    args = ap.parse_args()

    load_probe_data()

    vmm = load_vmm()
    h   = init_vmm(vmm)
    pid = _find_pid(vmm, h, dma_config.process_name_bytes())
    print(f"[snap] attached  pid={pid}  process={dma_config.process_name()}")

    snap_path = dma_config.snapshot_path()
    print(f"[snap] output → {snap_path}  ({args.interval*1000:.0f} ms interval)")

    t = threading.Thread(
        target=snapshot_loop,
        args=(vmm, h, pid, snap_path, args.interval),
        daemon=True,
    )
    t.start()

    if args.local_server:
        srv = HTTPServer(("0.0.0.0", 8765), _Handler)
        print("[snap] debug http://localhost:8765/data")
        srv.serve_forever()
    else:
        print("[snap] running — Ctrl+C to stop")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("[snap] stopped")


if __name__ == "__main__":
    main()
