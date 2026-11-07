"""
Open FPGA once, find Among Us.exe PID, check GameAssembly.dll build.
Prints KEY=VALUE lines for the launcher to parse.
Exit 0 = found + build OK or new baseline saved
Exit 1 = build mismatch
Exit 2 = Among Us not found
Exit 3 = FPGA error
"""
import ctypes as ct
import json
import os
import struct
import sys
from pathlib import Path

DLL = (Path.home() / "Downloads"
       / "PCILeech_files_and_binaries_v4.20.0-win_x64-20261014"
       / "vmm.dll")
CONFIG = Path(__file__).parent / "skeld_config.json"

P, D, Q = ct.c_void_p, ct.c_uint32, ct.c_uint64

def init_vmm():
    os.add_dll_directory(str(DLL.parent))
    v = ct.CDLL(str(DLL))
    v.VMMDLL_Initialize.argtypes = [D, ct.POINTER(ct.c_char_p)]
    v.VMMDLL_Initialize.restype = P
    v.VMMDLL_Close.argtypes = [P]; v.VMMDLL_Close.restype = None
    v.VMMDLL_ProcessGetModuleBaseU.argtypes = [P, D, ct.c_char_p]
    v.VMMDLL_ProcessGetModuleBaseU.restype = Q
    v.VMMDLL_MemReadEx.argtypes = [P, D, Q, P, D, ct.POINTER(D), Q]
    v.VMMDLL_MemReadEx.restype = ct.c_int
    # PidGetFromName may or may not be exported; try it
    try:
        v.VMMDLL_PidGetFromName.argtypes = [P, ct.c_char_p, ct.POINTER(D)]
        v.VMMDLL_PidGetFromName.restype = ct.c_int
    except AttributeError:
        v.VMMDLL_PidGetFromName = None
    args = (ct.c_char_p * 4)(b"", b"-device", b"fpga", b"-printf")
    h = v.VMMDLL_Initialize(len(args), args)
    if not h:
        raise RuntimeError("FPGA init failed")
    return v, h

def read_mem(v, h, pid, address, fmt):
    n = struct.calcsize(fmt)
    buf, got = ct.create_string_buffer(n), D()
    v.VMMDLL_MemReadEx(h, pid, address, buf, n, ct.byref(got), 0x11)
    return struct.unpack(fmt, buf.raw)

def find_pid(v, h):
    if v.VMMDLL_PidGetFromName:
        pid_out = D(0)
        ok = v.VMMDLL_PidGetFromName(h, b"Among Us.exe", ct.byref(pid_out))
        if ok and pid_out.value:
            return pid_out.value
    return None

def pe_timestamp(v, h, pid, base):
    pe_off, = read_mem(v, h, pid, base + 0x3C, "<I")
    ts,     = read_mem(v, h, pid, base + pe_off + 8, "<I")
    size,   = read_mem(v, h, pid, base + pe_off + 0x50, "<I")
    return ts, size

def main():
    try:
        v, h = init_vmm()
    except RuntimeError as e:
        print(f"ERROR={e}")
        sys.exit(3)

    try:
        pid = find_pid(v, h)
        if pid is None:
            print("ERROR=Among Us.exe not found in process list")
            sys.exit(2)

        base = v.VMMDLL_ProcessGetModuleBaseU(h, pid, b"GameAssembly.dll")
        if not base:
            print(f"PID={pid}")
            print("ERROR=GameAssembly.dll not found for that PID")
            sys.exit(2)

        ts, size = pe_timestamp(v, h, pid, base)
        print(f"PID={pid}")
        print(f"TS={ts:#010x}")
        print(f"SIZE={size:#010x}")

        # Compare with saved baseline
        if CONFIG.exists():
            saved = json.loads(CONFIG.read_text(encoding="utf-8"))
            if saved.get("pe_timestamp") == ts and saved.get("size_of_image") == size:
                print("BUILD=OK")
                sys.exit(0)
            else:
                print(f"BUILD=MISMATCH  (saved TS={saved.get('pe_timestamp', '?'):#010x})")
                sys.exit(1)
        else:
            # First run — save baseline
            CONFIG.write_text(
                json.dumps({"pe_timestamp": ts, "size_of_image": size}, indent=2),
                encoding="utf-8")
            print("BUILD=NEW_BASELINE  (saved for future runs)")
            sys.exit(0)
    finally:
        v.VMMDLL_Close(h)

if __name__ == "__main__":
    main()
