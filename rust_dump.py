#!/usr/bin/env python3 -I
"""
rust_dump.py — IL2CPP v39 field offset extractor for Rust (Unity 6, no external tools)

Usage:
    python -I rust_dump.py <global-metadata.dat> <GameAssembly.dll> <output.json>

Example (Rust on Steam):
    python -I rust_dump.py ^
      "C:\\...\\RustClient_Data\\il2cpp_data\\Metadata\\global-metadata.dat" ^
      "C:\\...\\GameAssembly.dll" ^
      dump_fields.json

Output: JSON dict  { "ClassName": { "fieldName": "0x1A0" | null, ... }, ... }

Confirmed offsets for Unity 6 / IL2CPP v39 / Rust build SHA256:
  dll      199a470f2510320be42d127cc55e946f51a2fc0b0b3ed0bf8671c2a8bb73b252
  metadata 266b023e647e1fcedd7c4590ea110d5131bbb070bf23f3c42fb33f935cc9750a

  global-metadata.dat header: stride=12, entry=(fileOffset, byteSize, recordCount)
  Section indices:  2=strings  11=fields  19=typeDefinitions  20=images
  Il2CppTypeDefinition (82 bytes):
    +0   nameIndex       int32
    +4   namespaceIndex  int32
    +26  fieldStart      int32
    +62  field_count     uint16
    +78  token           uint32
  Il2CppFieldDefinition (12 bytes): nameIndex(4)+typeIndex(4)+token(4)
  MetadataRegistration RVA: 0x0DBED230 (located by Cpp2IL for this exact DLL)
    +80  numFieldOffsets  int64
    +88  fieldOffsets**   ptr64

  Values of 0x67676767 are static/special fields with no instance offset.
"""
import struct, json, sys, hashlib
from pathlib import Path

MAGIC   = 0xFAB11BAF
VERSION = 39

TARGET_CLASSES = {
    'BasePlayer', 'BaseCombatEntity', 'BaseNetworkable',
    'OreResourceEntity', 'LootContainer', 'BradleyAPC',
    'BaseHelicopter', 'SupplyDrop', 'CollectibleEntity', 'BaseVehicle',
    'HackableLockedCrate', 'LockedByEntCrate',
    'BaseMagazine', 'BaseProjectile',
    'PlayerInventory', 'ItemContainer', 'Item', 'ItemDefinition',
    'BaseAnimalNPC', 'DroppedItem',
}

# ── Confirmed RVA from Cpp2IL (this exact Rust build) ──────────────────────
# If the DLL hash changes (game update), update this via Cpp2IL and push.
KNOWN_REGISTRATION_RVA = 0x0DBED230

# ── Helpers ──────────────────────────────────────────────────────────────────
def _u16(d, o): return struct.unpack_from('<H', d, o)[0]
def _i32(d, o): return struct.unpack_from('<i', d, o)[0]
def _u32(d, o): return struct.unpack_from('<I', d, o)[0]
def _u64(d, o): return struct.unpack_from('<Q', d, o)[0]
def _i64(d, o): return struct.unpack_from('<q', d, o)[0]

def _cstr(d, o, limit=None):
    end = d.index(b'\x00', o, o + (limit or 512))
    return d[o:end].decode('utf-8', 'replace')

def _looks_like_name(s):
    return bool(s) and len(s) < 128 and all(
        c.isascii() and (c.isalnum() or c in '._<>[]`#@$%') for c in s)

# ── Phase 1: Parse global-metadata.dat ───────────────────────────────────────
# Header entry layout (v27+ / Unity 6):
#   byte +0 : int32  fileOffset
#   byte +4 : int32  byteSize      ← NOT record count
#   byte +8 : int32  recordCount
SECT_STRIDE = 12

def _section(d, idx):
    base = 8 + idx * SECT_STRIDE
    return _i32(d, base), _i32(d, base + 4), _i32(d, base + 8)


