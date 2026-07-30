// Real IOKit driver class, compiled against macOS SDK's Kernel.framework
// headers (Xcode 15.4 ships them under
// System/Library/Frameworks/Kernel.framework/Versions/A/Headers/IOKit/ --
// no Apple KDK needed) but targeting the arm64e-apple-ios kernel-context
// triple for actual codegen. XNU's IOKit ABI is shared between macOS and iOS
// of the same kernel generation, so this is the standard trick used across
// the jailbreak-development community for building iOS kexts without an
// iOS-specific kext SDK (which Apple never shipped -- iOS never officially
// supported loadable kexts).
//
// Confirmed working end to end: a hand-linked build of this class, injected
// into a real (unmodified-toolchain) iOS kernelcache's __kmod_init/personality
// machinery, genuinely runs its whole IOKit lifecycle -- OSMetaClass
// registration, alloc(), init(), attach(), probe(), start() -- inside a live,
// booting XNU kernel, and genuinely talks to a real QEMU device
// (inferno-vgpu-v1) via IOMemoryDescriptor::withPhysicalAddress(), confirmed
// both guest-side (scratch readback) and from the device's own access trace.
//
// See inferno-vgpu-kext/resolve.py and InfernoData/patch_kernelcache.py in
// the Inferno repo for the injection pipeline; the vtable-slot layout
// produced by this exact SDK header does NOT match the real internal one
// used to build the target kernelcache (confirmed empirically), so
// resolve.py corrects known-mismatched slots post-compile. The same
// pipeline also replaces every `-fapple-kext`-miscompiled indirect call to
// an inherited, un-overridden virtual method (both explicit super-calls and
// plain `this->method()` calls) with a direct `bl` to the method's real
// exported symbol -- see resolve.py's SUPERCALL_FIXUPS/PLAIN_CALL_FIXUPS.
// Every new virtual call added here needs its discriminator added to one of
// those tables after the next CI compile+resolve cycle.

#include <IOKit/IOService.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOLib.h> // IOSleep()

// Real, exported XNU-internal function (not part of any public IOKit header
// -- hand-declared here) that walks the live kernel page tables to translate
// a kernel virtual address to its actual physical address. A plain C
// function, not virtual, so no -fapple-kext dispatch-fixup concerns at all --
// resolves as an ordinary direct call like every other kernel-export BL in
// this file. Needed because carved/reserved RAM (like the COMMON_BASE
// scratch region) does NOT follow the kernel image's own physmap formula
// (phys = virt - kernel_virt_base + kernel_phys_base) -- confirmed wrong,
// twice, live: the carve's real physical backing address isn't even stable
// across rebuilds/reboots of the same kernelcache, so no hardcoded constant
// can ever be correct here; it must be looked up fresh every boot.
extern "C" uint64_t kvtophys(uintptr_t va);

// Real, exported XNU kernel-internal thread primitives, used for the
// boot-time present-dispatch retry loop below (IOTimerEventSource/
// IOWorkLoop's event-source machinery was tried first, but confirmed via
// kernel-symbols.txt that this exact kernelcache build exports NO
// IOTimerEventSource methods at all, so that whole approach is unusable
// here). No extern declarations needed for these -- kern/thread.h and
// mach/thread_act.h, transitively included via the IOKit/Kernel.framework
// headers this file already pulls in, declare kernel_thread_start,
// current_thread, thread_deallocate, and thread_terminate (and thread_t)
// already; redeclaring them with placeholder void*/int types (as an
// earlier version of this file did) conflicts with those real
// declarations and fails to compile.

// Markers for the first real-userspace test (IOServiceOpen -> newUserClient
// -> externalMethod -> GetVersion): written unconditionally the moment each
// stage is genuinely reached, independent of what the userspace caller does
// with any return value or whether its own stdout goes anywhere observable.
// Distinct offsets from every earlier scratch use in this file (0x40/0x100/
// 0x108/0x110/0x200/0x300/0x400-0x40c).
#define USERSPACE_NEWCLIENT_MARKER_ADDR 0xfffffff1020c4500ULL
#define USERSPACE_GETVERSION_MARKER_ADDR 0xfffffff1020c4508ULL
// Diagnostics for InfernoVGPUUserClient::start() -- IOServiceOpen was
// returning kIOReturnError from newUserClient()'s `!client->start(this)`
// branch; these pin down exactly which of the two ways start() can fail.
#define USERSPACE_SUPERSTART_MARKER_ADDR 0xfffffff1020c4510ULL
#define USERSPACE_DYNCAST_MARKER_ADDR 0xfffffff1020c4518ULL
// IOServiceOpen() from real userspace was returning kIOReturnNotPermitted
// (0xe00002e2) even after newUserClient() started returning success by every
// marker so far -- this pins down whether our own function really reaches
// its final `return kIOReturnSuccess`, or whether the compiler/some earlier
// path silently diverges (e.g. attach()/start() succeeding doesn't strictly
// prove control flow reaches line 319 unmodified).
#define USERSPACE_NEWCLIENT_RESULT_MARKER_ADDR 0xfffffff1020c4520ULL
// submitBootPresentDispatch() diagnostics: [0]=computed payload `total`
// size, [1]=1 once reached (before the size-guard), [2]=submitPacket()'s
// return status (0=ok) once it actually calls out.
#define BOOT_PRESENT_TOTAL_MARKER_ADDR 0xfffffff1020c4528ULL
#define BOOT_PRESENT_REACHED_MARKER_ADDR 0xfffffff1020c4530ULL
#define BOOT_PRESENT_STATUS_MARKER_ADDR 0xfffffff1020c4538ULL

