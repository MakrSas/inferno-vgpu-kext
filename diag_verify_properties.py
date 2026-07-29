#!/usr/bin/env python3
"""One-off diagnostic: after patch_kernelcache.py has already produced
kernelcache.vgpu2.patched, overlay a small guest-side probe that calls
IORegistryEntry::getProperty() for our 3 published keys right after
registerService() returns, storing each result pointer into COMMON_BASE
scratch + BRK. Not part of the real driver -- verifies setProperty/
registerService actually landed real values, nothing more.
"""
import struct

KC = "/home/makr/Documents/Inferno/InfernoData/kernelcache.vgpu2.patched"
OUT = "/home/makr/Documents/Inferno/InfernoData/kernelcache.vgpu2.diagprop.patched"

CODE_BASE = 0xfffffff009427e10
COMMON_BASE = 0xfffffff1020c4000
GETPROPERTY = 0xfffffff007fffabc  # __ZNK15IORegistryEntry11getPropertyEPKc
STRING_PAGE = 0xfffffff009428000

REGISTERSERVICE_RETURN_VA = CODE_BASE + 0x4d4  # right after `bl registerService`
DIAG_VA = CODE_BASE + 0x600

KEY_OFFS = [0xd71, 0xd91, 0xdb2]  # IOMatchCategory / MetalPluginName / MetalPluginClassName
SCRATCH_OFFS = [0x100, 0x108, 0x110]


def va2off(data, va):
    ncmds = struct.unpack_from("<I", data, 16)[0]
    off = 32
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == 0x19:
            segname, vmaddr, vmsize, fileoff, filesize = struct.unpack_from("<16sQQQQ", data, off + 8)
            if vmaddr <= va < vmaddr + vmsize:
                return fileoff + (va - vmaddr)
        off += cmdsize
    return None


def adrp(rd, pc, target):
    page_delta = (target & ~0xFFF) - (pc & ~0xFFF)
    imm = (page_delta >> 12) & 0x1FFFFF
    immlo = imm & 0x3
    immhi = (imm >> 2) & 0x7FFFF
    return 0x90000000 | (immlo << 29) | (immhi << 5) | rd


def add_imm(rd, rn, imm12):
    return 0x91000000 | ((imm12 & 0xFFF) << 10) | (rn << 5) | rd


def bl(pc, target):
    delta = target - pc
    assert delta % 4 == 0
    return 0x94000000 | ((delta // 4) & 0x3FFFFFF)


def str_x(rt, rn):
    return 0xF9000000 | (rn << 5) | rt


def mov_reg(rd, rm):
    return 0xAA0003E0 | (rm << 16) | rd


def main():
    with open(KC, "rb") as f:
        data = bytearray(f.read())

    off = va2off(data, REGISTERSERVICE_RETURN_VA)
    orig = struct.unpack_from("<I", data, off)[0]
    b_delta = DIAG_VA - REGISTERSERVICE_RETURN_VA
    struct.pack_into("<I", data, off, 0x14000000 | ((b_delta // 4) & 0x3FFFFFF))
    print(f"redirected {hex(REGISTERSERVICE_RETURN_VA)} ({hex(orig)}) -> b {hex(DIAG_VA)}")

    insns = [0xD503237F, 0xF94003F3]  # pacibsp; ldr x19,[sp]
    pc = DIAG_VA + 8
    for key_off, scratch_off in zip(KEY_OFFS, SCRATCH_OFFS):
        key_va = STRING_PAGE + key_off
        block = []
        block.append(mov_reg(0, 19))
        block.append(("adrp", 1, key_va))
        block.append(add_imm(1, 1, key_va & 0xFFF))
        block.append(("bl", GETPROPERTY))
        block.append(("adrp", 9, COMMON_BASE))
        block.append(add_imm(9, 9, scratch_off))
        block.append(str_x(0, 9))
        for entry in block:
            if isinstance(entry, tuple) and entry[0] == "adrp":
                insns.append(adrp(entry[1], pc, entry[2]))
            elif isinstance(entry, tuple) and entry[0] == "bl":
                insns.append(bl(pc, entry[1]))
            else:
                insns.append(entry)
            pc += 4
    insns.append(0xD4200000)  # brk #0

    diag_off = va2off(data, DIAG_VA)
    for i, insn in enumerate(insns):
        struct.pack_into("<I", data, diag_off + i * 4, insn)
    print(f"wrote {len(insns)} diag instructions at {hex(DIAG_VA)} (file off {hex(diag_off)})")

    with open(OUT, "wb") as f:
        f.write(data)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
