#!/usr/bin/env python3
"""Faithful Python port of QEMU's QARMA5 PAC implementation
(target/arm/tcg/pauth_helper.c), for signing our own function pointers so
they authenticate correctly under BLRAA. See project_inferno_gpu.md memory
for why this is needed (structor calls in OSRuntimeCallStructorsInSection
are PAC-authenticated: `blraa x8, x22` with modifier=0x4a27).
"""

MASK64 = 0xFFFFFFFFFFFFFFFF


def extract64(v, start, length):
    return (v >> start) & ((1 << length) - 1)


def pac_cell_shuffle(i):
    o = 0
    o |= extract64(i, 52, 4)
    o |= extract64(i, 24, 4) << 4
    o |= extract64(i, 44, 4) << 8
    o |= extract64(i, 0, 4) << 12
    o |= extract64(i, 28, 4) << 16
    o |= extract64(i, 48, 4) << 20
    o |= extract64(i, 4, 4) << 24
    o |= extract64(i, 40, 4) << 28
    o |= extract64(i, 32, 4) << 32
    o |= extract64(i, 12, 4) << 36
    o |= extract64(i, 56, 4) << 40
    o |= extract64(i, 20, 4) << 44
    o |= extract64(i, 8, 4) << 48
    o |= extract64(i, 36, 4) << 52
    o |= extract64(i, 16, 4) << 56
    o |= extract64(i, 60, 4) << 60
    return o & MASK64


def pac_cell_inv_shuffle(i):
    o = 0
    o |= extract64(i, 12, 4)
    o |= extract64(i, 24, 4) << 4
    o |= extract64(i, 48, 4) << 8
    o |= extract64(i, 36, 4) << 12
    o |= extract64(i, 56, 4) << 16
    o |= extract64(i, 44, 4) << 20
    o |= extract64(i, 4, 4) << 24
    o |= extract64(i, 16, 4) << 28
    o |= i & (0xF << 32)
    o |= extract64(i, 52, 4) << 36
    o |= extract64(i, 28, 4) << 40
    o |= extract64(i, 8, 4) << 44
    o |= extract64(i, 20, 4) << 48
    o |= extract64(i, 0, 4) << 52
    o |= extract64(i, 40, 4) << 56
    o |= i & (0xF << 60)
    return o & MASK64


_SUB = [0xB, 0x6, 0x8, 0xF, 0xC, 0x0, 0x9, 0xE, 0x3, 0x7, 0x4, 0x5, 0xD, 0x2, 0x1, 0xA]
_INV_SUB = [0x5, 0xE, 0xD, 0x8, 0xA, 0xB, 0x1, 0x9, 0x2, 0x6, 0xF, 0x0, 0x4, 0xC, 0x7, 0x3]


def pac_sub(i):
    o = 0
    for b in range(0, 64, 4):
        o |= _SUB[(i >> b) & 0xF] << b
    return o


def pac_inv_sub(i):
    o = 0
    for b in range(0, 64, 4):
        o |= _INV_SUB[(i >> b) & 0xF] << b
    return o


def rot_cell(cell, n):
    cell |= cell << 4
    return extract64(cell, 4 - n, 4)


def pac_mult(i):
    o = 0
    for b in range(0, 16, 4):
        i0 = extract64(i, b, 4)
        i4 = extract64(i, b + 16, 4)
        i8 = extract64(i, b + 32, 4)
        ic = extract64(i, b + 48, 4)

        t0 = rot_cell(i8, 1) ^ rot_cell(i4, 2) ^ rot_cell(i0, 1)
        t1 = rot_cell(ic, 1) ^ rot_cell(i4, 1) ^ rot_cell(i0, 2)
        t2 = rot_cell(ic, 2) ^ rot_cell(i8, 1) ^ rot_cell(i0, 1)
        t3 = rot_cell(ic, 1) ^ rot_cell(i8, 2) ^ rot_cell(i4, 1)

        o |= t3 << b
        o |= t2 << (b + 16)
        o |= t1 << (b + 32)
        o |= t0 << (b + 48)
    return o & MASK64