// inferno-vgpu-v1's MMIO base, as mapped by t8030_create_inferno_vgpu_node()
// in this exact QEMU build/machine config (kaslr-off=true, fixed device
// tree) -- confirmed empirically via QMP `info mtree` showing
// `inferno-vgpu-v1.mmio` at this exact physical range.
#define INFERNO_VGPU_PHYS_BASE 0x23d100000ULL
#define INFERNO_VGPU_MMIO_SIZE 0x4000
#define INFERNO_VGPU_REG_CONTROL_FIFO 0x1000
#define INFERNO_VGPU_REG_FIFO_LENGTH 0x1004
#define INFERNO_VGPU_REG_FIFO_WRITTEN 0x1008
#define INFERNO_VGPU_REG_FIFO_READ 0x100c
#define INFERNO_VGPU_REG_MAIN_KICK 0x1024
#define INFERNO_VGPU_REG_FIFO_BASE_PAGE 0x1030
#define INFERNO_VGPU_REG_VERSION 0x1034

// A scratch write target inside the same writable 4KB carve InfernoVGPUHello
// already uses for gMetaClass (see resolve.py's COMMON_BASE) -- distinct
// offset from gMetaClass (0xe20) and the earlier one-off diagnostic probes
// (0x40/0x100/0x108/0x110/0x200), so nothing collides.
#define SCRATCH_ADDR 0xfffffff1020c4300ULL

// A small test FIFO ring, placed at a fixed offset within the same 4KB
// COMMON_BASE carve (well clear of gMetaClass/scratch above), used only to
// verify the device's packet-framing/ring-math end to end -- not part of the
// real driver's eventual FIFO usage. Its PHYSICAL page (needed for
// FIFO_BASE_PAGE) is deliberately NOT a hardcoded constant here -- see
// kvtophys() above for why -- it's computed at runtime in
// submitTestFifoPacket() instead.
#define FIFO_TEST_RING_OFFSET 0x400
#define FIFO_TEST_RING_ADDR (0xfffffff1020c4000ULL + FIFO_TEST_RING_OFFSET)
#define INFERNO_VGPU_PAGE_SHIFT 14
#define FIFO_TEST_RING_LEN 0x1000

// See inferno-vgpu.h: forwarded near-verbatim to the host inferno-render-daemon
// over a Unix socket by the QEMU device model.
#define INFERNO_VGPU_OP_COMPUTE_DISPATCH 0x0002
#define INFERNO_VGPU_OP_DRAW 0x0003
#define INFERNO_VGPU_OP_PRESENT 0x0004

class InfernoVGPUHello : public IOService
{
	OSDeclareDefaultStructors(InfernoVGPUHello)

public:
	virtual bool start(IOService *provider) override;
	virtual IOReturn newUserClient(task_t owningTask, void *securityID,
	                                UInt32 type, OSDictionary *properties,
	                                IOUserClient **handler) override;

	uint32_t readVersionRegister(void);
	void submitTestFifoPacket(void);
	// Builds a real INFERNO_VGPU_OP_COMPUTE_DISPATCH packet from `payload`
	// (already in the wire format inferno-vgpu.h documents: air_len, air
	// bytes, buf_len, buf bytes) into the same test ring, kicks the device,
	// and returns the device's LAST_STATUS (0 = ok). The device overwrites
	// the buffer-bytes region of the ring in place with the real dispatch
	// result -- callers read it back via ringBase()/ringPayloadOffset().
	uint32_t submitComputeDispatch(const uint8_t *payload, uint32_t payloadLen);
	// Same idea, opcode INFERNO_VGPU_OP_DRAW: payload is vert AIR + frag AIR +
	// vertex bytes + width/height/vertex_count (see inferno-vgpu.h). On
	// success the device overwrites payload offset 0 in place with
	// width*height*4 bytes of RGBA8 pixels -- caller reads them back from
	// ringBase()+12 directly (no per-field offset math needed, unlike
	// compute's buffer-in-place convention).
	uint32_t submitDrawDispatch(const uint8_t *payload, uint32_t payloadLen);
	// Same idea, opcode INFERNO_VGPU_OP_PRESENT: payload is identical to
	// submitDrawDispatch's, plus a trailing dest_x/dest_y (see
	// inferno-vgpu.h). Unlike compute/draw, the device does NOT write
	// anything back into the ring -- the rendered frame goes straight to the
	// live display genpipe instead. Only LAST_STATUS matters to the caller.
	uint32_t submitPresentDispatch(const uint8_t *payload, uint32_t payloadLen);
	static volatile uint8_t *ringBase(void) { return (volatile uint8_t *)FIFO_TEST_RING_ADDR; }

