"""
Shared runtime config for ReaderRadar DMA tools.

All paths and names can be overridden with environment variables so the
same scripts work across machines without editing source.

  MEMPROCFS_DIR     Directory that contains vmm.dll (and related PCILeech files)
  VMM_DLL_PATH      Full path to vmm.dll (overrides MEMPROCFS_DIR)
  RUST_PROCESS      Game process name (default: RustClient.exe)
  SNAPSHOT_PATH     Live JSON file written by the snapshot loop
  WEB_RADAR_URL     Base URL of the FastAPI server (default: http://localhost:8080)
  WEB_RADAR_KEY     X-Push-Key for /push
  VMM_DEVICE        MemProcFS -device argument (default: fpga)
"""

from __future__ import annotations

import os
from pathlib import Path

# ─── MemProcFS / PCILeech ─────────────────────────────────────────────────────

def vmm_dll_path() -> Path:
    """Resolve vmm.dll from env or common install locations."""
    explicit = os.environ.get("VMM_DLL_PATH")
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise FileNotFoundError(f"VMM_DLL_PATH set but not a file: {p}")

    base = os.environ.get("MEMPROCFS_DIR")
    if base:
        p = Path(base) / "vmm.dll"
        if p.is_file():
            return p
        raise FileNotFoundError(f"MEMPROCFS_DIR set but vmm.dll missing: {p}")

    # Common layouts (newest first)
    candidates = [
        Path.home() / "Downloads" / "PCILeech_files_and_binaries_v4.20.0-win_x64-20261014" / "vmm.dll",
        Path.home() / "Downloads" / "MemProcFS" / "vmm.dll",
        Path.home() / "Tools" / "MemProcFS" / "vmm.dll",
        Path("C:/MemProcFS") / "vmm.dll",
        Path("C:/PCILeech") / "vmm.dll",
    ]
    for p in candidates:
        if p.is_file():
            return p

    searched = "\n  ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        "vmm.dll not found. Set VMM_DLL_PATH or MEMPROCFS_DIR.\n"
        f"Searched:\n  {searched}"
    )


def vmm_device() -> str:
    return os.environ.get("VMM_DEVICE", "fpga")


def process_name() -> str:
    return os.environ.get("RUST_PROCESS", "RustClient.exe")


def process_name_bytes() -> bytes:
    return process_name().encode("ascii", errors="replace")


# ─── Snapshot / web ───────────────────────────────────────────────────────────

def snapshot_path() -> Path:
    explicit = os.environ.get("SNAPSHOT_PATH")
    if explicit:
        return Path(explicit)
    temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")
    return temp / "ReaderRadar-Rust-live.json"


def web_radar_url() -> str:
    return os.environ.get("WEB_RADAR_URL", "http://localhost:8080").rstrip("/")


def web_radar_key() -> str:
    return os.environ.get("WEB_RADAR_KEY", "")


def world_size() -> int:
    """Server map size in metres. Set RUST_WORLD_SIZE to match the actual server."""
    return int(os.environ.get("RUST_WORLD_SIZE", "3500"))
