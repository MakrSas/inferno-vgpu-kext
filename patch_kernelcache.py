#!/usr/bin/env python3
import json
import struct

import os
KC_SRC = os.environ.get("KC", "/home/makr/Documents/Inferno/InfernoData/kernelcache.decompressed")
KC_OUT = "/home/makr/Documents/Inferno/InfernoData/kernelcache.vgpu2.patched"

INFO_SEC_OFF = 43798088
INFO_SEC_SIZE = 0x111b06

CODE_BASE = 0xfffffff009427e10
KERNEL_TEXT_BASE = 0xfffffff007004000

# Hijack a KNOWN-WORKING, real personality instead of a synthetic DT node:
# AppleSynopsysMIPIDSIController's start() is proven to run every single boot
# (its own log line "%s panicOnError NOT Supported" appears every time). Real
# personality dict (verbatim, confirmed from __PRELINK_INFO):
#   <dict><key>IOClass</key><string>AppleSynopsysMIPIDSIController</string>
#   <key>IOProviderClass</key><string>AppleARMIODevice</string>
#   <key>IONameMatch</key><string>mipi-dsim-1,synopsys</string>
#   <key>IOPlatformPanicAction</key><integer ID="226" size="64">0x14c08</integer></dict>
# Only touch IOClass's value -- keep IOProviderClass/IONameMatch/PanicAction
# untouched so the real, already-working match keeps matching, just
# instantiating our class instead.
OLD_IOCLASS = b"<key>IOClass</key><string>AppleSynopsysMIPIDSIController</string>"
NEW_IOCLASS = b"<key>IOClass</key><string>InfernoVGPUHello</string>"

# Second hijack, added once the real device-discovery gap was found: Metal's
# own MTLCreateSystemDefaultDevice() populates its device list by watching
# for the REAL AGX personality (found in __PRELINK_INFO, AGXG12P.kext):
#   <key>AGXG12P_B0</key><dict>
#     <key>IOClass</key><string>AGXAcceleratorG12P_B0</string>
#     <key>IOMatchCategory</key><string>IOAcceleratorES</string>
#     <key>IOProviderClass</key><string>AppleARMIODevice</string>
#     <key>IONameMatch</key><array><string>gpu,t8015</string>
#       <string>gpu,t8027</string><string>gpu,t8030</string></array>
#     <key>MetalPluginName</key><string>AGXMetalA13</string> ... </dict>
# -- our MIPI-DSI hijack above never satisfies this (Metal doesn't discover
# by MetalPluginClassName property, it watches for *this* real personality
# specifically). t8030.c already creates the exact DT node this matches
# against (arm-io/sgx, compatible "gpu,t8030" -- see t8030_create_agx()) for
# its own apple_agx_from_node() device; hijacking IOClass here lets
# InfernoVGPUHello attach there too. Safe to add alongside the MIPI-DSI hook:
# InfernoVGPUHello::start() maps its MMIO by a hardcoded physical address
# (IOMemoryDescriptor::withPhysicalAddress(INFERNO_VGPU_PHYS_BASE, ...)), not
# anything derived from `provider`, so which personality triggers it doesn't
# matter -- both instantiations behave identically.
OLD_IOCLASS_AGX = b"<key>IOClass</key><string>AGXAcceleratorG12P_B0</string>"
NEW_IOCLASS_AGX = b"<key>IOClass</key><string>InfernoVGPUHello</string>"

