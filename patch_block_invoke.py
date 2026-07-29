#!/usr/bin/env python3
"""Live-patch ___MTLCreateSystemDefaultDevice_block_invoke (0x1970506e4,
inside the "Metal" image of the guest's own dyld_shared_cache_arm64e) to
replace its broken "read a global collection, check count==1" device-lookup
logic with: dlopen("/b") -> dlsym(handle, "Q") -> call it -> store the
returned device pointer into the exact same struct field the original code
would have written.

Why this exists: MTLCreateSystemDefaultDevice()'s real discovery mechanism
never populates the collection this function reads (confirmed via live
testing -- see project memory), even with the real AGX IOKit personality
hijacked onto our own kext. Rather than hand-assemble the entire
IOServiceOpen/CFDictionary/NSClassFromString/-initWithAcceleratorPort:
sequence in raw machine code, that logic lives in normally-compiled ObjC
(src/userspace_test/inferno_agx_bridge.m, exported as a 1-char symbol "Q"
so dlsym's name string fits a single MOVZ immediate) deployed to /b on the
guest. This patch is just the glue: dlopen+dlsym+call+store, ~19
instructions, verified to fit the 21-instruction code cave between the
prologue and the shared epilogue (see block-by-block layout below).

dlopen/dlsym themselves aren't directly `bl`-reachable (libdyld.dylib is
~368MB away, past the +/-128MB range of a direct BL) -- this patch instead
calls Metal's own already-linked local stubs for them (found via
`ipsw dyld macho DSC Metal --stubs`), which dyld has already bound to the
real libdyld implementations and which ARE in range since they live inside
Metal's own image alongside the patch site.

IMPORTANT: like the InfernoVGPUHello kernelcache personality hijack, a
dyld_shared_cache write only takes effect after a full guest reboot -- the
cache is mapped once near boot and not re-read from disk mid-session
(confirmed reproducibly earlier this session).
"""
import socket
import struct
import sys
import time

sys.path.insert(0, "/home/makr/Documents/inferno-vgpu-kext")
from asm_helper import (
    SP, add_imm, b, bl, blr, cbz, ldr_imm, mov_reg, movz, str_imm, sub_imm,
    verify_against_known,
)

# Applied via the GUEST's own root shell (dd, seek=file_off, conv=notrunc),
# matching the already-proven -initWithAcceleratorPort: patch methodology --
# NOT a direct host-side file edit. The host-side copy at
# InfernoData/dyld_shared_cache_arm64e was confirmed byte-identical at this
# offset (used only for read-only ipsw disassembly/analysis).
DSC_GUEST_PATH = "/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64e"
HOST, PORT = "127.0.0.1", 4444

TEXT_VA_BASE = 0x180000000
TEXT_FILE_OFF = 0x0

FUNC_START = 0x1970506e4          # pacibsp -- untouched
PATCH_START = 0x1970506fc          # right after `mov x19,x0`
EPILOGUE = 0x197050750              # ldp fp,lr / ldp x20,x19 / ldp x22,x21 / retab -- untouched
PATCH_END_EXCLUSIVE = EPILOGUE       # must not touch the epilogue itself

DLOPEN_STUB = 0x1970a5cc0            # Metal's own __stubs entry -> real _dlopen
DLSYM_STUB = 0x1970a5cd0             # Metal's own __stubs entry -> real _dlsym

X0, X1, X8, X19 = 0, 1, 8, 19


def va2off(va):
    assert TEXT_VA_BASE <= va < TEXT_VA_BASE + 0x4e434000, hex(va)
    return TEXT_FILE_OFF + (va - TEXT_VA_BASE)


