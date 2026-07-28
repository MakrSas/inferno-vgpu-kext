// Step 2 of the injection probe: a minimal, self-contained kernel-context
// function meant to overwrite com.apple.driver.AppleBCMWLANCore's reserved
// (and, in this VM, dead: Inferno already strips `wlan` from the device
// tree) 1,635,856-byte code slot at 0xfffffff009407e10, without touching that
// kext's kmod_info/personality plumbing yet -- just proving that injected
// code at that address can execute and leave observable state behind.
//
// First attempt used a file-local static (`g_marker`) for the write target,
// referenced via ADRP+ADD -- that turned out to need 2 relocations (__TEXT
// and __DATA are different sections, so the assembler can't bake the
// page-relative immediate without a real link step, which we don't have
// against a hand-placed kernelcache slot). This version hardcodes the target
// as an absolute 64-bit immediate instead: compiles to a MOVZ/MOVK sequence
// baked entirely at compile time, zero relocations, safe to copy as raw
// bytes to any load address.
//
// Marker address chosen well inside the same already-reserved slot (+0x10000,
// nowhere near this tiny function's own code), valid only because this VM's
// boot config sets kaslr-off=true (kernel virtual slide is 0, confirmed via
// `info: Kernel Virtual Slide: 0x0000000000000000` in the boot log) --
// otherwise this address would need to track the slide.
#define BCMWLANCORE_SLOT_BASE 0xfffffff009407e10ULL
#define MARKER_ADDR (BCMWLANCORE_SLOT_BASE + 0x10000ULL)

__attribute__((used))
void kmod_hello_start(void)
{
	*(volatile unsigned int *)MARKER_ADDR = 0xCA11AB1Eu;
}
