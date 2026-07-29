#!/usr/bin/env python3
"""Mini-linker for InfernoVGPUHello.o: resolves all 347 relocations against
the real kernelcache and produces final flat bytes ready to inject into the
BCMWLANCore code slot. See project_inferno_gpu.md memory for the full story
behind each of the formulas used here.
"""
import json, struct, subprocess, sys, os

KC = os.environ.get("KC", "/tmp/claude-1000/-home-makr-Documents-Inferno/fe200f1f-9db9-4623-9475-b435250a31ad/scratchpad/kc.decompressed")
OBJ = "obj/InfernoVGPUHello.o"

KERNEL_TEXT_BASE = 0xfffffff007004000   # this kernelcache's __TEXT vmaddr

# Final placement chosen for our injected blob (BCMWLANCore slot base + 0x20000,
# clear of the earlier kmod_hello test bytes at +0x8000/+0x10000).
BCMWLANCORE_SLOT_BASE = 0xfffffff009407e10
CODE_BASE = BCMWLANCORE_SLOT_BASE + 0x20000
# __common (gMetaClass, 40 bytes writable) -- dedicated 4KB carve, added
# specifically for this (t8030_memory_setup's "vgpu scratch region"), separate
# from the GFX firmware region (which is live RTKit mailbox traffic end-to-end,
# not spare space -- confirmed by reading it back mid-boot). Re-verify per
# rebuild via the "vgpu scratch region: base=0x..." qemu_log line.
COMMON_BASE = 0xfffffff1020c4000

BASE_CLASS_VTABLES = {
    "OSMetaClass": 0xfffffff0076fd360,
    "OSMetaClassBase": 0xfffffff0076fd258,
    "OSObject": 0xfffffff0076fd3f8,
    "IOService": 0xfffffff0077001e0,
    "IORegistryEntry": 0xfffffff0076ffc38,
    "IOUserClient": 0xfffffff00770d018,
}


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


def decode_chained_qword(raw):
    """Return the resolved plain VA for a DYLD_CHAINED_PTR_ARM64E-style qword
    found in this kernelcache's __DATA_CONST (auth-rebase or plain-rebase)."""
    auth = (raw >> 63) & 1
    bind = (raw >> 62) & 1
    if auth == 1 and bind == 0:
        target = raw & 0xFFFFFFFF
        return KERNEL_TEXT_BASE + target
    if auth == 0 and bind == 0:
        target = raw & ((1 << 43) - 1)
        high8 = (raw >> 43) & 0xFF
        return target | (high8 << 56)
    raise ValueError(f"unexpected bind pointer in vtable: {raw:#x}")


def read_kc_qword(kc_data, va):
    off = va2off(kc_data, va)
    return struct.unpack_from("<Q", kc_data, off)[0]


def load_kernel_symbols(path="kernel-symbols.txt"):
    syms = {}
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            addr_s, name = parts[0], parts[-1]
            try:
                syms[name] = int(addr_s, 16)
            except ValueError:
                continue
    return syms


def resolve_reserved_slot(name, kc_data):
    # name like __ZN9IOService20_RESERVEDIOService10Ev -- pull class + index
    import re
    m = re.match(r"__ZN(\d+)(\w+?)\d*_RESERVED\2(\d+)Ev$", name)
    if not m:
        # fallback: brute-force match class name embedded in the mangled name
        for cls, vtable_va in BASE_CLASS_VTABLES.items():
            if f"_RESERVED{cls}" in name:
                idx_m = re.search(r"_RESERVED" + cls + r"(\d+)Ev$", name)
                if idx_m:
                    return cls, int(idx_m.group(1))
        raise ValueError(f"cannot parse reserved-slot symbol {name}")
    cls, idx = m.group(2), int(m.group(3))
    return cls, idx


def get_vtable_slot_va(cls, slot_index, kc_data):
    """slot_index counts *real* vfunc slots starting at 0 (i.e. skip the
    2 leading words: offset-to-top + RTTI pointer)."""
    base = BASE_CLASS_VTABLES[cls]
    entry_va = base + 16 + slot_index * 8
    raw = read_kc_qword(kc_data, entry_va)
    return decode_chained_qword(raw)