def parse_metadata(path):
    d = Path(path).read_bytes()
    magic = _u32(d, 0)
    ver   = _i32(d, 4)
    if magic != MAGIC:
        raise ValueError(f"Not IL2CPP metadata (magic={magic:#010x})")
    if ver != VERSION:
        print(f"[meta] Warning: expected v39, got v{ver} — offsets may differ")
    else:
        print(f"[meta] metadata version = {ver}")

    str_off,  str_sz,  _        = _section(d, 2)   # UTF-8 string blob
    fld_off,  fld_sz,  fld_cnt  = _section(d, 11)  # Il2CppFieldDefinition[]
    typ_off,  typ_sz,  typ_cnt  = _section(d, 19)  # Il2CppTypeDefinition[]
    img_off,  img_sz,  img_cnt  = _section(d, 20)  # Il2CppImageDefinition[]

    td_size = typ_sz // typ_cnt if typ_cnt else 82
    print(f"[meta] td_size={td_size}  typ_cnt={typ_cnt}  fld_cnt={fld_cnt}")

    # Confirmed offsets inside Il2CppTypeDefinition (82-byte struct):
    #   +0  nameIndex     int32
    #   +4  namespaceIndex int32
    #   +26 fieldStart    int32
    #   +62 field_count   uint16
    #   +78 token         uint32
    if td_size != 82:
        print(f"[meta] Warning: expected 82-byte typedef, got {td_size} — using probed offsets")

    NAME_OFF      = 0
    NS_OFF        = 4
    # fieldStart and field_count offsets: confirmed 26/62 for 82-byte struct,
    # probe alternatives for other sizes.
    if td_size == 82:
        FS_OFF = 26;  FC_OFF = 62
    elif td_size == 92:
        FS_OFF = 36;  FC_OFF = 72    # classic v27 layout
    elif td_size == 96:
        FS_OFF = 36;  FC_OFF = 72
    else:
        FS_OFF = 26;  FC_OFF = 62    # best guess for newer structs

    def string(idx):
        if not (0 <= idx < str_sz):
            return ''
        try:
            return _cstr(d, str_off + idx, str_sz - idx)
        except Exception:
            return ''

    # Find Assembly-CSharp.dll image to restrict search to game types only.
    # Il2CppImageDefinition (36 bytes): nameIndex(4)+customAttributeIndex(4)+firstTypeIndex(4)+count(4)+...
    game_range = None
    IMG_SIZE = 36
    for i in range(img_cnt):
        img_base = img_off + i * IMG_SIZE
        img_name_idx = _i32(d, img_base)
        img_name = string(img_name_idx)
        if img_name == 'Assembly-CSharp.dll':
            first_type = _i32(d, img_base + 8)   # Il2CppImageDefinition.firstTypeIndex
            type_count = _i32(d, img_base + 12)   # Il2CppImageDefinition.typeCount
            # Unity 6 / newer: might be uint16 firstTypeIndex in a different position
            # Try to validate
            if 0 < type_count <= typ_cnt and 0 <= first_type < typ_cnt:
                game_range = range(first_type, first_type + type_count)
                print(f"[meta] Assembly-CSharp: types {first_type}..{first_type+type_count-1}")
                break
            # Try alternative layout: firstTypeIndex at +12 as uint32
            first_type2 = _u32(d, img_base + 12)
            type_count2  = _i32(d, img_base + 16)
            if 0 < type_count2 <= typ_cnt and 0 <= first_type2 < typ_cnt:
                game_range = range(first_type2, first_type2 + type_count2)
                print(f"[meta] Assembly-CSharp (alt layout): types {first_type2}..{first_type2+type_count2-1}")
                break

    if game_range is None:
        print("[meta] Warning: Assembly-CSharp.dll image not found — scanning all types")
        game_range = range(typ_cnt)

    type_info = {}
    for i in game_range:
        base = typ_off + i * td_size
        if base + td_size > len(d):
            break
        name = string(_i32(d, base + NAME_OFF))
        if not name or name not in TARGET_CLASSES:
            continue
        ns = string(_i32(d, base + NS_OFF))
        if ns:   # skip same-named types in other namespaces
            continue

        field_start = _i32(d, base + FS_OFF)
        field_count = _u16(d, base + FC_OFF)
        full = f"{name}"
        print(f"[meta] {full}  typeIdx={i}  fieldStart={field_start}  fieldCount={field_count}")

        fields = []
        for j in range(field_count):
            fb = fld_off + (field_start + j) * 12
            if fb + 12 > len(d):
                break
            fname = string(_i32(d, fb)) or f'?f{j}'
            fields.append({'name': fname, 'global_idx': field_start + j})
        print(f"       fields[{len(fields)}]: {[f['name'] for f in fields[:15]]}"
              + ("..." if len(fields) > 15 else ""))

        type_info[name] = {
            'type_idx':    i,
            'field_start': field_start,
            'field_count': field_count,
            'fields':      fields,
        }

    return type_info, len(d)   # return metadata bytes length for DLL phase

