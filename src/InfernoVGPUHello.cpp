// Step 3 of the injection probe: an actual IOKit C++ driver class, compiled
// against macOS SDK's Kernel.framework headers (Xcode 15.4 ships them under
// System/Library/Frameworks/Kernel.framework/Versions/A/Headers/IOKit/ --
// confirmed present via CI probe, no Apple KDK needed) but targeting the
// arm64e-apple-ios kernel-context triple for actual codegen. XNU's IOKit ABI
// is shared between macOS and iOS of the same kernel generation, so this is
// the standard trick used across the jailbreak-development community for
// building iOS kexts without an iOS-specific kext SDK (which Apple never
// shipped -- iOS never officially supported loadable kexts).
//
// This does NOT yet match against anything or do anything GPU-related. The
// only question this step answers: does a real OSDeclareDefaultStructors /
// OSDefineMetaClassAndStructors class -- the actual mechanism real IOKit
// drivers use, as opposed to the two dead-end shortcuts already ruled out
// (kmod_info.start, raw __kmod_init entry replacement) -- get its static
// constructor invoked and its start() called, when injected the same way.

#include <IOKit/IOService.h>

class InfernoVGPUHello : public IOService
{
	OSDeclareDefaultStructors(InfernoVGPUHello)

public:
	virtual bool start(IOService *provider) override;
};

OSDefineMetaClassAndStructors(InfernoVGPUHello, IOService)

// Same marker-write trick as kmod_hello.c: an absolute hardcoded address
// inside our own reserved kext slot, so this compiles to self-contained,
// relocation-free code we can copy as raw bytes. Address chosen once we
// know where in the slot this build's __TEXT lands (see workflow output).
#define MARKER_ADDR 0xfffffff009420000ULL

bool InfernoVGPUHello::start(IOService *provider)
{
	*(volatile unsigned int *)MARKER_ADDR = 0xCA11AB1Eu;
	return true;
}
