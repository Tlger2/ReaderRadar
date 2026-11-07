"""Publish the local Freeplay character's validated transform to ReaderRadar."""
import ctypes as ct
import json
import math
import os
from pathlib import Path
import struct
import sys
import time


def world_position(read, ptr, native):
    hierarchy = ptr(native + 0x38, "hierarchy")
    index, = read(native + 0x40, "<i", "transform index")
    records = ptr(hierarchy + 0x18, "transform records")
    parents = ptr(hierarchy + 0x20, "parent indices")
    if not 0 <= index < 65536:
        raise RuntimeError(f"Invalid transform index {index}")
    x, y, z = read(records + index * 0x30, "<3f", "local translation")
    seen = {index}
    for _ in range(64):
        parent, = read(parents + index * 4, "<i", "parent index")
        if parent < 0:
            if not all(map(math.isfinite, (x, y, z))):
                raise RuntimeError("Non-finite world position")
            return x, y, z
        if parent >= 65536 or parent in seen:
            raise RuntimeError(f"Invalid or repeated parent index {parent}")
        seen.add(parent)
        tx, ty, tz, _, qx, qy, qz, qw, sx, sy, sz, _ = read(
            records + parent * 0x30, "<12f", "parent transform")
        x, y, z = sx*x, sy*y, sz*z
        a, b, c = 2*(qy*z-qz*y), 2*(qz*x-qx*z), 2*(qx*y-qy*x)
        x, y, z = (tx+x+qw*a+qy*c-qz*b, ty+y+qw*b+qz*a-qx*c,
                   tz+z+qw*c+qx*b-qy*a)
        index = parent
    raise RuntimeError("Transform hierarchy exceeded 64 parents")


TASK_CATEGORY = {
    "DivertPowerTask": "task-divert-power",
    "UploadDataTask":  "task-upload-data",
    "CleanO2Task":     "task-clean-o2",
    "StartReactor":    "task-start-reactor",
}

# TaskTypes enum value → list of world (x, y) positions for that console task.
# Captured from ShipStatus.AllConsoles via DMA probe on Skeld Freeplay.
# NormalPlayerTask.TaskType is at managed offset +0x34.
NORMAL_TASK_CONSOLES = {
    0:  [(-7.328, -5.248)],                                              # SubmitScan
    1:  [(7.526, -13.981)],                                              # PrimeShields
    4:  [(-21.792, -5.573)],                                             # StartReactor
    5:  [(5.635, -8.633)],                                               # SwipeCard
    6:  [(9.088, 1.812)],                                                # ClearAsteroids
    7:  [(2.508, -6.265)],                                               # UploadData
    8:  [(-6.144, -4.260)],                                              # InspectSample
    11: [(-19.151, -12.618), (-19.176, -0.414)],                        # AlignEngineOutput
    12: [(1.392, -6.409), (-5.281, 5.246), (14.52, -3.774),
         (-7.728, -7.667), (-1.932, -8.72), (-15.559, -4.592)],         # FixWiring
    13: [(-5.864, -7.477)],                                              # EnterIdCode
    14: [(-9.000, -7.294)],                                              # CleanO2Filter
    15: [(-22.536, -2.500)],                                             # UnlockManifolds
    16: [(-21.276, -1.337), (-21.288, -9.017)],                         # FixLights
    17: [(-9.792, -9.899)],                                              # DivertPower
    18: [(5.743, -2.977)],                                               # StartFans
    20: [(6.454, -6.408), (6.712, -2.832)],                              # ProcessData
    21: [(18.732, -4.728)],                                              # RunDiagnostics
}

NORMAL_TASK_CATEGORY = {
    0:  "task-submit-scan",
    1:  "task-prime-shields",
    4:  "task-start-reactor",
    5:  "task-swipe-card",
    6:  "task-clear-asteroids",
    7:  "task-upload-data",
    8:  "task-inspect-sample",
    11: "task-align-engine",
    12: "task-fix-wiring",
    13: "task-enter-id-code",
    14: "task-clean-o2",
    15: "task-unlock-manifolds",
    16: "task-fix-lights",
    17: "task-divert-power",
    18: "task-start-fans",
    20: "task-process-data",
    21: "task-run-diagnostics",
}


