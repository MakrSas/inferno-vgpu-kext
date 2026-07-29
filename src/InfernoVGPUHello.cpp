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

private:
	InfernoVGPUHello *fProvider;
};

OSDefineMetaClassAndStructors(InfernoVGPUUserClient, IOUserClient)

enum {
	kInfernoVGPUMethodGetVersion = 0,
	kInfernoVGPUMethodCount
};

static const IOExternalMethodDispatch sInfernoVGPUMethods[kInfernoVGPUMethodCount] = {
	{ (IOExternalMethodAction)&InfernoVGPUUserClient::sGetVersion, 0, 0, 0, 1 },
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
	if (!IOUserClient::start(provider)) {
		return false;
	}
	fProvider = OSDynamicCast(InfernoVGPUHello, provider);
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
	return kIOReturnSuccess;
}
