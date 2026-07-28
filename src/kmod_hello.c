// Step 2 of the injection probe: a minimal, self-contained kernel-context
// function meant to be dropped into an already-reserved kext code slot
// (replacing com.apple.driver.AppleBCMWLANCore, see project memory) without
// touching that kext's kmod_info/personality plumbing yet -- just proving
// that injected code at that address can execute and leave observable state
// behind.
//
// Constraints, deliberately:
//  - No external symbol references of any kind (no libSystem, no other
//    kext's exports, no kernel exports). Everything here must resolve via
//    PC-relative addressing within this one translation unit, because we are
//    not running a real link/relocation step against the kernelcache -- we
//    are copying raw .text bytes into an existing slot.
//  - `g_marker` is a file-local static, so accesses to it are ADRP+ADD/STR
//    (PC-relative, resolved by the assembler already, no GOT, no external
//    relocation) -- safe to relocate as a raw byte blob to any load address.

static volatile unsigned int g_marker __attribute__((section("__DATA,__data"))) = 0;

__attribute__((used))
void kmod_hello_start(void)
{
	g_marker = 0xCA11AB1Eu;
}