def publish(path, x, y, entities=()):
    payload = {"targetId": "among-us-freeplay-skeld",
               "player": {"position": {"x": x, "y": 0, "z": y}},
               "entities": list(entities)}
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    os.replace(temp, path)


def _read_klass_name(read, klass_ptr):
    """Return Il2CppClass name string, or '' on any failure."""
    try:
        name_ptr, = read(klass_ptr + 0x10, "<Q", "klass name ptr")
        if not 0x10000 <= name_ptr < 0x800000000000:
            return ""
        parts = []
        for chunk in range(5):
            raw = read(name_ptr + chunk * 8, "<8s", "name chunk")[0]
            if b"\x00" in raw:
                parts.append(raw[:raw.index(b"\x00")].decode("ascii", errors="replace"))
                break
            parts.append(raw.decode("ascii", errors="replace"))
        return "".join(parts)
    except (RuntimeError, struct.error):
        return ""


def _task_world_position(read, ptr_fn, task_obj):
    """Return (x, y, z) world position of a task game object, or raise RuntimeError."""
    comp,  = read(task_obj + 0x10, "<Q", "task comp")
    go,    = read(comp + 0x30,     "<Q", "task go")
    cs,    = read(go + 0x30,       "<Q", "task cs")
    te,    = read(cs + 0x08,       "<Q", "task te")
    for p in (comp, go, cs, te):
        if not (0x10000 <= p < 0x800000000000):
            raise RuntimeError(f"bad task pointer 0x{p:X}")
    tag, = read(te + 0x20, "<I", "task te tag")
    if tag == 2:
        wrapper, = read(te + 0x28, "<Q", "task wrapper")
        if not (0x10000 <= wrapper < 0x800000000000):
            raise RuntimeError("bad task wrapper")
        native = ptr_fn(wrapper + 0x10, "task native")
    else:
        native = te
    return world_position(read, ptr_fn, native)


def _read_task_entities(read, ptr_fn, player):
    """
    Read myTasks list from the local player and return entity dicts for tasks
    whose game objects sit at a fixed map position (not NormalPlayerTask).
    """
    entities = []
    try:
        my_tasks_ptr, = read(player + 0x100, "<Q", "myTasks ptr")
        if not (0x10000 <= my_tasks_ptr < 0x800000000000):
            return entities
        items_arr, = read(my_tasks_ptr + 0x10, "<Q", "task items arr")
        count, = read(my_tasks_ptr + 0x18, "<i", "task count")
        if not (0 < count <= 20 and 0x10000 <= items_arr < 0x800000000000):
            return entities
    except (RuntimeError, struct.error):
        return entities

    for i in range(count):
        try:
            task_obj, = read(items_arr + 0x20 + i * 8, "<Q", f"task[{i}]")
            if not (0x10000 <= task_obj < 0x800000000000):
                continue
            klass_ptr, = read(task_obj, "<Q", f"task[{i}] klass")
            if not (0x10000 <= klass_ptr < 0x800000000000):
                continue
            kname = _read_klass_name(read, klass_ptr)
            if not kname:
                continue
            # NormalPlayerTask positions come from ShipStatus.AllConsoles, not from
            # the task game object (which follows the player).
            if kname == "NormalPlayerTask":
                try:
                    task_type, = read(task_obj + 0x34, "<i",
                                      f"task[{i}] NormalPlayerTask type")
                    positions = NORMAL_TASK_CONSOLES.get(task_type, [])
                    category = NORMAL_TASK_CATEGORY.get(
                        task_type, f"task-normal-{task_type}")
                    for px, py in positions:
                        entities.append(
                            {"category": category,
                             "position": {"x": round(float(px), 3),
                                          "y": 0,
                                          "z": round(float(py), 3)}})
                except (RuntimeError, struct.error):
                    pass
                continue
            category = TASK_CATEGORY.get(kname, f"task-{kname.lower()}")
            tx, ty, tz = _task_world_position(read, ptr_fn, task_obj)
            if not all(map(math.isfinite, (tx, ty, tz))):
                continue
            # Skip tasks hidden off-screen (y < -15 means hidden minigame)
            if ty < -15:
                continue
            entities.append({"category": category,
                              "position": {"x": round(float(tx), 3),
                                           "y": 0,
                                           "z": round(float(ty), 3)}})
        except (RuntimeError, struct.error):
            continue
    return entities