	// Fires one hardcoded INFERNO_VGPU_OP_PRESENT (a real Metal triangle
	// draw, rendered onto the live display) straight from kernel context.
	// Entirely sidesteps every userspace-security mechanism (AMFI/
	// codesigning, the exec()-time SIGKILL that blocks every fresh
	// unsigned userspace binary, and the Sandbox.kext
	// com.apple.security.iokit-user-client-class entitlement gate that
	// blocks IOServiceOpen from already-running signed processes like
	// bash) since kernel code is subject to none of them. Returns the
	// device's LAST_STATUS (0=ok, 1=render failed, 2=no active display
	// genpipe yet -- see inferno-vgpu.h), or 0xffffffff if it never even
	// reached the device (bad size/no MMIO map).
	uint32_t submitBootPresentDispatch(void);
	// start() runs far too early for this -- the real iOS display driver
	// hasn't necessarily enabled any genpipe yet (confirmed empirically:
	// the render itself succeeds, inferno_vgpu_present_frame() just finds
	// no active genpipe to write into, so it correctly no-ops rather than
	// crash). Retries on a dedicated kernel thread (IOSleep between
	// attempts) instead of blocking start() itself (IOKit service matching
	// has its own patience for how long start() may block) until it
	// succeeds or gives up. NOT IOTimerEventSource -- confirmed via
	// kernel-symbols.txt that this exact kernelcache build exports no
	// IOTimerEventSource methods at all.
	static void presentRetryThreadMain(void *parameter, int wait_result);

private:
	uint32_t submitPacket(uint16_t opcode, const uint8_t *payload, uint32_t payloadLen);
	IOMemoryMap *fDeviceMap;
};

OSDefineMetaClassAndStructors(InfernoVGPUHello, IOService)

bool InfernoVGPUHello::start(IOService *provider)
{
	if (!IOService::start(provider)) {
		return false;
	}

	fDeviceMap = NULL;

	setProperty("IOMatchCategory", "IOAcceleratorES");
	setProperty("MetalPluginName", "InfernoVGPUMetal");
	setProperty("MetalPluginClassName", "InfernoVGPUMetalDevice");
	// IOServiceOpen() was returning kIOReturnNotPermitted (0xe00002e2) even
	// though newUserClient() reliably returns kIOReturnSuccess with a valid
	// handler (confirmed via a marker at the exact return statement) --
	// is_io_service_open_extended() itself never loads that error constant,
	// so it's coming from a post-newUserClient() check elsewhere in the
	// open path. The standard, Apple-documented way IOKit services declare
	// their user client class is this property; older manual newUserClient()
	// overrides like ours technically instantiate the client themselves
	// without needing it, but its *absence* may be what an entitlement/
	// consistency check elsewhere in this path is reading as "denied"
	// rather than "unrestricted".
	setProperty("IOUserClientClass", "InfernoVGPUUserClient");

	IOMemoryDescriptor *desc = IOMemoryDescriptor::withPhysicalAddress(
		INFERNO_VGPU_PHYS_BASE, INFERNO_VGPU_MMIO_SIZE, kIODirectionInOut);
	if (desc != NULL) {
		fDeviceMap = desc->map();
	}

	*(volatile uint32_t *)SCRATCH_ADDR = readVersionRegister();

	submitTestFifoPacket();

	thread_t presentThread = NULL;
	kernel_thread_start(&InfernoVGPUHello::presentRetryThreadMain, this, &presentThread);
	if (presentThread != NULL) {
		thread_deallocate(presentThread); // drop our reference; the thread runs detached
	}

	registerService();

	return true;
}

void InfernoVGPUHello::presentRetryThreadMain(void *parameter, int wait_result)
{
	(void)wait_result;
	// Plain cast, not OSDynamicCast -- see project notes elsewhere in this
	// file: our hand-linked OSMetaClass doesn't reliably support it, and we
	// are the only spawner of this thread, so it's exactly as correct here.
	InfernoVGPUHello *self = (InfernoVGPUHello *)parameter;
	// 15 attempts * 2s (30s total) was confirmed too short live: at 140s of
	// QEMU wall-clock boot the guest is still bringing up USB/multitouch,
	// nowhere near a live display genpipe yet. Widened to 100 * 3s (5min)
	// -- comfortably past observed emulated-boot time to a usable display,
	// still bounded so the thread can't spin forever if the genpipe truly
	// never comes up this boot.
	bool live = false;
	for (int attempt = 0; attempt < 100; attempt++) {
		IOSleep(3000);
		uint32_t status = self->submitBootPresentDispatch();
		if (status == 0) {
			live = true;
			break;
		}
		// else: keep retrying (e.g. status 2 == no active display genpipe
		// yet, expected for a good chunk of boot) until the attempt budget
		// above runs out.
	}
	// Confirmed live: a single successful present gets stomped by the very
	// next frame the real display driver draws (boot logo animation,
	// springboard, etc.) -- adp_v4_present_frame() blits straight into the
	// same live genpipe buffer the real driver keeps redrawing, so a
	// one-shot write is visible for at most one frame and isn't reliably
	// catchable in an externally-timed screendump. Once the genpipe is
	// confirmed live, keep re-presenting indefinitely so the frame stays
	// fresh; this is a standing proof-of-concept overlay, not meant to ever
	// stop on its own.
	if (live) {
		for (;;) {
			IOSleep(1000);
			self->submitBootPresentDispatch();
		}
	}
	thread_terminate(current_thread());
}