# ---------------------------------------------------------------------------
# Security-gate byte patches (gates #1-#4 baked in permanently in the
# 2026-07-30 session; gate #6 added 2026-07-31 -- see its own entry below for
# why the numbering skips #5, which remains a documented-but-unresolved
# candidate, see PROJECT_STATUS.md's iokit-open/entitlement investigation).
#
# Gates #1-#4: four independent kernel security gates were found
# live-patching every freshly-transferred unsigned MAIN EXECUTABLE into an
# instant `Killed: 9` at exec time (see PROJECT_STATUS.md's "CRITICAL: the
# SIGKILL mystery" section for the full derivation of each one -- breakpoint
# traces, disassembly, live verification). All four were previously only
# ever applied in-memory via GDB against the running QEMU process, and were
# lost on every restart. This table makes them permanent, file-level
# patches, applied the same way as everything else in this script (straight
# file-offset byte replacement via va2off). Gate #6 (added below) is a
# structurally different check (file-write-data, not exec-time) found in a
# later session -- kept in the same table/mechanism since the patch
# machinery is identical.
#
# Each entry is (VA, orig_bytes, new_bytes, description). Before writing,
# the bytes actually present at that file offset are asserted to exactly
# match orig_bytes -- if a mismatch is found, it means either the offset
# math is wrong or this isn't the same kernelcache build these patches were
# derived against, and patching blindly would be dangerous.
SIGKILL_GATE_PATCHES = [
    (
        0xfffffff007eef788,
        bytes.fromhex("a0000034"),
        bytes.fromhex("05000014"),
        "gate #1: load_machfile cbz->b, ignore parse_machfile failure",
    ),
    (
        0xfffffff0082f2b18,
        bytes.fromhex("e800c837"),
        bytes.fromhex("07000014"),
        "gate #2a: AMFI cred_label_update_execve tbnz->b, ignore csflags bit25 (dyld sig check)",
    ),
    (
        0xfffffff0082f2b8c,
        bytes.fromhex("800b0036"),
        bytes.fromhex("1f2003d5"),
        "gate #2b: AMFI cred_label_update_execve NOP tbz, ignore loadEntitlementsFromVnode failure",
    ),
    (
        0xfffffff0092b0f00,
        bytes.fromhex("e0060034"),
        bytes.fromhex("1f2003d5"),
        "gate #3: Sandbox hook_cred_label_update_execve NOP cbz, ignore \"only launchd...\" check",
    ),
    (
        0xfffffff0092b126c,
        bytes.fromhex("08060034"),
        bytes.fromhex("1f2003d5"),
        "gate #4: Sandbox hook_cred_label_update_execve NOP cbz, ignore \"outside of container...\" check",
    ),
    (
        0xfffffff0092a25b4,
        bytes.fromhex("f50300aa"),
        bytes.fromhex("15008052"),
        "gate #6: Sandbox hook_vnode_check_open op 0x1f (file-write-data), "
        "force-allow capture register -- found 2026-07-31 live-patching the "
        "app-container write needed to swap StocksWidget's binary in place; "
        "see PROJECT_STATUS.md's 'A 6th, previously-undiscovered security "
        "gate' section. NOT an exec-time kill gate like #1-#4 above (this "
        "one guards file-write-data via a MACF vnode-check hook, not "
        "process exec) -- kept in this same table/file per that section's "
        "own recommended next step, since the mechanics (disassemble-first, "
        "minimal single-instruction patch, verify-before-write) and the "
        "permanence-baking machinery below are identical either way.",
    ),
]


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

    # Edit only *within* the __info section's own byte range: apply both
    # personality-IOClass replacements (each shorter than the original, so
    # the section only ever shrinks), then NUL-pad the tail to restore the
    # section's exact original size. This keeps the section's start/size
    # (and every offset in the rest of the file) completely unchanged.
    section_end = INFO_SEC_OFF + INFO_SEC_SIZE
    section = bytes(data[INFO_SEC_OFF:section_end])
    total_delta = 0
    for old, new in ((OLD_IOCLASS, NEW_IOCLASS), (OLD_IOCLASS_AGX, NEW_IOCLASS_AGX)):
        start = section.find(old)
        assert start != -1, f"{old!r} not found"
        end = start + len(old)
        delta = len(old) - len(new)
        assert delta >= 0, f"new IOClass string is LONGER than old ({len(new)} > {len(old)}) -- would overflow the section"
        print(f"personality IOClass: old_len={len(old)} new_len={len(new)} pad={delta}")
        section = section[:start] + new + section[end:]
        total_delta += delta
    section += b"\x00" * total_delta
    assert len(section) == INFO_SEC_SIZE
    data[INFO_SEC_OFF:section_end] = section

    # ---- apply the 5 SIGKILL-gate byte patches (see table above) ----
    for va, orig_bytes, new_bytes, desc in SIGKILL_GATE_PATCHES:
        off = va2off(data, va)
        assert off is not None, f"va2off failed for VA {hex(va)} ({desc})"
        actual = bytes(data[off:off + len(orig_bytes)])
        assert actual == orig_bytes, (
            f"SIGKILL gate patch mismatch at VA {hex(va)} (file offset {hex(off)}): "
            f"expected {orig_bytes.hex()}, found {actual.hex()} -- {desc}"
        )
        data[off:off + len(new_bytes)] = new_bytes
        print(f"SIGKILL gate patch applied at VA {hex(va)} (file offset {hex(off)}): "
              f"{orig_bytes.hex()} -> {new_bytes.hex()} -- {desc}")

    # ---- inject resolved IOKit class blob ----
    with open("/home/makr/Documents/inferno-vgpu-kext/resolved_blob.bin", "rb") as f:
        blob = f.read()
    code_off = va2off(data, CODE_BASE)
    assert code_off is not None
    print(f"injecting {len(blob)} bytes at file offset {hex(code_off)} (VA {hex(CODE_BASE)})")
    data[code_off:code_off + len(blob)] = blob

    # (start()'s old leftover diagnostic marker-write, which needed a NOP
    # patch here, is gone -- start() now does real IOKit work, compiled in
    # directly. See resolve.py's history for that fix if ever needed again.)

    # ---- redirect via an existing, ALREADY-VALIDLY-SIGNED __kmod_init entry ----
    # Directly repointing the array entry to our plain ctor address doesn't work:
    # OSRuntimeCallStructorsInSection calls it via `blraa x8, x22` (a real
    # authenticated call, confirmed by disassembly -- modifier x22 is a fixed
    # constant 0x4a27) and PAC is genuinely active (confirmed: SCTLR_EL1 has
    # EnIA/EnIB/EnDA/EnDB all set at call time). Worse, the PAC key
    # (APIAKeyLo/Hi_EL1) is generated fresh every boot (confirmed empirically:
    # two separate QEMU launches of the identical kernelcache gave different
    # key values), so there is no way to pre-compute a correctly-signed pointer
    # and bake it into a static file ahead of time.
    #
    # Fix: leave the array entry COMPLETELY UNTOUCHED (it's already correctly
    # signed by Apple's real build -- authentication will succeed). Instead,
    # patch the FIRST INSTRUCTION of the function that entry already points to
    # (BCMWLANCore's own, presumably-dead-code original ctor) with a plain
    # direct B to our own ctor. Direct branches are PC-relative immediates
    # baked into the instruction itself -- no signing, no authentication,
    # nothing for PAC to reject. blraa authenticates the untouched pointer,
    # branches to the original (real, validly-signed) target address, executes
    # our injected "B our_ctor" there, and we're in.
    # AppleSynopsysMIPIDSI's real __kmod_init entries -- confirmed via a live
    # boot BRK test that OSKext::start() -> OSRuntimeInitializeCPP(this) ->
    # OSRuntimeCallStructorsInSection really fires for this kext, with
    # kmod_info->address landing at 0xfffffff008d358e8 (textStart) and
    # textEnd at 0xfffffff008d4b678 -- confirmed by finding real __kmod_init
    # entries (indices 1095-1105) whose decoded values fall in that exact
    # range. Note: kmod_info->address is at struct offset 0x9c in the
    # real in-kernel layout, NOT 0xa0 as the public kmod_info_t definition
    # in mach/kmod.h would suggest -- discovered by disassembling the real
    # per-kext code path in OSRuntimeInitializeCPP.
    # NOTE: this address range turned out to actually belong to the SCSI
    # family kext (IOSCSI*.cpp static initializers), not AppleSynopsysMIPIDSI
    # as originally assumed -- the 2MB generous range guess happened to
    # contain both. Confirmed harmless/general: the technique works via ANY
    # real, validly-signed __kmod_init entry, regardless of which kext it
    # belongs to. Index 1100 (IOSCSIPrimaryCommandsDevice.cpp) caused a real
    # panic (OSMetaClass::allocClassWithName failure) -- likely because SCSI
    # command-layer registration is load-bearing for the NVMe root disk in
    # this VM. Switched to 1097 (IOSCSIMultipathedLogicalUnit.cpp) -- SCSI
    # multipath is an enterprise feature almost certainly unused by this
    # simple single-path NVMe setup, much safer to sacrifice.
    KMOD_INIT_BASE = 0xfffffff0076bd2b8
    KMOD_INIT_IDX = 1097  # IOSCSIMultipathedLogicalUnit.cpp -- safer than 1100
    # __GLOBAL__sub_I_InfernoVGPUHello.cpp's offset within __text shifts every
    # time the object's compiled code size changes (bug caught live: this was
    # hardcoded as CODE_BASE+0x410 and silently went stale -- and wrong -- the
    # moment start() grew past a diagnostic marker write into real IOKit work,
    # sending the redirect into the middle of an unrelated function and
    # hanging the whole boot in a tight loop). Always derive it fresh from the
    # object's own symbol table instead of hardcoding.
    _symtab = json.load(open("/home/makr/Documents/inferno-vgpu-kext/obj_symtab.json"))
    _ctor_sym = next(s for s in _symtab if s["name"] == "__GLOBAL__sub_I_InfernoVGPUHello.cpp")
    CTOR_VA = CODE_BASE + _ctor_sym["value"]
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

    # Patch the SECOND instruction, not the first: the first instruction is
    # `pacibsp`, a valid BTI landing pad for an indirect (blraa) branch target.
    # Overwriting it with a plain B would leave a non-BTI-compatible landing
    # pad, which can itself fault (Branch Target Exception) if BTI enforcement
    # is active -- independent of PAC authentication succeeding or not.
    target_off = va2off(data, target_va)
    orig_first_insn = struct.unpack_from("<I", data, target_off)[0]
    orig_second_insn = struct.unpack_from("<I", data, target_off + 4)[0]
    redirect = b_insn(target_va + 4, CTOR_VA)
    struct.pack_into("<I", data, target_off + 4, redirect)
    print(f"kept first insn at {hex(target_va)} ({hex(orig_first_insn)}, pacibsp/BTI landing pad)")
    print(f"patched second insn at {hex(target_va+4)}: {hex(orig_second_insn)} -> B {hex(CTOR_VA)} ({hex(redirect)})")

    with open(KC_OUT, "wb") as f:
        f.write(data)
    print("wrote", KC_OUT)


if __name__ == "__main__":
    main()