def main(pid, path, dll):
    directory = os.add_dll_directory(str(dll.parent))
    v = ct.CDLL(str(dll))
    P, D, Q = ct.c_void_p, ct.c_uint32, ct.c_uint64
    v.VMMDLL_Initialize.argtypes = [D, ct.POINTER(ct.c_char_p)]
    v.VMMDLL_Initialize.restype = P
    v.VMMDLL_Close.argtypes = [P]
    v.VMMDLL_Close.restype = None
    v.VMMDLL_ProcessGetModuleBaseU.argtypes = [P, D, ct.c_char_p]
    v.VMMDLL_ProcessGetModuleBaseU.restype = Q
    v.VMMDLL_MemReadEx.argtypes = [P, D, Q, P, D, ct.POINTER(D), Q]
    v.VMMDLL_MemReadEx.restype = ct.c_int
    args = (ct.c_char_p * 4)(b"", b"-device", b"fpga", b"-printf")
    handle = v.VMMDLL_Initialize(len(args), args)
    if not handle:
        directory.close()
        raise RuntimeError("Could not open FPGA or initialize target memory view")

    def read(address, fmt, label):
        size = struct.calcsize(fmt)
        buf, got = ct.create_string_buffer(size), D()
        ok = v.VMMDLL_MemReadEx(handle, pid, address, buf, size, ct.byref(got), 0x11)
        if not ok or got.value != size:
            raise RuntimeError(f"{label}: read failed ({got.value}/{size} bytes)")
        return struct.unpack(fmt, buf.raw)

    def ptr(address, label):
        value, = read(address, "<Q", label)
        if not 0x10000 <= value < 0x800000000000 or value % 8:
            raise RuntimeError(f"{label}: unexpected pointer 0x{value:X}")
        return value

    try:
        base = v.VMMDLL_ProcessGetModuleBaseU(handle, pid, b"GameAssembly.dll")
        if not base:
            raise RuntimeError(f"GameAssembly.dll not found for PID {pid}")
        print(f"Watch local snapshot: {path}", flush=True)
        failures = 0
        while True:
            try:
                klass = ptr(base + 0x03431C20, "PlayerControl class")
                storage = ptr(klass + 0xB8, "static storage")
                player = ptr(storage, "LocalPlayer")
                component = ptr(player + 0x10, "native Component")
                obj = ptr(component + 0x30, "native GameObject")
                entries = ptr(obj + 0x30, "component storage")
                entry = ptr(entries + 0x08, "Transform entry")
                tag, = read(entry + 0x20, "<I", "Transform entry tag")
                if tag != 2:
                    raise RuntimeError(f"Unsupported Transform entry tag {tag}")
                wrapper = ptr(entry + 0x28, "managed Transform")
                native = ptr(wrapper + 0x10, "native Transform")
                x, y, _ = world_position(read, ptr, native)
                entities = _read_task_entities(read, ptr, player)
                publish(path, x, y, entities)
                failures = 0
                entity_count = len(entities)
                print(f"Snapshot saved: X={x:.4f} Y={y:.4f}  tasks={entity_count}", flush=True)
            except (RuntimeError, OSError) as exc:
                failures += 1
                print(f"Update skipped: {exc}", flush=True)
                if failures >= 5:
                    raise RuntimeError("Stopped after five consecutive update failures") from exc
            time.sleep(1)
    finally:
        v.VMMDLL_Close(handle)
        directory.close()


if __name__ == "__main__":
    dll = Path.home() / "Downloads" / "PCILeech_files_and_binaries_v4.20.0-win_x64-20261014" / "vmm.dll"
    try:
        main(int(sys.argv[1]), Path(sys.argv[2]), dll)
    except KeyboardInterrupt:
        print("Snapshot feed stopped.")
    except (OSError, RuntimeError) as exc:
        raise SystemExit(str(exc))