// Same vertex_passthrough/fragment_solid_red pair already proven end to end
// (host_render_poc, agx_metal_api_draw_test, bash_present_builtin) -- a
// trivial triangle: one float4 vertex attribute passed straight through,
// solid red fragment output.
static const char kBootPresentVertAir[] =
	"source_filename = \"vertex_passthrough.metal\"\n"
	"target datalayout = \"e-p:64:64:64\"\n"
	"target triple = \"air64-apple-macosx14.0.0\"\n"
	"\n"
	"define <4 x float> @vmain(<4 x float> %position) local_unnamed_addr #0 {\n"
	"  ret <4 x float> %position\n"
	"}\n"
	"\n"
	"attributes #0 = { nounwind }\n"
	"\n"
	"!air.vertex = !{!0}\n"
	"!0 = !{ptr @vmain, !1, !2}\n"
	"!1 = !{!3}\n"
	"!2 = !{!4}\n"
	"!3 = !{!\"air.position\", !\"air.arg_type_name\", !\"float4\"}\n"
	"!4 = !{i32 0, !\"air.vertex_input\", !\"air.location_index\", i32 0, i32 1, "
	"!\"air.arg_type_name\", !\"float4\", !\"air.arg_name\", !\"position\"}\n";

static const char kBootPresentFragAir[] =
	"source_filename = \"fragment_solid_red.metal\"\n"
	"target datalayout = \"e-p:64:64:64\"\n"
	"target triple = \"air64-apple-macosx14.0.0\"\n"
	"\n"
	"define <4 x float> @frag(<4 x float> %position) local_unnamed_addr #0 {\n"
	"  %r = insertelement <4 x float> undef, float 1.000000e+00, i64 0\n"
	"  %rg = insertelement <4 x float> %r, float 0.000000e+00, i64 1\n"
	"  %rgb = insertelement <4 x float> %rg, float 0.000000e+00, i64 2\n"
	"  %rgba = insertelement <4 x float> %rgb, float 1.000000e+00, i64 3\n"
	"  ret <4 x float> %rgba\n"
	"}\n"
	"\n"
	"attributes #0 = { nounwind }\n"
	"\n"
	"!air.fragment = !{!0}\n"
	"!0 = !{ptr @frag, !1, !2}\n"
	"!1 = !{!3}\n"
	"!2 = !{!4}\n"
	"!3 = !{i32 0, !\"air.render_target\", i32 0, i32 0, !\"air.arg_type_name\", !\"float4\"}\n"
	"!4 = !{i32 0, !\"air.position\", !\"air.center\", !\"air.arg_type_name\", !\"float4\"}\n";

uint32_t InfernoVGPUHello::submitBootPresentDispatch(void)
{
	const uint32_t vertLen = (uint32_t)(sizeof(kBootPresentVertAir) - 1);
	const uint32_t vertPad = (4 - (vertLen % 4)) % 4;
	const uint32_t fragLen = (uint32_t)(sizeof(kBootPresentFragAir) - 1);
	const uint32_t fragPad = (4 - (fragLen % 4)) % 4;

	float verts[3][4] = {
		{ 0.0f,  0.6f, 0.0f, 1.0f},
		{-0.6f, -0.6f, 0.0f, 1.0f},
		{ 0.6f, -0.6f, 0.0f, 1.0f},
	};
	const uint32_t vbufLen = (uint32_t)sizeof(verts);
	const uint32_t vbufPad = (4 - (vbufLen % 4)) % 4;

	const uint32_t width = 200, height = 200, vertexCount = 3, destX = 50, destY = 50;

	const uint32_t total = 4 + vertLen + vertPad + 4 + fragLen + fragPad +
	                       4 + vbufLen + vbufPad + 4 + 4 + 4 + 4 + 4;
	*(volatile uint32_t *)BOOT_PRESENT_TOTAL_MARKER_ADDR = total;
	*(volatile uint32_t *)BOOT_PRESENT_REACHED_MARKER_ADDR = 1;
	// The two AIR shader texts alone run ~1.1KB combined -- still well
	// under a typical kernel stack frame's headroom.
	uint8_t payload[2048];
	if (total > sizeof(payload)) {
		return 0xffffffff;
	}

	uint32_t off = 0;
	*(uint32_t *)(payload + off) = vertLen; off += 4;
	for (uint32_t i = 0; i < vertLen; i++) { payload[off + i] = (uint8_t)kBootPresentVertAir[i]; }
	off += vertLen + vertPad;
	*(uint32_t *)(payload + off) = fragLen; off += 4;
	for (uint32_t i = 0; i < fragLen; i++) { payload[off + i] = (uint8_t)kBootPresentFragAir[i]; }
	off += fragLen + fragPad;
	*(uint32_t *)(payload + off) = vbufLen; off += 4;
	for (uint32_t i = 0; i < vbufLen; i++) { payload[off + i] = ((const uint8_t *)verts)[i]; }
	off += vbufLen + vbufPad;
	*(uint32_t *)(payload + off) = width; off += 4;
	*(uint32_t *)(payload + off) = height; off += 4;
	*(uint32_t *)(payload + off) = vertexCount; off += 4;
	*(uint32_t *)(payload + off) = destX; off += 4;
	*(uint32_t *)(payload + off) = destY; off += 4;

	uint32_t status = submitPresentDispatch(payload, total);
	*(volatile uint32_t *)BOOT_PRESENT_STATUS_MARKER_ADDR = 0x600D0000 | (status & 0xff);
	return status;
}