# ── Phase 2: Extract field offsets from GameAssembly.dll ──────────────────────
def _pe_sections(d):
    e_lfanew = _u32(d, 0x3C)
    assert _u32(d, e_lfanew) == 0x4550, "Not a PE file"
    coff = e_lfanew + 4
    num_sec  = _u16(d, coff + 2)
    opt_size = _u16(d, coff + 16)
    image_base = _u64(d, coff + 20 + 24)
    sect_base  = coff + 20 + opt_size
    secs = {}
    for i in range(num_sec):
        s = sect_base + i * 40
        name     = d[s:s+8].rstrip(b'\x00').decode('ascii', 'replace')
        vaddr    = _u32(d, s + 12)
        raw_off  = _u32(d, s + 20)
        raw_size = _u32(d, s + 16)
        secs[name] = (vaddr, raw_off, raw_size)
    return secs, image_base

def _va_to_foff(va, secs):
    for _, (sv, sf, ss) in secs.items():
        if sv <= va < sv + ss:
            return sf + (va - sv)
    return None


def _read_field_offsets_at_rva(d, secs, image_base, reg_rva, type_info):
    """
    Read field offsets using the MetadataRegistration at the given RVA.
    Registration layout at +80:
      +80  numFieldOffsets  int64 (should == total type count)
      +88  fieldOffsets**   ptr64  → array of ptr64, one per type index
    """
    reg_va = image_base + reg_rva
    reg_fo = _va_to_foff(reg_va, secs)
    if reg_fo is None or reg_fo + 96 > len(d):
        return False

    num_fo  = _i64(d, reg_fo + 80)
    table_va = _u64(d, reg_fo + 88)
    table_fo = _va_to_foff(table_va, secs)

    if not (1_000 < num_fo < 2_000_000) or table_fo is None:
        return False

    print(f"[dll]  registration VA={reg_va:#x}  fieldOffsets[{num_fo}]  table VA={table_va:#x}")

    found = 0
    for cls, info in type_info.items():
        tidx   = info['type_idx']
        if tidx >= num_fo:
            continue
        ptr_fo = table_fo + tidx * 8
        if ptr_fo + 8 > len(d):
            continue
        arr_va = _u64(d, ptr_fo)
        if not arr_va:
            continue
        arr_fo = _va_to_foff(arr_va, secs)
        if arr_fo is None:
            continue

        fc = info['field_count']
        offsets = []
        for k in range(fc):
            if arr_fo + k * 4 + 4 > len(d):
                break
            offsets.append(_i32(d, arr_fo + k * 4))
        info['raw_offsets'] = offsets
        valid = sum(1 for o in offsets if 0x08 <= o <= 0x4000)
        print(f"[dll]    {cls}: {sum(1 for o in offsets if o >= 0)}/{fc} non-null  "
              f"({valid} in plausible range)  sample={[hex(o) for o in offsets[:6]]}")
        if valid > 0 or fc == 0:
            found += 1

    if found > 0:
        print(f"[dll]  Resolved offsets for {found}/{len(type_info)} classes")
    return found > 0