def tweak_cell_rot(cell):
    return ((cell >> 1) | (((cell ^ (cell >> 1)) & 1) << 3)) & 0xF


def tweak_shuffle(i):
    o = 0
    o |= extract64(i, 16, 4) << 0
    o |= extract64(i, 20, 4) << 4
    o |= tweak_cell_rot(extract64(i, 24, 4)) << 8
    o |= extract64(i, 28, 4) << 12
    o |= tweak_cell_rot(extract64(i, 44, 4)) << 16
    o |= extract64(i, 8, 4) << 20
    o |= extract64(i, 12, 4) << 24
    o |= tweak_cell_rot(extract64(i, 32, 4)) << 28
    o |= extract64(i, 48, 4) << 32
    o |= extract64(i, 52, 4) << 36
    o |= extract64(i, 56, 4) << 40
    o |= tweak_cell_rot(extract64(i, 60, 4)) << 44
    o |= tweak_cell_rot(extract64(i, 0, 4)) << 48
    o |= extract64(i, 4, 4) << 52
    o |= tweak_cell_rot(extract64(i, 40, 4)) << 56
    o |= tweak_cell_rot(extract64(i, 36, 4)) << 60
    return o & MASK64


def tweak_cell_inv_rot(cell):
    return (((cell << 1) & 0xF) | ((cell & 1) ^ (cell >> 3))) & 0xF


def tweak_inv_shuffle(i):
    o = 0
    o |= tweak_cell_inv_rot(extract64(i, 48, 4))
    o |= extract64(i, 52, 4) << 4
    o |= extract64(i, 20, 4) << 8
    o |= extract64(i, 24, 4) << 12
    o |= extract64(i, 0, 4) << 16
    o |= extract64(i, 4, 4) << 20
    o |= tweak_cell_inv_rot(extract64(i, 8, 4)) << 24
    o |= extract64(i, 12, 4) << 28
    o |= tweak_cell_inv_rot(extract64(i, 28, 4)) << 32
    o |= tweak_cell_inv_rot(extract64(i, 60, 4)) << 36
    o |= tweak_cell_inv_rot(extract64(i, 56, 4)) << 40
    o |= tweak_cell_inv_rot(extract64(i, 16, 4)) << 44
    o |= extract64(i, 32, 4) << 48
    o |= extract64(i, 36, 4) << 52
    o |= extract64(i, 40, 4) << 56
    o |= tweak_cell_inv_rot(extract64(i, 44, 4)) << 60
    return o & MASK64


_RC = [
    0x0000000000000000,
    0x13198A2E03707344,
    0xA4093822299F31D0,
    0x082EFA98EC4E6C89,
    0x452821E638D01377,
]
_ALPHA = 0xC0AC29B7C97C50DD


def pauth_computepac_architected(data, modifier, key_lo, key_hi, isqarma3=False):
    iterations = 2 if isqarma3 else 4
    key0, key1 = key_hi, key_lo
    sub = pac_sub if not isqarma3 else None  # qarma3 unused here (isqarma3=False always for us)

    modk0 = ((key0 << 63) | ((key0 >> 1) ^ (key0 >> 63))) & MASK64
    runningmod = modifier
    workingval = (data ^ key0) & MASK64

    for i in range(0, iterations + 1):
        roundkey = key1 ^ runningmod
        workingval ^= roundkey
        workingval ^= _RC[i]
        if i > 0:
            workingval = pac_cell_shuffle(workingval)
            workingval = pac_mult(workingval)
        workingval = pac_sub(workingval)
        runningmod = tweak_shuffle(runningmod)

    roundkey = modk0 ^ runningmod
    workingval ^= roundkey
    workingval = pac_cell_shuffle(workingval)
    workingval = pac_mult(workingval)
    workingval = pac_sub(workingval)
    workingval = pac_cell_shuffle(workingval)
    workingval = pac_mult(workingval)
    workingval ^= key1
    workingval = pac_cell_inv_shuffle(workingval)
    workingval = pac_inv_sub(workingval)
    workingval = pac_mult(workingval)
    workingval = pac_cell_inv_shuffle(workingval)
    workingval ^= key0
    workingval ^= runningmod

    for i in range(0, iterations + 1):
        workingval = pac_inv_sub(workingval)
        if i < iterations:
            workingval = pac_mult(workingval)
            workingval = pac_cell_inv_shuffle(workingval)
        runningmod = tweak_inv_shuffle(runningmod)
        roundkey = key1 ^ runningmod
        workingval ^= _RC[iterations - i]
        workingval ^= roundkey
        workingval ^= _ALPHA

    workingval ^= modk0
    return workingval & MASK64


