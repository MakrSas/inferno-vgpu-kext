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
// post-compile rather than trusting the compiled layout verbatim.
//
// start() now does real, minimal IOKit work: publish this service under the
// IOAcceleratorES match category with MetalPluginName/MetalPluginClassName
// properties set, matching what `+[MTLIOAccelDevice registerDevices]` looks
// for via IOServiceGetMatchingServices + IORegistryEntryCreateCFProperty
// (confirmed by dyld_shared_cache string/disassembly analysis earlier in this
// project -- the loader is structurally generic, not name-whitelisted). The
// plugin bundle these properties name does not exist yet; that's the next
// piece of work, not this one.

#include <IOKit/IOService.h>

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

	registerService();

	return true;
}
