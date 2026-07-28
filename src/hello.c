// Minimal freestanding probe: no libSystem, no external symbol references at
// all. The only question this file exists to answer is "can we produce valid
// arm64e kernel-context Mach-O code on a macOS GitHub Actions runner, from an
// iOS-targeted clang invocation, with no Xcode kext project template to lean
// on" -- nothing here is meant to run yet.

// A dummy IOKit-shaped vtable slot to get pointer-authenticated code
// generation exercised (arm64e signs vtable/return addresses); a plain
// C function alone would not tell us anything about arm64e PAC codegen.
struct fake_vtable {
	void (*slot0)(void *self);
};

static void hello_start(void *self)
{
	(void)self;
}

const struct fake_vtable g_fake_vtable = {
	.slot0 = hello_start,
};

int hello_marker(void)
{
	return 0x1CE0FFEE;
}
