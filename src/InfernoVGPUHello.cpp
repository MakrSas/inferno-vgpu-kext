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

// inferno-vgpu-v1's MMIO base, as mapped by t8030_create_inferno_vgpu_node()
// in this exact QEMU build/machine config (kaslr-off=true, fixed device
// tree) -- confirmed empirically via QMP `info mtree` showing
// `inferno-vgpu-v1.mmio` at this exact physical range.
#define INFERNO_VGPU_PHYS_BASE 0x23d100000ULL
#define INFERNO_VGPU_MMIO_SIZE 0x4000
#define INFERNO_VGPU_REG_VERSION 0x1034

// A scratch write target inside the same writable 4KB carve InfernoVGPUHello
// already uses for gMetaClass (see resolve.py's COMMON_BASE) -- distinct
// offset from gMetaClass (0xe20) and the earlier one-off diagnostic probes
// (0x40/0x100/0x108/0x110/0x200), so nothing collides.
#define SCRATCH_ADDR 0xfffffff1020c4300ULL

class InfernoVGPUHello : public IOService
{
	OSDeclareDefaultStructors(InfernoVGPUHello)

public:
	virtual bool start(IOService *provider) override;
	virtual IOReturn newUserClient(task_t owningTask, void *securityID,
	                                UInt32 type, OSDictionary *properties,
	                                IOUserClient **handler) override;

	uint32_t readVersionRegister(void);

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