uint32_t InfernoVGPUHello::readVersionRegister(void)
{
	if (fDeviceMap == NULL) {
		return 0;
	}
	volatile uint32_t *regs = (volatile uint32_t *)fDeviceMap->getVirtualAddress();
	return regs[INFERNO_VGPU_REG_VERSION / 4];
}

// One-shot smoke test for the FIFO packet framing/ring math itself -- writes
// one well-formed header-only packet (no payload) into a small test ring and
// kicks the device. Not part of the real driver's eventual FIFO usage (real
// command submission needs actual GPU work to describe); this only proves
// the ring/header format the device implements (see inferno-vgpu.c's
// inferno_vgpu_drain_fifo) is exactly what a real producer would write.
void InfernoVGPUHello::submitTestFifoPacket(void)
{
	if (fDeviceMap == NULL) {
		return;
	}

	volatile uint8_t *packet = (volatile uint8_t *)FIFO_TEST_RING_ADDR;
	// opcode=0x1234 (u16), stamp_count=0 (u16), total_size=12 (u32, header
	// only, no stamps/payload), completion_stamp=0xCAFEBABE (u32) -- little
	// endian, matching inferno-vgpu.h's documented packet layout.
	packet[0] = 0x34; packet[1] = 0x12;             // opcode
	packet[2] = 0x00; packet[3] = 0x00;              // stamp_count
	packet[4] = 0x0c; packet[5] = 0x00; packet[6] = 0x00; packet[7] = 0x00;  // total_size = 12
	packet[8] = 0xbe; packet[9] = 0xba; packet[10] = 0xfe; packet[11] = 0xca; // completion_stamp

	// Page-truncating a physical address that falls inside the target page
	// (rather than page-aligning the virtual address before translating)
	// gives the same page number either way, since the offset within the
	// page is preserved by kvtophys() and then discarded by the shift.
	uint64_t ring_phys = kvtophys((uintptr_t)FIFO_TEST_RING_ADDR);
	uint32_t ring_base_page = (uint32_t)(ring_phys >> INFERNO_VGPU_PAGE_SHIFT);

	volatile uint32_t *regs = (volatile uint32_t *)fDeviceMap->getVirtualAddress();
	regs[INFERNO_VGPU_REG_FIFO_BASE_PAGE / 4] = ring_base_page;
	regs[INFERNO_VGPU_REG_FIFO_LENGTH / 4] = 0x1000;
	regs[INFERNO_VGPU_REG_FIFO_READ / 4] = FIFO_TEST_RING_OFFSET;
	regs[INFERNO_VGPU_REG_CONTROL_FIFO / 4] = 1;
	regs[INFERNO_VGPU_REG_FIFO_WRITTEN / 4] = FIFO_TEST_RING_OFFSET + 12;
	regs[INFERNO_VGPU_REG_MAIN_KICK / 4] = 1;
}

uint32_t InfernoVGPUHello::submitPacket(uint16_t opcode, const uint8_t *payload, uint32_t payloadLen)
{
	if (fDeviceMap == NULL) {
		return 1;
	}
	// Header (12) + payload must fit the ring's fixed length -- same 4KB
	// budget submitTestFifoPacket() already lives inside.
	if (payloadLen > FIFO_TEST_RING_LEN - 12) {
		return 1;
	}

	volatile uint8_t *packet = ringBase();
	uint32_t totalSize = 12 + payloadLen;

	packet[0] = opcode & 0xff;
	packet[1] = (opcode >> 8) & 0xff;
	packet[2] = 0;
	packet[3] = 0;
	packet[4] = totalSize & 0xff;
	packet[5] = (totalSize >> 8) & 0xff;
	packet[6] = (totalSize >> 16) & 0xff;
	packet[7] = (totalSize >> 24) & 0xff;
	packet[8] = 0xbe;
	packet[9] = 0xba;
	packet[10] = 0xfe;
	packet[11] = 0xca;
	for (uint32_t i = 0; i < payloadLen; i++) {
		packet[12 + i] = payload[i];
	}

	uint64_t ring_phys = kvtophys((uintptr_t)FIFO_TEST_RING_ADDR);
	uint32_t ring_base_page = (uint32_t)(ring_phys >> INFERNO_VGPU_PAGE_SHIFT);

	volatile uint32_t *regs = (volatile uint32_t *)fDeviceMap->getVirtualAddress();
	regs[INFERNO_VGPU_REG_FIFO_BASE_PAGE / 4] = ring_base_page;
	regs[INFERNO_VGPU_REG_FIFO_LENGTH / 4] = FIFO_TEST_RING_LEN;
	regs[INFERNO_VGPU_REG_FIFO_READ / 4] = FIFO_TEST_RING_OFFSET;
	regs[INFERNO_VGPU_REG_CONTROL_FIFO / 4] = 1;
	regs[INFERNO_VGPU_REG_FIFO_WRITTEN / 4] = FIFO_TEST_RING_OFFSET + totalSize;
	regs[INFERNO_VGPU_REG_MAIN_KICK / 4] = 1;

	// LAST_STATUS: written synchronously by the device's drain (QEMU's MMIO
	// dispatch is single-threaded), 0 = ok. See inferno-vgpu.h.
	return regs[0x103c / 4];
}

