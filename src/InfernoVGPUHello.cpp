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
	static volatile uint8_t *ringBase(void) { return (volatile uint8_t *)FIFO_TEST_RING_ADDR; }

private:
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

	registerService();

	return true;
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

uint32_t InfernoVGPUHello::submitComputeDispatch(const uint8_t *payload, uint32_t payloadLen)
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

	packet[0] = INFERNO_VGPU_OP_COMPUTE_DISPATCH & 0xff;
	packet[1] = (INFERNO_VGPU_OP_COMPUTE_DISPATCH >> 8) & 0xff;
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

private:
	InfernoVGPUHello *fProvider;
};

OSDefineMetaClassAndStructors(InfernoVGPUUserClient, IOUserClient)

enum {
	kInfernoVGPUMethodGetVersion = 0,
	kInfernoVGPUMethodComputeDispatch = 1,
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
