#!/usr/bin/env python3
"""Parse a Mach-O relocatable object (arm64e kext .o) into the JSON shapes
resolve.py expects: obj_sections.json, obj_symtab.json, obj_relocs.json.
Hand-decoded, no macholib -- same approach used earlier this session,
rewritten because the original script wasn't saved outside this session's
scratch. Field layouts cross-checked against `nm -m`/`otool -lv`/`otool -r`
output on the same object before trusting this.
"""
import struct
import json
import sys
import subprocess

OBJ = sys.argv[1] if len(sys.argv) > 1 else "obj/InfernoVGPUHello.o"


def main():
    with open(OBJ, "rb") as f:
        data = f.read()

    magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved = \
        struct.unpack_from("<IiiIIIII", data, 0)
    assert magic == 0xfeedfacf, f"not a 64-bit Mach-O object: {magic:#x}"

    sections = []
    symtab_off = symtab_n = str_off = str_size = None

    off = 32
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == 0x19:  # LC_SEGMENT_64
            segname, vmaddr, vmsize, fileoff, filesize, maxprot, initprot, nsects, sflags = \
                struct.unpack_from("<16sQQQQiiII", data, off + 8)
            sect_off = off + 8 + 16 + 8 + 8 + 8 + 8 + 4 + 4 + 4 + 4
            for i in range(nsects):
                so = sect_off + i * 80
                sectname, segname2, addr, size, offset, align, reloff, nreloc, sflags2, r1, r2, r3 = \
                    struct.unpack_from("<16s16sQQIIIIIIII", data, so)
                sections.append({
                    "sectname": sectname.rstrip(b"\x00").decode(),
                    "segname": segname2.rstrip(b"\x00").decode(),
                    "addr": addr,
                    "size": size,
                    "offset": offset,
                    "reloff": reloff,
                    "nreloc": nreloc,
                })
        elif cmd == 0x2:  # LC_SYMTAB
            symoff, nsyms, stroff, strsize = struct.unpack_from("<IIII", data, off + 8)
            symtab_off, symtab_n, str_off, str_size = symoff, nsyms, stroff, strsize
        off += cmdsize

    symtab = []
    for i in range(symtab_n):
        so = symtab_off + i * 16
        n_strx, n_type, n_sect, n_desc, n_value = struct.unpack_from("<IBBHQ", data, so)
        name_off = str_off + n_strx
        end = data.find(b"\x00", name_off)
        name = data[name_off:end].decode()
        symtab.append({
            "idx": i,
            "name": name,
            "type": n_type,
            "sect": n_sect,
            "desc": n_desc,
            "value": n_value,
        })

    relocs = {"text": [], "const": [], "init": [], "term": []}
    sec_by_name = {s["sectname"]: s for s in sections}
    for secname, key in (("__text", "text"), ("__const", "const"),
                          ("__mod_init_func", "init"), ("__mod_term_func", "term")):
        sec = sec_by_name.get(secname)
        if not sec or sec["nreloc"] == 0:
            continue
        for i in range(sec["nreloc"]):
            ro = sec["reloff"] + i * 8
            r_address, packed = struct.unpack_from("<Ii", data, ro)
            upacked = packed & 0xFFFFFFFF
            r_symbolnum = upacked & 0xFFFFFF
            r_pcrel = (upacked >> 24) & 1
            r_length = (upacked >> 25) & 3
            r_extern = (upacked >> 27) & 1
            r_type = (upacked >> 28) & 0xF
            relocs[key].append({
                "address": r_address,
                "symbolnum": r_symbolnum,
                "pcrel": r_pcrel,
                "length": r_length,
                "extern": r_extern,
                "type": r_type,
            })

    with open("obj_sections.json", "w") as f:
        json.dump(sections, f, indent=2)
    with open("obj_symtab.json", "w") as f:
        json.dump(symtab, f, indent=2)
    with open("obj_relocs.json", "w") as f:
        json.dump(relocs, f, indent=2)

    print(f"OK: {len(sections)} sections, {len(symtab)} symbols, "
          f"{len(relocs['text'])} text relocs, {len(relocs['const'])} const relocs, "
          f"{len(relocs['init'])} init relocs, {len(relocs['term'])} term relocs")

    # cross-check against otool/nm if available (macOS-only tools -- skip if absent)
    try:
        nm_out = subprocess.run(["nm", "-m", OBJ], capture_output=True, text=True, check=True).stdout
        print("--- nm -m cross-check (first 5 lines) ---")
        print("\n".join(nm_out.splitlines()[:5]))
    except Exception:
        pass


if __name__ == "__main__":
    main()