uint32_t InfernoVGPUHello::submitComputeDispatch(const uint8_t *payload, uint32_t payloadLen)
{
	return submitPacket(INFERNO_VGPU_OP_COMPUTE_DISPATCH, payload, payloadLen);
}

uint32_t InfernoVGPUHello::submitDrawDispatch(const uint8_t *payload, uint32_t payloadLen)
{
	return submitPacket(INFERNO_VGPU_OP_DRAW, payload, payloadLen);
}

uint32_t InfernoVGPUHello::submitPresentDispatch(const uint8_t *payload, uint32_t payloadLen)
{
	return submitPacket(INFERNO_VGPU_OP_PRESENT, payload, payloadLen);
}

class InfernoVGPUUserClient : public IOUserClient
{
	OSDeclareDefaultStructors(InfernoVGPUUserClient)

public:
	virtual bool initWithTask(task_t owningTask, void *securityID, UInt32 type,
	                           OSDictionary *properties) override;
	virtual bool start(IOService *provider) override;
	virtual IOReturn clientClose(void) override;
	virtual IOReturn externalMethod(uint32_t selector,
	                                 IOExternalMethodArguments *arguments,
	                                 IOExternalMethodDispatch *dispatch,
	                                 OSObject *target, void *reference) override;

	static IOReturn sGetVersion(InfernoVGPUUserClient *target, void *reference,
	                             IOExternalMethodArguments *arguments);
	static IOReturn sComputeDispatch(InfernoVGPUUserClient *target, void *reference,
	                                   IOExternalMethodArguments *arguments);
	static IOReturn sDrawDispatch(InfernoVGPUUserClient *target, void *reference,
	                                IOExternalMethodArguments *arguments);
	static IOReturn sPresentDispatch(InfernoVGPUUserClient *target, void *reference,
	                                   IOExternalMethodArguments *arguments);

private:
	InfernoVGPUHello *fProvider;
};

OSDefineMetaClassAndStructors(InfernoVGPUUserClient, IOUserClient)

enum {
	kInfernoVGPUMethodGetVersion = 0,
	kInfernoVGPUMethodComputeDispatch = 1,
	kInfernoVGPUMethodDrawDispatch = 2,
	kInfernoVGPUMethodPresentDispatch = 3,
	kInfernoVGPUMethodCount
};

// IOExternalMethodDispatch = { function, checkScalarInputCount,
// checkStructureInputSize, checkScalarOutputCount, checkStructureOutputSize }.
// sGetVersion takes no input and returns exactly 1 scalar output
// (arguments->scalarOutput[0]) -- the last two fields here were transposed
// (0 scalar-out, 1 structure-out) which made IOUserClient::externalMethod's
// own built-in argument-count validation reject every real call with
// kIOReturnBadArgument before sGetVersion ever ran, confirmed live via
// IOConnectCallScalarMethod(..., &version, &outputCount=1) failing with
// 0xe00002c2 despite IOServiceOpen() itself succeeding.
// sComputeDispatch takes a variable-size structure input (the wire payload)
// and produces a variable-size structure output (the dispatch result) --
// kIOUCVariableStructureSize on both, no scalars either side.
static const IOExternalMethodDispatch sInfernoVGPUMethods[kInfernoVGPUMethodCount] = {
	{ (IOExternalMethodAction)&InfernoVGPUUserClient::sGetVersion, 0, 0, 1, 0 },
	{ (IOExternalMethodAction)&InfernoVGPUUserClient::sComputeDispatch, 0,
	  kIOUCVariableStructureSize, 0, kIOUCVariableStructureSize },
	{ (IOExternalMethodAction)&InfernoVGPUUserClient::sDrawDispatch, 0,
	  kIOUCVariableStructureSize, 0, kIOUCVariableStructureSize },
	// sPresentDispatch: variable-size structure input, no structure output
	// (nothing comes back into guest memory -- the frame goes straight to
	// the display), 1 scalar output (LAST_STATUS, 0 = ok).
	{ (IOExternalMethodAction)&InfernoVGPUUserClient::sPresentDispatch, 0,
	  kIOUCVariableStructureSize, 1, 0 },
};

bool InfernoVGPUUserClient::initWithTask(task_t owningTask, void *securityID,
                                          UInt32 type, OSDictionary *properties)
{
	if (!IOUserClient::initWithTask(owningTask, securityID, type, properties)) {
		return false;
	}
	fProvider = NULL;
	return true;
}