def scan_dll(dll_path, type_info):
    d = Path(dll_path).read_bytes()
    secs, image_base = _pe_sections(d)
    print(f"[dll]  image_base = {image_base:#x}")
    print(f"[dll]  sections   = {list(secs.keys())}")

    # Try the confirmed RVA first (fast path, no scanning needed)
    if KNOWN_REGISTRATION_RVA:
        print(f"[dll]  Trying known RVA {KNOWN_REGISTRATION_RVA:#x}")
        if _read_field_offsets_at_rva(d, secs, image_base,
                                      KNOWN_REGISTRATION_RVA, type_info):
            return type_info

    # Fallback: scan .text for LEA rcx,[RIP+disp32] candidates
    print("[dll]  Known RVA failed — scanning .text for MetadataRegistration")
    text_vaddr, text_foff, text_size = secs.get('.text', (0, 0, 0))
    LEA = b'\x48\x8D\x0D'
    text_data = d[text_foff:text_foff + text_size]
    max_type_idx = max((info['type_idx'] for info in type_info.values()), default=0)

    pos = 0
    while True:
        idx = text_data.find(LEA, pos)
        if idx == -1:
            break
        pos = idx + 1
        rip  = text_vaddr + idx + 7
        disp = struct.unpack_from('<i', text_data, idx + 3)[0]
        rva  = (rip + disp) - image_base

        fo = _va_to_foff(rip + disp, secs)
        if fo is None or fo + 96 > len(d):
            continue
        num_generic = _i32(d, fo + 0)
        num_types   = _i32(d, fo + 32)
        if not (0 < num_generic < 500_000) or not (max_type_idx < num_types < 1_000_000):
            continue

        print(f"[dll]  Trying candidate RVA {rva:#x}  numGenericTypes={num_generic}  numTypes={num_types}")
        if _read_field_offsets_at_rva(d, secs, image_base, rva, type_info):
            print(f"[dll]  ** Update KNOWN_REGISTRATION_RVA = {rva:#x} in this script")
            return type_info

    print("[dll]  Warning: MetadataRegistration not found — offsets will be null.")
    print("         Re-run Cpp2IL to get a fresh REGISTRATION_RVA and update this script.")
    return type_info

# ── Phase 3: Build output JSON ────────────────────────────────────────────────
def build_output(type_info):
    out = {}
    for cls, info in type_info.items():
        raw = info.get('raw_offsets', [])
        fields_out = {}
        for i, fdef in enumerate(info.get('fields', [])):
            off = raw[i] if i < len(raw) else None
            if off is None:
                fields_out[fdef['name']] = None
            elif off == 0x67676767:
                fields_out[fdef['name']] = 'static'   # static/special — no instance offset
            elif off >= 0:
                fields_out[fdef['name']] = hex(off)
            else:
                fields_out[fdef['name']] = None
        out[cls] = fields_out
    return out

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    meta_path, dll_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

    print("=" * 60)
    print("Phase 1: Parse global-metadata.dat")
    print("=" * 60)
    type_info, _meta_len = parse_metadata(meta_path)
    print(f"\n  → found {len(type_info)} target classes in metadata")

    if not type_info:
        print("\nERROR: No target classes found.")
        sys.exit(2)

    print()
    print("=" * 60)
    print("Phase 2: Scan GameAssembly.dll for field offsets")
    print("=" * 60)
    type_info = scan_dll(dll_path, type_info)

    print()
    print("=" * 60)
    print("Phase 3: Write output")
    print("=" * 60)
    result = build_output(type_info)
    Path(out_path).write_text(json.dumps(result, indent=2))
    print(f"Written: {out_path}")

    total_fields = sum(len(v) for v in result.values())
    resolved     = sum(1 for cls in result.values()
                       for v in cls.values() if v and v != 'static')
    static_count = sum(1 for cls in result.values()
                       for v in cls.values() if v == 'static')
    print(f"\nSummary: {resolved} instance + {static_count} static / {total_fields} total "
          f"across {len(result)} classes")
    for cls, fields in sorted(result.items()):
        ok     = sum(1 for v in fields.values() if v and v != 'static')
        static = sum(1 for v in fields.values() if v == 'static')
        total  = len(fields)
        status = (f"OK ({ok})" if ok == total - static
                  else f"{ok} instance + {static} static / {total}" if ok
                  else f"FAILED (0 instance offsets)")
        print(f"  {cls:35s} {status}")


if __name__ == '__main__':
    main()
