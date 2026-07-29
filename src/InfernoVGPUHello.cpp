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
// booting XNU kernel. See inferno-vgpu-kext/resolve.py and
// InfernoData/patch_kernelcache.py in the Inferno repo for the injection
// pipeline; the vtable-slot layout produced by this exact SDK header does NOT
// match the real internal one used to build the target kernelcache (confirmed
// empirically, not a guess), so resolve.py corrects known-mismatched slots
// post-compile rather than trusting the compiled layout verbatim. The same
// pipeline also replaces every `-fapple-kext`-miscompiled indirect call to an
// inherited, un-overridden virtual method (both explicit super-calls and
// plain `this->method()` calls) with a direct `bl` to the method's real
// exported symbol -- see resolve.py's SUPERCALL_FIXUPS/PLAIN_CALL_FIXUPS.
//
// start() publishes this service under the IOAcceleratorES match category
// with MetalPluginName/MetalPluginClassName properties set, matching what
// `+[MTLIOAccelDevice registerDevices]` looks for (confirmed by
// dyld_shared_cache analysis earlier in this project). It then maps the
// inferno-vgpu-v1 QEMU device's MMIO region directly by physical address
// (IOMemoryDescriptor::withPhysicalAddress -- not through provider matching,
// since whether IOKit's platform-expert promotes the device's synthetic DT
// node to a matchable IOService is a still-open question this deliberately
// sidesteps) and reads its VERSION register as a first real hardware-facing
// smoke test, storing the raw value for external verification.

#include <IOKit/IOService.h>
#include <IOKit/IOMemoryDescriptor.h>

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
// (0x40/0x100/0x108/0x110), so nothing collides.
#define SCRATCH_ADDR 0xfffffff1020c4200ULL

class InfernoVGPUHello : public IOService
{
	OSDeclareDefaultStructors(InfernoVGPUHello)

public:
	virtual bool start(IOService *provider) override;
};

OSDefineMetaClassAndStructors(InfernoVGPUHello, IOService)

bool InfernoVGPUHello::start(IOService *provider)
{
	if (!IOService::start(provider)) {
		return false;
	}

	setProperty("IOMatchCategory", "IOAcceleratorES");
	setProperty("MetalPluginName", "InfernoVGPUMetal");
	setProperty("MetalPluginClassName", "InfernoVGPUMetalDevice");

	IOMemoryDescriptor *desc = IOMemoryDescriptor::withPhysicalAddress(
		INFERNO_VGPU_PHYS_BASE, INFERNO_VGPU_MMIO_SIZE, kIODirectionInOut);
	if (desc != NULL) {
		IOMemoryMap *map = desc->map();
		if (map != NULL) {
			volatile uint32_t *regs = (volatile uint32_t *)map->getVirtualAddress();
			*(volatile uint32_t *)SCRATCH_ADDR = regs[INFERNO_VGPU_REG_VERSION / 4];
		}
	}

	registerService();

	return true;
}