def main():
    with open(OBJ, "rb") as f:
        obj = bytearray(f.read())
    with open(KC, "rb") as f:
        kc_data = f.read()

    symtab = {e["idx"]: e for e in json.load(open("obj_symtab.json"))}
    symtab_by_name = {e["name"]: e for e in symtab.values()}
    relocs = json.load(open("obj_relocs.json"))
    kernel_syms = load_kernel_symbols()

    sections = {s["sectname"]: s for s in json.load(open("obj_sections.json"))}
    text = sections["__text"]
    const = sections["__const"]

    # local-symbol final VA (within our own injected blob)
    def local_va(sym):
        if sym["sect"] == 2:  # __common, bss, separately placed
            return COMMON_BASE + sym["value"]
        return CODE_BASE + sym["value"]

    reserved_cache = {}

    def resolve_symbolnum(symbolnum):
        sym = symtab[symbolnum]
        name = sym["name"]
        if sym["sect"] != 0:
            return local_va(sym), name
        # external/undefined -- real kernel export?
        if name in kernel_syms:
            return kernel_syms[name], name
        # reserved vtable-padding slot
        if name in reserved_cache:
            return reserved_cache[name], name
        cls, idx = resolve_reserved_slot(name, kc_data)
        va = get_vtable_slot_va(cls, idx, kc_data)
        reserved_cache[name] = va
        return va, name

    unresolved = []

    # ---- __const: UNSIGNED (metaClass/superClass self-pointers) + AUTHENTICATED_POINTER (vtables) ----
    const_off = const["offset"]
    for r in relocs["const"]:
        addr = r["address"]  # section-relative already
        file_pos = const_off + addr
        try:
            target, name = resolve_symbolnum(r["symbolnum"])
        except Exception as e:
            unresolved.append((r, str(e)))
            continue
        struct.pack_into("<Q", obj, file_pos, target & 0xFFFFFFFFFFFFFFFF)

    # ---- __text: BRANCH26 / PAGE21 / PAGEOFF12 / GOT_LOAD pairs ----
    text_off = text["offset"]
    got_slots = {}  # symbolnum -> GOT slot VA (we'll place a small GOT right after __common)
    next_got_va = COMMON_BASE + 64  # 40 bytes gMetaClass + padding, then GOT

    for r in relocs["text"]:
        addr = r["address"]
        file_pos = text_off + addr
        try:
            target, name = resolve_symbolnum(r["symbolnum"])
        except Exception as e:
            unresolved.append((r, str(e)))
            continue
        insn = struct.unpack_from("<I", obj, file_pos)[0]
        pc = CODE_BASE + addr

        if r["type"] == 2:  # BRANCH26
            delta = target - pc
            assert delta % 4 == 0, f"unaligned branch target {name}"
            imm26 = (delta // 4) & 0x3FFFFFF
            insn = (insn & 0xFC000000) | imm26
        elif r["type"] == 3:  # PAGE21 (ADRP)
            page_delta = (target & ~0xFFF) - (pc & ~0xFFF)
            imm = (page_delta >> 12) & 0x1FFFFF
            immlo = imm & 0x3
            immhi = (imm >> 2) & 0x7FFFF
            insn = (insn & 0x9F00001F) | (immlo << 29) | (immhi << 5)
        elif r["type"] == 4:  # PAGEOFF12 (ADD/LDR imm)
            page_off = target & 0xFFF
            # ADD (imm12, unshifted) vs LDR/STR (imm12, scaled by access size) --
            # detect by opcode class: LDR/STR (bits 31:22 pattern) use scaled imm.
            if (insn >> 24) & 0x3F in (0x39, 0x3D):  # LDR/STR (unsigned imm class)
                size = (insn >> 30) & 0x3
                if (insn >> 26) & 1:  # SIMD&FP variant, size in 30:31 + opc bit
                    scale = size if size else 4
                else:
                    scale = size
                shift = scale
                imm12 = (page_off >> shift) & 0xFFF
            else:  # ADD (immediate)
                imm12 = page_off & 0xFFF
            insn = (insn & 0xFFC003FF) | (imm12 << 10)
        elif r["type"] in (5, 6):  # GOT_LOAD_PAGE21 / GOT_LOAD_PAGEOFF12
            # Relax GOT-indirect (adrp+ldr of a GOT slot) into a direct
            # adrp+add of the real target address. A real GOT slot would
            # need something to WRITE the resolved pointer into RAM at
            # runtime before our ctor reads it -- nothing does that (our
            # injected blob only covers __text/__const/__cstring/mod_init/
            # mod_term, never arbitrary scratch RAM), so the GOT slot
            # silently stayed all-zero and `superClass` was constructed as
            # NULL. Since the target (a real kernel export, fixed VA under
            # kaslr-off=true) is always in ADRP's +-4GB reach from our own
            # code, direct adrp+add is strictly simpler and needs no
            # runtime-writable memory at all.
            if r["type"] == 5:  # was ADRP -- repoint at the real target's page
                page_delta = (target & ~0xFFF) - (pc & ~0xFFF)
                imm = (page_delta >> 12) & 0x1FFFFF
                immlo = imm & 0x3
                immhi = (imm >> 2) & 0x7FFFF
                insn = (insn & 0x9F00001F) | (immlo << 29) | (immhi << 5)
            else:  # was `ldr xN, [xN, #off]` -- turn into `add xN, xN, #off`
                imm12 = target & 0xFFF
                insn = 0x91000000 | (imm12 << 10) | (insn & 0x3FF)
        else:
            unresolved.append((r, f"unhandled type {r['type']}"))
            continue
        struct.pack_into("<I", obj, file_pos, insn)

    # ---- Fix up explicit super-calls to a base class method ----
    # `-fapple-kext` (required for kext codegen) makes even an EXPLICITLY
    # qualified virtual call like `IOService::start(provider)` compile as an
    # indirect, PAC-authenticated dispatch through a GOT-loaded reference to
    # the base class's OWN vtable (`__ZTV9IOService` here), not a direct call
    # -- confirmed by finding a `GOT_LOAD` relocation for `__ZTV<Base>` right
    # where `start()`'s super-call should be, followed by a further `ldr
    # x8,[x8,#imm]` indexing into it. That immediate byte offset does NOT
    # correspond to the base class's real vtable slot for the method under
    # either header-inclusive or header-skipped indexing (checked against
    # `start()`'s independently-confirmed real slot, 86) -- confirmed live,
    # by boot: it authenticated fine (no `brk 0xc472`) but branched to a
    # genuinely invalid, unmapped kernel address (instruction fetch abort).
    # Whatever addressing convention `-fapple-kext` actually uses here isn't
    # understood yet, and doesn't need to be: a super-call always means "call
    # exactly this one, fixed, real implementation" regardless of runtime
    # type, so replacing the whole indirect-dispatch sequence with a plain,
    # direct `bl` to the base method's real exported address is strictly
    # simpler and exactly, unambiguously correct -- no vtable/PAC involved at
    # all, same pattern already proven reliable throughout this whole
    # project for every other direct kernel-symbol call.
    # Two live-boot bugs already caught and abandoned in earlier versions of
    # this fixup: (1) blanket-NOPing a hardcoded-length block after the ADRP
    # also discarded genuine side-effecting instructions (this/provider
    # stack-saves) interleaved in there by the compiler, corrupting every
    # later call in the function; (2) the block's total length isn't even
    # stable across recompiles -- a later build interleaved unrelated,
    # independent scratch-address setup code into the middle of what was
    # assumed to be one contiguous dispatch sequence, silently invalidating
    # every hardcoded relative offset. **Robust fix: touch nothing but the
    # `blraa` instruction itself.** Every instruction before it only computes
    # values into x8/x9/x16/x17, which the `blraa` alone consumes -- replace
    # just that one instruction with a direct `bl` and the preceding
    # computation becomes harmless dead work, correct regardless of what
    # the compiler interleaves around it or how long the sequence is.
    BLRAA_X8_X17 = 0xd73f0911
    # A base-class vtable symbol can appear more than once (one GOT_LOAD per
    # super-call site) mapping to DIFFERENT real methods -- e.g.
    # `InfernoVGPUUserClient` makes three separate `IOUserClient::xxx(...)`
    # super-calls, all referencing `__ZTV12IOUserClient`. Disambiguate by
    # which of OUR OWN function bodies (symtab range) the call address falls
    # inside, keyed on that enclosing function's own name.
    text_syms_sorted = sorted(
        [s for s in symtab.values() if s.get("sect") == 1], key=lambda s: s["value"])

    def enclosing_func(addr):
        best = None
        for s in text_syms_sorted:
            if s["value"] <= addr:
                best = s
            else:
                break
        return best["name"] if best else None

    SUPERCALL_FIXUPS = {
        # (name of OUR function containing the super-call) -> real exported
        # symbol of the specific base method actually being called.
        "__ZN16InfernoVGPUHello5startEP9IOService": "__ZN9IOService5startEPS_",
        "__ZN21InfernoVGPUUserClient12initWithTaskEP4taskPvjP12OSDictionary":
            "__ZN12IOUserClient12initWithTaskEP4taskPvjP12OSDictionary",
        "__ZN21InfernoVGPUUserClient5startEP9IOService": "__ZN9IOService5startEPS_",
        "__ZN21InfernoVGPUUserClient14externalMethodEjP25IOExternalMethodArgumentsP24IOExternalMethodDispatchP8OSObjectPv":
            "__ZN12IOUserClient14externalMethodEjP25IOExternalMethodArgumentsP24IOExternalMethodDispatchP8OSObjectPv",
    }
    for r in relocs["text"]:
        if r["type"] != 5 or not r["extern"]:
            continue
        sym = symtab[r["symbolnum"]]
        if not sym["name"].startswith("__ZTV"):
            continue  # only base-class vtable references are super-calls
        adrp_addr = r["address"]
        func_name = enclosing_func(adrp_addr)
        if func_name not in SUPERCALL_FIXUPS:
            print(f"Super-call fixup: SKIPPING unrecognized super-call in "
                  f"{func_name} (vtable ref {sym['name']}) at "
                  f"{hex(CODE_BASE+adrp_addr)} -- will crash if reached")
            continue
        real_target = kernel_syms[SUPERCALL_FIXUPS[func_name]]
        blraa_off = None
        for cand in range(adrp_addr, min(adrp_addr + 200, text["size"] - 4), 4):
            if struct.unpack_from("<I", obj, text_off + cand)[0] == BLRAA_X8_X17:
                blraa_off = cand
                break
        assert blraa_off is not None, f"no blraa found after super-call GOT_LOAD for {sym['name']}"
        pc = CODE_BASE + blraa_off
        bl_delta = real_target - pc
        assert bl_delta % 4 == 0
        bl_insn = 0x94000000 | ((bl_delta // 4) & 0x3FFFFFF)
        struct.pack_into("<I", obj, text_off + blraa_off, bl_insn)
        print(f"Super-call fixup: {func_name} ({sym['name']}) indirect dispatch at {hex(pc)} "
              f"-> direct bl {SUPERCALL_FIXUPS[func_name]} ({hex(real_target)})")

    # ---- Fix up plain (non-super) calls to inherited, unoverridden virtuals ----
    # Same root cause as the super-call fixup above, but for ordinary
    # `this->inheritedMethod(...)` calls our own code makes (setProperty,
    # registerService): the offset our compiler bakes in (from the mismatched
    # SDK header) doesn't match the real vtable slot, confirmed live by a
    # second boot crash (instruction fetch/data abort reading a bogus slot
    # computed from a wrong immediate). Since we don't override any of these
    # either, a direct call is exactly correct, same reasoning as above.
    # Detected generically by scanning for the fixed-encoding `autda x16,x17`
    # / `blraa x8,x17` instruction pair this exact clang/-fapple-kext codegen
    # idiom always uses (registers are always the same; only the constants
    # loaded into them vary), rather than hardcoding byte offsets that would
    # go stale on the next recompile exactly like CTOR_VA already did once.
    # Same "only touch the blraa itself" robustness lesson as the super-call
    # fixup above applies here too -- the earlier NOP-based version of this
    # fixup happened to work on the compiles tested so far, but relied on the
    # same unstable assumption (fixed instruction count between `autda` and
    # `blraa`) that just broke the super-call fixup on a routine recompile.
    AUTDA_X16_X17 = 0xdac11a30
    BLRAA_X8_X17 = 0xd73f0911
    # Values are either a plain kernel-export symbol name (resolved via
    # kernel_syms -- the call goes to a REAL base-class implementation we
    # don't override), or ("local", name) for a symbol defined in our own
    # object (the call is on an object of exactly our own concrete type --
    # e.g. `client->start(this)` on a fresh InfernoVGPUUserClient* -- so
    # calling our own override directly is exactly equivalent to a correct
    # virtual dispatch, no possibility of a further-derived override existing).
    PLAIN_CALL_FIXUPS = {
        0x1814: "__ZN15IORegistryEntry11setPropertyEPKcS1_",  # setProperty(const char*, const char*)
        0x7d59: "__ZN9IOService15registerServiceEj",           # registerService(IOOptionBits)
        0x3ed6: "__ZN18IOMemoryDescriptor3mapEj",                # IOMemoryDescriptor::map(IOOptionBits)
        0x34f6: "__ZN11IOMemoryMap17getVirtualAddressEv",         # IOMemoryMap::getVirtualAddress()
        0x5ec5: "__ZN9IOService9terminateEj",                      # terminate(IOOptionBits) (in clientClose)
        0x1601: ("local", "__ZNK21InfernoVGPUUserClient9MetaClass5allocEv"),  # OSTypeAlloc -> metaClass->alloc()
        0xbde5: ("local", "__ZN21InfernoVGPUUserClient12initWithTaskEP4taskPvjP12OSDictionary"),  # client->initWithTask(...)
        0x3a87: "__ZNK8OSObject7releaseEv",                          # client->release() (3 call sites, same target)
        0x9be9: "__ZN9IOService6attachEPS_",                          # client->attach(this)
        0x3c68: ("local", "__ZN21InfernoVGPUUserClient5startEP9IOService"),  # client->start(this)
        0x7318: "__ZN9IOService6detachEPS_",                          # client->detach(this)
    }

    def resolve_fixup_target(entry):
        if isinstance(entry, tuple) and entry[0] == "local":
            return local_va(symtab_by_name[entry[1]])
        return kernel_syms[entry]

    n_fixed = 0
    n_skipped = 0
    off = 0
    while off < text["size"] - 4:
        insn = struct.unpack_from("<I", obj, text_off + off)[0]
        if insn == AUTDA_X16_X17:
            autda_off = off
            blraa_off = None
            for cand in range(autda_off + 4, min(autda_off + 200, text["size"] - 4), 4):
                if struct.unpack_from("<I", obj, text_off + cand)[0] == BLRAA_X8_X17:
                    blraa_off = cand
                    break
            if blraa_off is not None:
                movk = struct.unpack_from("<I", obj, text_off + blraa_off - 4)[0]
                disc = (movk >> 5) & 0xFFFF
                if disc in PLAIN_CALL_FIXUPS:
                    real_target = resolve_fixup_target(PLAIN_CALL_FIXUPS[disc])
                    pc = CODE_BASE + blraa_off
                    bl_delta = real_target - pc
                    assert bl_delta % 4 == 0
                    bl_insn = 0x94000000 | ((bl_delta // 4) & 0x3FFFFFF)
                    struct.pack_into("<I", obj, text_off + blraa_off, bl_insn)
                    if disc == 0x1601:
                        # OSTypeAlloc(T) expands to `T::metaClass->alloc()` --
                        # unlike every other fixed-up call, x0 ("this") is
                        # NOT already correctly sitting in a register here:
                        # the object address only exists transiently in x16
                        # right after `autda`, then the very next instruction
                        # (`ldr x8,[x16,#off]!`, pre-indexed) overwrites x16
                        # with x16+off before anything captures it. Caught by
                        # careful disassembly review, not a live crash this
                        # time. Since the target class's gMetaClass is a
                        # fixed, known address (not something that needs
                        # runtime lookup), load it directly: repurpose the
                        # two dead instruction slots right after `autda` (the
                        # pre-indexed ldr and the following `mov x9,x16` --
                        # both irrelevant now that the call is direct) to
                        # compute x0 explicitly instead. Only one OSTypeAlloc
                        # call site exists in this object today (allocating
                        # InfernoVGPUUserClient); revisit if more are added.
                        gmc_va = local_va(symtab_by_name["__ZN21InfernoVGPUUserClient10gMetaClassE"])
                        ldr_pos = text_off + blraa_off - 0x10
                        mov9_pos = text_off + blraa_off - 0xc
                        page_delta = (gmc_va & ~0xFFF) - ((CODE_BASE + blraa_off - 0x10) & ~0xFFF)
                        imm = (page_delta >> 12) & 0x1FFFFF
                        immlo = imm & 0x3
                        immhi = (imm >> 2) & 0x7FFFF
                        adrp_x0 = 0x90000000 | (immlo << 29) | (immhi << 5) | 0  # Rd=0
                        add_x0 = 0x91000000 | ((gmc_va & 0xFFF) << 10) | (0 << 5) | 0  # add x0,x0,#imm
                        struct.pack_into("<I", obj, ldr_pos, adrp_x0)
                        struct.pack_into("<I", obj, mov9_pos, add_x0)
                        print(f"  (OSTypeAlloc x0 fixup: x0 = &InfernoVGPUUserClient::gMetaClass = {hex(gmc_va)})")
                    n_fixed += 1
                else:
                    print(f"Plain-call fixup: UNRECOGNIZED disc={hex(disc)} at "
                          f"{hex(CODE_BASE+autda_off)} -- left as-is, will crash if reached")
                    n_skipped += 1
            off = blraa_off + 4 if blraa_off is not None else off + 4
        else:
            off += 4
    print(f"Plain-call fixup pass: {n_fixed} call site(s) fixed, {n_skipped} unrecognized")

    # ---- mod_init/mod_term: UNSIGNED pointers to our static ctor/dtor ----
    for sec_name in ("__mod_init_func", "__mod_term_func"):
        sec = sections[sec_name]
        # obj_relocs.json only captured mod_init ('init' key); mod_term has 1
        # reloc too but wasn't dumped earlier -- both are simple UNSIGNED
        # pointers to a local __text symbol, resolve directly from raw bytes.
        cur = struct.unpack_from("<Q", obj, sec["offset"])[0]
        # cur is 0 in the object (relocation not yet applied); use symtab directly
    for r in relocs.get("init", []):
        sec = sections["__mod_init_func"]
        target, name = resolve_symbolnum(r["symbolnum"])
        struct.pack_into("<Q", obj, sec["offset"], target)

    # mod_term: locate its symbolnum by inspecting GLOBAL__D_a (idx 7) directly
    term_sym = None
    for idx, e in symtab.items():
        if e["name"] == "__GLOBAL__D_a":
            term_sym = idx
            break
    if term_sym is not None:
        sec = sections["__mod_term_func"]
        target, name = resolve_symbolnum(term_sym)
        struct.pack_into("<Q", obj, sec["offset"], target)

    if unresolved:
        print(f"{len(unresolved)} UNRESOLVED relocations:")
        for r, err in unresolved[:20]:
            print(" ", r, err)
        sys.exit(1)

    # ---- Fix up MetaClass vtable layout mismatch ----
    # Our object was compiled against the public macOS SDK's Kernel.framework
    # headers (the only ones available -- Apple never shipped an iOS-kext KDK),
    # whose OSMetaClassBase/OSMetaClass declaration order differs from whatever
    # real internal header built this specific iOS 14/T8030 kernelcache. Decoded
    # directly from a real, live vtable (__ZTVN9IOService9MetaClassE), slots 0-11
    # (destructor pair + OSMetaClassBase's 10 own pure virtuals) match byte-for-
    # byte between our compiled layout and the real one, but the REAL ABI places
    # `alloc()` at slot 13 (only one slot of padding after the shared prefix),
    # while our compiled vtable -- reflecting a header with `Dispatch(IORPC)`
    # plus 8 full reserved slots before `alloc()` -- puts it at slot 21 instead.
    # OSMetaClass::allocClassWithName (real kernel code) authenticates and calls
    # through slot 13 unconditionally, so our vtable must present `alloc()`
    # there. Swap (not overwrite) so nothing is silently discarded.
    # Applies identically to any class's MetaClass helper: alloc()'s real
    # slot (13) is a property of the shared OSMetaClass ABI, not of the
    # specific outer class, since MetaClass always directly inherits
    # OSMetaClass regardless of what the outer class itself inherits.
    METACLASS_VT_SYMS = [
        "__ZTVN16InfernoVGPUHello9MetaClassE",
        "__ZTVN21InfernoVGPUUserClient9MetaClassE",
    ]
    for vt_sym_name in METACLASS_VT_SYMS:
        metaclass_vt_sym = symtab_by_name.get(vt_sym_name)
        if not metaclass_vt_sym:
            print(f"WARNING: {vt_sym_name} not found, skipping vtable layout fixup")
            continue
        vt_body_off = const_off + (metaclass_vt_sym["value"] - const["addr"]) + 0x10
        REAL_ALLOC_SLOT, OUR_ALLOC_SLOT = 13, 21
        off_a = vt_body_off + REAL_ALLOC_SLOT * 8
        off_b = vt_body_off + OUR_ALLOC_SLOT * 8
        qa = struct.unpack_from("<Q", obj, off_a)[0]
        qb = struct.unpack_from("<Q", obj, off_b)[0]
        struct.pack_into("<Q", obj, off_a, qb)
        struct.pack_into("<Q", obj, off_b, qa)
        print(f"MetaClass vtable fixup ({vt_sym_name}): swapped slot {REAL_ALLOC_SLOT} "
              f"({qa:#x}) <-> slot {OUR_ALLOC_SLOT} ({qb:#x})")

    # ---- Fix up the (much bigger) instance vtables the same way ----
    # Confirmed via a real data-abort boot test: IOService::probeCandidates's
    # `inst->attach(this)` call read slot 108 (offset 0x360) expecting
    # IOService::attach()'s real address there -- ours held something else
    # entirely, same class of header-order mismatch as the MetaClass vtable,
    # just much bigger (273+ real slots) and therefore far too error-prone to
    # fix slot-by-slot by hand. Since Itanium ABI guarantees inherited slots
    # keep the same position in every derived class's vtable, CLONE the real,
    # live base-class vtable verbatim (already correct by construction) and
    # patch in only the slots we genuinely override, at their real slot index
    # (independently confirmed per class/method by decoding that base class's
    # own real vtable and searching for its real function addresses -- see
    # project memory for exactly how each index below was found).
    INSTANCE_FIXUPS = [
        {
            "our_vt_sym": "__ZTV16InfernoVGPUHello",
            "next_const_sym": "__ZTVN16InfernoVGPUHello9MetaClassE",
            "base_vtable_va": 0xfffffff0077001e0,  # real __ZTV9IOService
            "destructor_syms": ("__ZN16InfernoVGPUHelloD1Ev", "__ZN16InfernoVGPUHelloD0Ev"),
            "overrides": {
                # NOTE: getMetaClass() (slot 7) was tried here to fix
                # OSDynamicCast() on our objects (see project memory), and it
                # DID fix that -- but caused watchdog panics under normal
                # idle load some time after boot, twice, only with that
                # patch present. Reverted: something else in the kernel's
                # own housekeeping evidently also walks live services'
                # metaclasses via this slot and doesn't tolerate whatever's
                # still wrong deeper in our hand-linked OSMetaClass instance
                # (same underlying issue as the applyToInstancesOfClassName
                # crash). Worked around at the call site instead (a plain
                # cast instead of OSDynamicCast in
                # InfernoVGPUUserClient::start()) rather than fixing this
                # vtable slot globally -- revisit only with a real fix for
                # the OSMetaClass field layout itself, not another
                # single-slot patch.
                86: "__ZN16InfernoVGPUHello5startEP9IOService",  # IOService::start real slot
                # Found by decoding real __ZTV9IOService (0xfffffff0077001e0)
                # for the qword matching real IOService::newUserClient's real
                # address (0xfffffff0080042f0) -- without this, IOServiceOpen()
                # from real userspace dispatches through the *cloned* (i.e.
                # IOService's own default, kIOReturnUnsupported-returning)
                # slot instead of ours, confirmed live: IOServiceOpen returned
                # 0xe00002c7 (kIOReturnUnsupported) despite the service being
                # found correctly, with our own newUserClient() never running.
                # (First attempt used slot 142 -- off by 2, forgot the real
                # vtable scan must start at base_vtable_va+0x10 same as the
                # clone loop below; confirmed the +0x10 methodology against
                # the already-known-good slot 86 (IOService::start) landing
                # exactly on itself before trusting slot 140 for this one.)
                140: "__ZN16InfernoVGPUHello13newUserClientEP4taskPvjP12OSDictionaryPP12IOUserClient",
            },
        },
        {
            "our_vt_sym": "__ZTV21InfernoVGPUUserClient",
            "next_const_sym": "__ZTVN21InfernoVGPUUserClient9MetaClassE",
            "base_vtable_va": 0xfffffff00770d018,  # real __ZTV12IOUserClient
            "destructor_syms": ("__ZN21InfernoVGPUUserClientD1Ev", "__ZN21InfernoVGPUUserClientD0Ev"),
            "overrides": {
                86: "__ZN21InfernoVGPUUserClient5startEP9IOService",  # inherited IOService::start real slot
                168: "__ZN21InfernoVGPUUserClient14externalMethodEjP25IOExternalMethodArgumentsP24IOExternalMethodDispatchP8OSObjectPv",
                170: "__ZN21InfernoVGPUUserClient12initWithTaskEP4taskPvjP12OSDictionary",
                172: "__ZN21InfernoVGPUUserClient11clientCloseEv",
            },
        },
    ]
    for spec in INSTANCE_FIXUPS:
        instance_vt_sym = symtab_by_name.get(spec["our_vt_sym"])
        if not instance_vt_sym:
            print(f"WARNING: {spec['our_vt_sym']} not found, skipping instance vtable fixup")
            continue
        real_body_off = va2off(kc_data, spec["base_vtable_va"] + 0x10)
        next_sym = symtab_by_name[spec["next_const_sym"]]
        n_slots = (next_sym["value"] - instance_vt_sym["value"] - 0x10) // 8
        our_body_off = const_off + (instance_vt_sym["value"] - const["addr"]) + 0x10
        for i in range(n_slots):
            raw = struct.unpack_from("<Q", kc_data, real_body_off + i * 8)[0]
            real_va = decode_chained_qword(raw)
            struct.pack_into("<Q", obj, our_body_off + i * 8, real_va)
        d1_name, d0_name = spec["destructor_syms"]
        for slot, name in ((0, d1_name), (1, d0_name)):
            va = local_va(symtab_by_name[name])
            struct.pack_into("<Q", obj, our_body_off + slot * 8, va)
        for slot, name in spec["overrides"].items():
            va = local_va(symtab_by_name[name])
            struct.pack_into("<Q", obj, our_body_off + slot * 8, va)
        print(f"Instance vtable fixup ({spec['our_vt_sym']}): cloned {n_slots} slots, "
              f"restored destructor at slots 0,1, patched overrides at "
              f"{sorted(spec['overrides'].keys())}")

    # ---- Emit final flat blob: __text + __const + __cstring + mod_init + mod_term ----
    blob_size = sections["__mod_term_func"]["addr"] + sections["__mod_term_func"]["size"]
    blob = bytearray(blob_size)
    for name in ("__text", "__const", "__cstring", "__mod_init_func", "__mod_term_func"):
        sec = sections[name]
        blob[sec["addr"]:sec["addr"] + sec["size"]] = obj[sec["offset"]:sec["offset"] + sec["size"]]

    with open("resolved_blob.bin", "wb") as f:
        f.write(blob)
    print(f"OK: wrote resolved_blob.bin, {len(blob)} bytes, all {len(relocs['text'])+len(relocs['const'])+1} relocations resolved")
    print(f"CODE_BASE = {CODE_BASE:#x}  COMMON_BASE (gMetaClass) = {COMMON_BASE:#x}")
    print(f"reserved-slot cache resolved: {len(reserved_cache)} unique symbols")
    for k, v in reserved_cache.items():
        print(f"   {k} -> {v:#x}")


if __name__ == "__main__":
    main()