def build_patch():
    va = PATCH_START
    words = []

    def emit(w):
        nonlocal va
        words.append((va, w))
        va += 4

    # -- build "/b\0..." on the stack, at [sp] --
    emit(sub_imm(SP, SP, 32))             # sub sp, sp, #32
    emit(movz(X0, 0x622F))                 # movz x0, #0x622f  ("/b\0\0\0\0\0\0")
    emit(str_imm(X0, SP, 0))                # str x0, [sp]
    emit(movz(X0, 0x0051))                   # movz x0, #0x51    ("Q\0\0\0\0\0\0\0")
    emit(str_imm(X0, SP, 16))                 # str x0, [sp, #16]

    # -- dlopen("/b", RTLD_NOW) --
    emit(add_imm(X0, SP, 0))                   # mov x0, sp        (add x0,sp,#0)
    emit(movz(X1, 2))                           # mov x1, #2        (RTLD_NOW)
    bl_dlopen_va = va
    emit(bl(bl_dlopen_va, DLOPEN_STUB))           # bl dlopen_stub    -> x0=handle
    cbz1_va = va
    cbz1_slot = len(words)
    emit(0)                                        # placeholder, patched below once fail: is known

    # -- dlsym(handle, "Q") --  (x0 already = handle from dlopen's return)
    emit(add_imm(X1, SP, 16))                       # add x1, sp, #16   (&"Q")
    bl_dlsym_va = va
    emit(bl(bl_dlsym_va, DLSYM_STUB))                 # bl dlsym_stub     -> x0=fn ptr
    cbz2_va = va
    cbz2_slot = len(words)
    emit(0)                                            # placeholder

    # -- call it --
    emit(blr(X0))                                        # blr x0            -> x0=device
    cbz3_va = va
    cbz3_slot = len(words)
    emit(0)                                                # placeholder

    # -- store into [[x19+0x20]+0x8]+0x28, same field the original wrote --
    emit(ldr_imm(X8, X19, 0x20))                             # ldr x8, [x19, #0x20]
    emit(ldr_imm(X8, X8, 0x8))                                 # ldr x8, [x8, #0x8]
    emit(str_imm(X0, X8, 0x28))                                  # str x0, [x8, #0x28]

    fail_va = va
    emit(add_imm(SP, SP, 32))                                     # add sp, sp, #32
    emit(b(va, EPILOGUE))                                          # b epilogue

    # backpatch the three cbz placeholders now that fail_va is known
    words[cbz1_slot] = (cbz1_va, cbz(X0, fail_va - cbz1_va))
    words[cbz2_slot] = (cbz2_va, cbz(X0, fail_va - cbz2_va))
    words[cbz3_slot] = (cbz3_va, cbz(X0, fail_va - cbz3_va))

    end_va = va
    assert end_va <= PATCH_END_EXCLUSIVE, (
        f"patch overruns into epilogue: end={hex(end_va)} limit={hex(PATCH_END_EXCLUSIVE)}"
    )
    print(f"patch occupies {hex(PATCH_START)}..{hex(end_va)} "
          f"({end_va - PATCH_START} bytes / {len(words)} instructions), "
          f"budget was {PATCH_END_EXCLUSIVE - PATCH_START} bytes, "
          f"{PATCH_END_EXCLUSIVE - end_va} bytes slack before epilogue at {hex(EPILOGUE)}")
    return words


def guest_cmd(sock, cmd, idle=1.0, deadline_total=20.0):
    sock.sendall((cmd + "\n").encode())
    buf = b""
    deadline = time.time() + deadline_total
    idle_deadline = time.time() + idle
    while time.time() < deadline and time.time() < idle_deadline:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            idle_deadline = time.time() + idle
        except socket.timeout:
            continue
    return buf.decode(errors="replace")


def main():
    verify_against_known()
    words = build_patch()

    print("\n-- final instruction listing --")
    for addr, w in words:
        print(f"{hex(addr)}: {w:#010x}")

    sock = socket.create_connection((HOST, PORT), timeout=10)
    sock.settimeout(0.3)
    guest_cmd(sock, "echo PATCH_START_$$", idle=2.0)  # sync up / drain any stale output

    for addr, w in words:
        off = va2off(addr)
        raw = struct.pack("<I", w)
        hexescapes = "".join(f"\\x{b:02x}" for b in raw)
        cmd = (
            f"printf '{hexescapes}' | dd of={DSC_GUEST_PATH} bs=1 "
            f"seek={off} conv=notrunc 2>&1; echo RC=$?"
        )
        out = guest_cmd(sock, cmd, idle=1.5)
        ok = "RC=0" in out
        print(f"{'OK ' if ok else 'FAIL'} wrote {hex(addr)} (file off {hex(off)}) := {w:08x}  guest_out={out.strip()!r}")
        if not ok:
            print("ABORTING on first failed write -- guest cache may be partially patched, verify before retrying")
            sock.close()
            sys.exit(1)

    sock.close()
    print("\nall writes acknowledged by guest -- reboot the guest for the dyld_shared_cache change to take effect")


if __name__ == "__main__":
    main()