bool InfernoVGPUUserClient::start(IOService *provider)
{
	bool superOk = IOUserClient::start(provider);
	*(volatile uint32_t *)USERSPACE_SUPERSTART_MARKER_ADDR = superOk ? 1u : 2u;
	if (!superOk) {
		return false;
	}
	// Not OSDynamicCast(InfernoVGPUHello, provider): that walks
	// provider->getMetaClass()'s superClassLink chain, which needs our
	// hand-linked OSMetaClass instance's fields to be laid out exactly like
	// the real kernel's OSMetaClass (confirmed broken -- see the
	// applyToInstancesOfClassName crash notes in resolve.py/project memory).
	// Patching getMetaClass()'s vtable slot to fix *this* one check made the
	// system unstable (watchdog panics under normal idle load some time
	// later, twice, only after that patch -- something else in the kernel's
	// own housekeeping evidently also walks live services' metaclasses and
	// doesn't tolerate whatever's still wrong deeper in ours). We are the
	// only caller of newUserClient() on this exact class pair, so a plain
	// cast is exactly as correct here without touching that vtable slot.
	fProvider = static_cast<InfernoVGPUHello *>(provider);
	*(volatile uint32_t *)USERSPACE_DYNCAST_MARKER_ADDR = (fProvider != NULL) ? 1u : 2u;
	return fProvider != NULL;
}

IOReturn InfernoVGPUUserClient::clientClose(void)
{
	terminate();
	return kIOReturnSuccess;
}

IOReturn InfernoVGPUUserClient::externalMethod(uint32_t selector,
                                                IOExternalMethodArguments *arguments,
                                                IOExternalMethodDispatch *dispatch,
                                                OSObject *target, void *reference)
{
	if (selector >= kInfernoVGPUMethodCount) {
		return kIOReturnUnsupported;
	}
	return IOUserClient::externalMethod(selector, arguments,
	                                     (IOExternalMethodDispatch *)&sInfernoVGPUMethods[selector],
	                                     this, reference);
}

IOReturn InfernoVGPUUserClient::sGetVersion(InfernoVGPUUserClient *target,
                                             void *reference,
                                             IOExternalMethodArguments *arguments)
{
	// Written unconditionally the moment a real userspace caller's
	// externalMethod dispatch genuinely reaches here -- observable via QMP
	// regardless of whether the calling process's own stdout goes anywhere
	// visible.
	*(volatile uint32_t *)USERSPACE_GETVERSION_MARKER_ADDR = 0xB00B1E5;

	if (target == NULL || target->fProvider == NULL) {
		return kIOReturnNotReady;
	}
	arguments->scalarOutput[0] = target->fProvider->readVersionRegister();
	arguments->scalarOutputCount = 1;
	return kIOReturnSuccess;
}

// Input wire format (inferno-vgpu.h): u32 air_len, air_bytes[air_len]
// (4-byte padded), u32 buf_len, buf_bytes[buf_len] (4-byte padded). Kicks a
// real INFERNO_VGPU_OP_COMPUTE_DISPATCH through the provider, then copies
// the ring's now-mutated buffer bytes back out as the structure output.
IOReturn InfernoVGPUUserClient::sComputeDispatch(InfernoVGPUUserClient *target,
                                                   void *reference,
                                                   IOExternalMethodArguments *arguments)
{
	if (target == NULL || target->fProvider == NULL) {
		return kIOReturnNotReady;
	}

	const uint8_t *in = (const uint8_t *)arguments->structureInput;
	uint32_t inSize = arguments->structureInputSize;
	if (in == NULL || inSize < 8) {
		return kIOReturnBadArgument;
	}

	uint32_t airLen = in[0] | (in[1] << 8) | (in[2] << 16) | (in[3] << 24);
	uint32_t bufOff = 4 + ((airLen + 3) & ~3u);
	if (bufOff + 4 > inSize) {
		return kIOReturnBadArgument;
	}
	uint32_t bufLen = in[bufOff] | (in[bufOff + 1] << 8) | (in[bufOff + 2] << 16) | (in[bufOff + 3] << 24);
	if (bufOff + 4 + bufLen > inSize) {
		return kIOReturnBadArgument;
	}

	uint32_t status = target->fProvider->submitComputeDispatch(in, inSize);

	if (status == 0 && arguments->structureOutput != NULL &&
	   arguments->structureOutputSize >= bufLen) {
		volatile uint8_t *ring = InfernoVGPUHello::ringBase();
		uint8_t *out = (uint8_t *)arguments->structureOutput;
		uint32_t resultOff = 12 + bufOff + 4;
		for (uint32_t i = 0; i < bufLen; i++) {
			out[i] = ring[resultOff + i];
		}
		arguments->structureOutputSize = bufLen;
	} else {
		arguments->structureOutputSize = 0;
	}

	return (status == 0) ? kIOReturnSuccess : kIOReturnIOError;
}

