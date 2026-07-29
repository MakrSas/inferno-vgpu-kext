#!/usr/bin/env python3
"""Same as patch_kernelcache.py (personality hijack via __kmod_init[1097],
BTI-safe redirect to our ctor) but ALSO patches a BRK #0 at CODE_BASE+0x250 --
the instruction right after `bl OSMetaClass::OSMetaClass(name,super,size)`
inside InfernoVGPUHello::MetaClass::MetaClass2Ev (disassembly-confirmed: the
call at CODE_BASE+0x24c is `bl #0xfffffff007f82648` ==
__ZN11OSMetaClassC2EPKcPKS_j exactly). This location is reached ONLY by our
own static-init chain (__GLOBAL__sub_I -> MetaClassC1Ev -> MetaClassC2Ev),
once, right after gMetaClass's core fields (name/superclass/classSize) are
set -- guaranteed race-free and unambiguous, no register-filtering needed.
"""
import struct

KC_SRC = "/tmp/claude-1000/-home-makr-Documents-Inferno/fe200f1f-9db9-4623-9475-b435250a31ad/scratchpad/kc.decompressed"
KC_OUT = "/home/makr/Documents/Inferno/InfernoData/kernelcache.vgpu3.brk2.patched"

INFO_SEC_OFF = 43798088
INFO_SEC_SIZE = 0x111b06

CODE_BASE = 0xfffffff009427e10
KERNEL_TEXT_BASE = 0xfffffff007004000

OLD_IOCLASS = b"<key>IOClass</key><string>AppleSynopsysMIPIDSIController</string>"
NEW_IOCLASS = b"<key>IOClass</key><string>InfernoVGPUHello</string>"

BRK2_VA = CODE_BASE + 0x250   # return address right after OSMetaClass::OSMetaClass() call
BRK_INSN = 0xD4200000          # BRK #0


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


def main():
    with open(KC_SRC, "rb") as f:
        data = bytearray(f.read())

    start = data.find(OLD_IOCLASS)
    assert start != -1, "AppleSynopsysMIPIDSIController IOClass not found"
    end = start + len(OLD_IOCLASS)
    old_len = end - start
    delta = old_len - len(NEW_IOCLASS)
    assert delta >= 0
    print(f"personality IOClass: old_len={old_len} new_len={len(NEW_IOCLASS)} pad={delta}")

    section_end = INFO_SEC_OFF + INFO_SEC_SIZE
    new_section = data[INFO_SEC_OFF:start] + NEW_IOCLASS + data[end:section_end]
    new_section += b"\x00" * delta
    assert len(new_section) == INFO_SEC_SIZE
    data[INFO_SEC_OFF:section_end] = new_section

    with open("/home/makr/Documents/inferno-vgpu-kext/resolved_blob.bin", "rb") as f:
        blob = f.read()
    code_off = va2off(data, CODE_BASE)
    assert code_off is not None
    print(f"injecting {len(blob)} bytes at file offset {hex(code_off)} (VA {hex(CODE_BASE)})")
    data[code_off:code_off + len(blob)] = blob

    KMOD_INIT_BASE = 0xfffffff0076bd2b8
    KMOD_INIT_IDX = 1097
    CTOR_VA = CODE_BASE + 0x410
    entry_va = KMOD_INIT_BASE + KMOD_INIT_IDX * 8
    entry_off = va2off(data, entry_va)
    raw_entry = struct.unpack_from("<Q", data, entry_off)[0]
    target_va = (raw_entry & 0xFFFFFFFFFFFF) | 0xFFFF000000000000
    print(f"__kmod_init[{KMOD_INIT_IDX}] left untouched, raw={hex(raw_entry)}, real target VA={hex(target_va)}")

    def b_insn(pc, target):
        delta = target - pc
        assert delta % 4 == 0
        imm26 = (delta // 4) & 0x3FFFFFF
        return 0x14000000 | imm26

    target_off = va2off(data, target_va)
    orig_second_insn = struct.unpack_from("<I", data, target_off + 4)[0]
    redirect = b_insn(target_va + 4, CTOR_VA)
    struct.pack_into("<I", data, target_off + 4, redirect)
    print(f"patched second insn at {hex(target_va+4)}: {hex(orig_second_insn)} -> B {hex(CTOR_VA)} ({hex(redirect)})")

    # ---- diagnostic BRK right after gMetaClass's OSMetaClass base ctor returns ----
    brk2_off = va2off(data, BRK2_VA)
    orig_brk2_insn = struct.unpack_from("<I", data, brk2_off)[0]
    struct.pack_into("<I", data, brk2_off, BRK_INSN)
    print(f"patched BRK2 at {hex(BRK2_VA)}: {hex(orig_brk2_insn)} -> BRK #0")

    with open(KC_OUT, "wb") as f:
        f.write(data)
    print("wrote", KC_OUT)


if __name__ == "__main__":
    main()
