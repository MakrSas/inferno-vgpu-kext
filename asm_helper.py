#!/usr/bin/env python3
"""Small, self-checking AArch64 instruction encoder for the block_invoke
patch. Every helper is verified against real bytes captured from the
unmodified ___MTLCreateSystemDefaultDevice_block_invoke disassembly before
being trusted for the live patch -- see verify_against_known() below.
"""
import struct


def sub_imm(rd, rn, imm12, sf=1):
    assert 0 <= imm12 < 4096
    base = 0xD1000000 if sf else 0x51000000
    return base | (imm12 << 10) | (rn << 5) | rd


def add_imm(rd, rn, imm12, sf=1):
    assert 0 <= imm12 < 4096
    base = 0x91000000 if sf else 0x11000000
    return base | (imm12 << 10) | (rn << 5) | rd


def movz(rd, imm16, hw=0, sf=1):
    assert 0 <= imm16 < 65536
    base = 0xD2800000 if sf else 0x52800000
    return base | (hw << 21) | (imm16 << 5) | rd


def movk(rd, imm16, hw=0, sf=1):
    assert 0 <= imm16 < 65536
    base = 0xF2800000 if sf else 0x72800000
    return base | (hw << 21) | (imm16 << 5) | rd


def mov_reg(rd, rm, sf=1):
    # MOV (register) = ORR Xd, XZR, Xm
    base = 0xAA0003E0 if sf else 0x2A0003E0
    return base | (rm << 16) | rd


def str_imm(rt, rn, imm, size=8):
    # unsigned-offset STR, 64-bit (size=8) or 32-bit (size=4)
    assert imm % size == 0
    imm12 = imm // size
    assert 0 <= imm12 < 4096
    base = 0xF9000000 if size == 8 else 0xB9000000
    return base | (imm12 << 10) | (rn << 5) | rt


def ldr_imm(rt, rn, imm, size=8):
    assert imm % size == 0
    imm12 = imm // size
    assert 0 <= imm12 < 4096
    base = 0xF9400000 if size == 8 else 0xB9400000
    return base | (imm12 << 10) | (rn << 5) | rt


def cbz(rt, delta_bytes, sf=1):
    assert delta_bytes % 4 == 0
    imm19 = (delta_bytes // 4) & 0x7FFFF
    return 0xB4000000 | (sf << 31) | (imm19 << 5) | rt


def bl(pc, target):
    delta = target - pc
    assert delta % 4 == 0
    imm26 = (delta // 4) & 0x3FFFFFF
    return 0x94000000 | imm26


def b(pc, target):
    delta = target - pc
    assert delta % 4 == 0
    imm26 = (delta // 4) & 0x3FFFFFF
    return 0x14000000 | imm26


def blr(rn):
    return 0xD63F0000 | (rn << 5)


SP = 31


def verify_against_known():
    # Real bytes captured from ipsw disassembly of the unmodified function,
    # little-endian 4-byte words -> checked against our encoders.
    checks = [
        # (bytes_hex, expected_mnemonic_desc, our_encoding_call)
        ("ff0302d1", "sub sp,sp,#0x80", sub_imm(SP, SP, 0x80)),
        ("fd830091", "add fp,sp,#0x20 (Rd=29,Rn=31)", add_imm(29, SP, 0x20)),
        ("024980 52".replace(" ", ""), "movz w2,#0x248 (32-bit)", movz(2, 0x248, sf=0)),
        ("a0fe42f9", "ldr x0,[x21,#0x5f8]", ldr_imm(0, 21, 0x5f8)),
        ("6812 40f9".replace(" ", ""), "ldr x8,[x19,#0x20]", ldr_imm(8, 19, 0x20)),
        ("0805 40f9".replace(" ", ""), "ldr x8,[x8,#0x8]", ldr_imm(8, 8, 0x8)),
        ("0015 00f9".replace(" ", ""), "str x0,[x8,#0x28]", str_imm(0, 8, 0x28)),
        ("e10314aa", "mov x1,x20", mov_reg(1, 20)),
        ("a84b5496", "bl 0x1905635b0 (from pc 0x197050710)",
         bl(0x197050710, 0x1905635b0)),
        ("e00100b4", "cbz x0,0x197050750 (from pc 0x197050714)",
         cbz(0, 0x197050750 - 0x197050714)),
        ("dbf00014", "b 0x19708caf4 (from pc 0x197050788)",
         b(0x197050788, 0x19708caf4)),
    ]
    ok = True
    for hexbytes, desc, got in checks:
        raw = bytes.fromhex(hexbytes)
        expected = struct.unpack("<I", raw)[0]
        status = "OK" if expected == got else "MISMATCH"
        if expected != got:
            ok = False
        print(f"[{status}] {desc}: expected={expected:#010x} got={got:#010x}")
    assert ok, "encoder verification FAILED -- do not trust these encodings"
    print("all encoders verified OK")


if __name__ == "__main__":
    verify_against_known()