// Input wire format (inferno-vgpu.h): u32 vert_air_len, vert_air_bytes[padded],
// u32 frag_air_len, frag_air_bytes[padded], u32 vbuf_len, vbuf_bytes[padded],
// u32 width, u32 height, u32 vertex_count. Kicks a real INFERNO_VGPU_OP_DRAW
// through the provider, then copies width*height*4 RGBA8 bytes back out of
// the ring's payload-start region (the device overwrites payload offset 0 in
// place -- see submitDrawDispatch/inferno-vgpu.h).
IOReturn InfernoVGPUUserClient::sDrawDispatch(InfernoVGPUUserClient *target,
                                                void *reference,
                                                IOExternalMethodArguments *arguments)
{
	if (target == NULL || target->fProvider == NULL) {
		return kIOReturnNotReady;
	}

	const uint8_t *in = (const uint8_t *)arguments->structureInput;
	uint32_t inSize = arguments->structureInputSize;
	if (in == NULL || inSize < 8) {
		return kIOReturnBadArgument;
	}

	uint32_t off = 0;
	uint32_t vertAirLen = in[off] | (in[off+1]<<8) | (in[off+2]<<16) | (in[off+3]<<24);
	off += 4 + ((vertAirLen + 3) & ~3u);
	if (off + 4 > inSize) {
		return kIOReturnBadArgument;
	}
	uint32_t fragAirLen = in[off] | (in[off+1]<<8) | (in[off+2]<<16) | (in[off+3]<<24);
	off += 4 + ((fragAirLen + 3) & ~3u);
	if (off + 4 > inSize) {
		return kIOReturnBadArgument;
	}
	uint32_t vbufLen = in[off] | (in[off+1]<<8) | (in[off+2]<<16) | (in[off+3]<<24);
	off += 4 + ((vbufLen + 3) & ~3u);
	if (off + 12 > inSize) {
		return kIOReturnBadArgument;
	}
	uint32_t width = in[off] | (in[off+1]<<8) | (in[off+2]<<16) | (in[off+3]<<24);
	uint32_t height = in[off+4] | (in[off+5]<<8) | (in[off+6]<<16) | (in[off+7]<<24);
	// vertex_count at off+8, not needed here -- the device parses it, not us.

	uint32_t pixelBytes = width * height * 4;
	if (pixelBytes == 0 || pixelBytes > inSize) {
		// Output must fit within the same payload region it overwrites --
		// see inferno-vgpu.h's documented constraint.
		return kIOReturnBadArgument;
	}

	uint32_t status = target->fProvider->submitDrawDispatch(in, inSize);

	if (status == 0 && arguments->structureOutput != NULL &&
	   arguments->structureOutputSize >= pixelBytes) {
		volatile uint8_t *ring = InfernoVGPUHello::ringBase();
		uint8_t *out = (uint8_t *)arguments->structureOutput;
		for (uint32_t i = 0; i < pixelBytes; i++) {
			out[i] = ring[12 + i];
		}
		arguments->structureOutputSize = pixelBytes;
	} else {
		arguments->structureOutputSize = 0;
	}

	return (status == 0) ? kIOReturnSuccess : kIOReturnIOError;
}

// Input wire format (inferno-vgpu.h): identical to sDrawDispatch's, plus a
// trailing dest_x/dest_y. Kicks a real INFERNO_VGPU_OP_PRESENT through the
// provider -- the device renders and blits the result straight onto the
// live display genpipe itself, so there is nothing to copy back here, only
// the resulting status.
IOReturn InfernoVGPUUserClient::sPresentDispatch(InfernoVGPUUserClient *target,
                                                   void *reference,
                                                   IOExternalMethodArguments *arguments)
{
	if (target == NULL || target->fProvider == NULL) {
		return kIOReturnNotReady;
	}

	const uint8_t *in = (const uint8_t *)arguments->structureInput;
	uint32_t inSize = arguments->structureInputSize;
	// Smallest legal payload: vert_air_len(4)+frag_air_len(4)+vbuf_len(4)
	// (each possibly 0) + width+height+vertex_count+dest_x+dest_y (20).
	if (in == NULL || inSize < 32) {
		return kIOReturnBadArgument;
	}

	uint32_t status = target->fProvider->submitPresentDispatch(in, inSize);

	arguments->scalarOutput[0] = status;
	arguments->scalarOutputCount = 1;

	return (status == 0) ? kIOReturnSuccess : kIOReturnIOError;
}

IOReturn InfernoVGPUHello::newUserClient(task_t owningTask, void *securityID,
                                          UInt32 type, OSDictionary *properties,
                                          IOUserClient **handler)
{
	// Same idea: proves IOServiceOpen really reached newUserClient() from
	// real userspace, independent of everything that follows.
	*(volatile uint32_t *)USERSPACE_NEWCLIENT_MARKER_ADDR = 0xCAFEF00D;

	InfernoVGPUUserClient *client = OSTypeAlloc(InfernoVGPUUserClient);

	if (client == NULL) {
		return kIOReturnNoMemory;
	}

	if (!client->initWithTask(owningTask, securityID, type, properties)) {
		client->release();
		return kIOReturnBadArgument;
	}

	if (!client->attach(this)) {
		client->release();
		return kIOReturnError;
	}

	if (!client->start(this)) {
		client->detach(this);
		client->release();
		return kIOReturnError;
	}

	*handler = client;
	*(volatile uint32_t *)USERSPACE_NEWCLIENT_RESULT_MARKER_ADDR = 0x600D;
	return kIOReturnSuccess;
}