def sign_pointer(ptr, modifier, key_lo, key_hi, tbi=True, va_bits=64 - 25):
    """Port of pauth_addpac (data=ptr, no data-pointer TBI/tag distinction
    needed for our case: instruction pointer, TBI on, standard kernel VA
    (top 25 bits sign-extended/canonical -- matches kernel's own tsz=25,
    i.e. va_bits=39 user-visible bits... actually for kernel EL1 TTBR1
    region with TCR_EL1.T1SZ, top_bit/bot_bit come from param.tbi/tsz. We
    hardcode the values seen in practice for kernel VAs: tbi=1, and the
    "good extension bits" check simply requires bits[bot_bit:top_bit] to
    be all-1 (canonical kernel address, which every 0xfffffff0... VA is).
    """
    top_bit = 64 - 8 * (1 if tbi else 0)  # tbi=1 -> top_bit=56
    bot_bit = va_bits  # bits below this are the real address; tune per TCR

    ext = (ptr >> 55) & 1
    if ext:
        ext = -1 & MASK64  # sign-extend to all-1s conceptually; we just need bit tests below
    # ext_ptr: ptr with [bot_bit:top_bit) replaced by sign-extension of bit55
    sign_bit = (ptr >> 55) & 1
    fill = MASK64 if sign_bit else 0
    field_mask = ((1 << (top_bit - bot_bit)) - 1) << bot_bit
    ext_ptr = (ptr & ~field_mask & MASK64) | (fill & field_mask)

    pac = pauth_computepac_architected(ext_ptr, modifier, key_lo, key_hi, isqarma3=False)

    # "good extension" check: bits [bot_bit:top_bit) of ptr must be all-0 or all-1
    test_field = (ptr & field_mask) >> bot_bit
    ones = (1 << (top_bit - bot_bit)) - 1
    good_ext = (test_field == 0) or (test_field == ones)
    if not good_ext:
        # PauthFeat_2 not modeled; older behavior corrupts one bit. We assume
        # canonical kernel addresses always pass (they do, by construction).
        pass

    # tbi path: keep ptr[0:bot_bit), pac occupies [bot_bit:55), preserve bit55
    ptr_out = ptr & ((1 << bot_bit) - 1)
    pac_out = pac & (((1 << (55 - bot_bit + 1)) - 1) << bot_bit)
    ext_bit = (ptr >> 55) & 1
    result = pac_out | (ext_bit << 55) | ptr_out
    return result & MASK64


if __name__ == "__main__":
    import sys
    # Self-test placeholder; real validation happens via the actual boot test.
    ptr = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0xfffffff009428220
    modifier = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0x4a27
    key_lo = int(sys.argv[3], 16) if len(sys.argv) > 3 else 0x000074c600009566
    key_hi = int(sys.argv[4], 16) if len(sys.argv) > 4 else 0x59249809bd945e78
    signed = sign_pointer(ptr, modifier, key_lo, key_hi)
    print(f"ptr={ptr:#x} modifier={modifier:#x}")
    print(f"key_lo={key_lo:#x} key_hi={key_hi:#x}")
    print(f"signed={signed:#x}")
