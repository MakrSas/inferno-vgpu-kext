// Exports InfernoGetAGXDevice(), a plain C function returning a real,
// working id<MTLDevice> (an Apple AGXPrincipalDevice instance, constructed
// with a connection to our own InfernoVGPUHello kext) or nil on failure.
// Meant to be dlopen()'d + dlsym()'d from a small patch inside Metal's own
// ___MTLCreateSystemDefaultDevice_block_invoke (see project memory) --
// keeping all the real logic here, in normally-compiled/iterated code,
// means the actual hand-assembled patch only needs dlopen+dlsym+call+store,
// not the whole IOKit/CFDictionary/ObjC call sequence in raw machine code.
#import <Foundation/Foundation.h>
#import <IOKit/IOKitLib.h>
#import <Metal/Metal.h>
#import <dlfcn.h>
#import <objc/runtime.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>

// Diagnostic-only: Q() can be called from a very fragile context (the raw
// machine-code patch inside Metal.framework's own
// ___MTLCreateSystemDefaultDevice_block_invoke -- see patch_block_invoke.py)
// where a crash produces zero stdout and the caller just observes "Killed:
// 9" with no other information. Appends one line per step to a plain file
// using raw POSIX open/write/close (no buffered stdio, no ObjC) so that
// whatever step was reached survives even if the process is killed
// immediately after this call returns. Intentionally NOT gated behind any
// build flag -- cheap enough to always run, and it's exactly the kind of
// blind spot that has bitten this project before (see project memory: the
// kernel-side SCRATCH_ADDR/USERSPACE_*_MARKER_ADDR markers exist for the
// identical reason).
static void QTrace(const char *msg)
{
    int fd = open("/tmp/q_trace.log", O_WRONLY | O_CREAT | O_APPEND, 0666);
    if (fd < 0) {
        return;
    }
    write(fd, msg, strlen(msg));
    write(fd, "\n", 1);
    close(fd);
}

// Our -initWithAcceleratorPort: patch (see project memory) is `return self`,
// skipping whatever real setup Apple's internal build had disabled -- so
// some MTLDevice protocol methods the real init would have wired up (e.g.
// -name) come back "unrecognized selector" on the constructed instance.
// Patch in safe, minimal fallbacks for the ones callers are likely to hit
// immediately, but only if the class doesn't already implement them --
// this must stay additive, never override a real Apple implementation.
static NSString *InfernoAGXName(id self, SEL _cmd)
{
    (void)self; (void)_cmd;
    return @"Inferno AGX";
}

static NSString *InfernoAGXVendorName(id self, SEL _cmd)
{
    (void)self; (void)_cmd;
    return @"Inferno";
}

static unsigned long long InfernoAGXRegistryID(id self, SEL _cmd)
{
    (void)self; (void)_cmd;
    return 0x494e464552ULL;  // "INFER" -- placeholder, no real IORegistry entry ID wired up yet.
}

static _Bool InfernoAGXYes(id self, SEL _cmd) { (void)self; (void)_cmd; return 1; }
static _Bool InfernoAGXNo(id self, SEL _cmd) { (void)self; (void)_cmd; return 0; }

static unsigned long long InfernoAGXMaxBufferLength(id self, SEL _cmd)
{
    (void)self; (void)_cmd;
    return 256ULL * 1024 * 1024;
}

static unsigned long long InfernoAGXRecommendedWorkingSet(id self, SEL _cmd)
{
    (void)self; (void)_cmd;
    return 512ULL * 1024 * 1024;
}

static unsigned long long InfernoAGXZero64(id self, SEL _cmd) { (void)self; (void)_cmd; return 0; }

// No real GPU-family/feature-set support exists yet (Inferno's AGX register
// emulation isn't functional -- see project memory) -- honestly reporting
// NO for every family is the safe answer; claiming support we can't back
// would just move the crash further downstream.
static _Bool InfernoAGXSupportsFamily(id self, SEL _cmd, long long family)
{
    (void)self; (void)_cmd; (void)family;
    return 0;
}

// Defined in inferno_command_queue.m -- give -newCommandQueue and
// -newBufferWithLength:options: real, non-crashing implementations backed
// by our own inferno-vgpu IOKit connection / host memory, instead of
// AGXPrincipalDevice's (nonfunctional, given Inferno's current AGX register
// emulation) real hardware path.
extern void InfernoAssociateVGPUConnection(id device, io_connect_t conn);
extern void InfernoInstallCommandQueueFallback(id device);
extern void InfernoInstallBufferFallback(id device);
extern void InfernoInstallComputeFallback(id device);
// Defined in inferno_render_encoder.m -- real -newTextureWithDescriptor:/
// -newRenderPipelineStateWithDescriptor:error: (the render-side siblings of
// InfernoInstallComputeFallback/InfernoInstallBufferFallback).
extern void InfernoInstallTextureFallback(id device);
extern void InfernoInstallRenderPipelineFallback(id device);

static void InfernoAddIfMissing(id device, SEL sel, IMP imp, const char *types)
{
    if (![device respondsToSelector:sel]) {
        class_addMethod(object_getClass(device), sel, imp, types);
    }
}

static void InfernoPatchMissingDeviceMethods(id device, io_connect_t conn)
{
    InfernoAddIfMissing(device, @selector(name), (IMP)InfernoAGXName, "@@:");
    InfernoAddIfMissing(device, @selector(vendorName), (IMP)InfernoAGXVendorName, "@@:");
    InfernoAddIfMissing(device, @selector(registryID), (IMP)InfernoAGXRegistryID, "Q@:");
    InfernoAddIfMissing(device, @selector(hasUnifiedMemory), (IMP)InfernoAGXYes, "B@:");
    InfernoAddIfMissing(device, @selector(isLowPower), (IMP)InfernoAGXNo, "B@:");
    InfernoAddIfMissing(device, @selector(isHeadless), (IMP)InfernoAGXNo, "B@:");
    InfernoAddIfMissing(device, @selector(isRemovable), (IMP)InfernoAGXNo, "B@:");
    InfernoAddIfMissing(device, @selector(maxBufferLength), (IMP)InfernoAGXMaxBufferLength, "Q@:");
    InfernoAddIfMissing(device, @selector(recommendedMaxWorkingSetSize), (IMP)InfernoAGXRecommendedWorkingSet, "Q@:");
    InfernoAddIfMissing(device, @selector(currentAllocatedSize), (IMP)InfernoAGXZero64, "Q@:");
    InfernoAddIfMissing(device, @selector(supportsFamily:), (IMP)InfernoAGXSupportsFamily, "B@:q");

    QTrace("Q: property fallbacks added");
    InfernoAssociateVGPUConnection(device, conn);
    QTrace("Q: VGPU connection associated");
    InfernoInstallCommandQueueFallback(device);
    QTrace("Q: command queue fallback installed");
    InfernoInstallBufferFallback(device);
    QTrace("Q: buffer fallback installed");
    InfernoInstallComputeFallback(device);
    QTrace("Q: compute fallback installed");
    InfernoInstallTextureFallback(device);
    QTrace("Q: texture fallback installed");
    InfernoInstallRenderPipelineFallback(device);
    QTrace("Q: render pipeline fallback installed");
}

// Exported as "Q": the raw machine-code patch in
// ___MTLCreateSystemDefaultDevice_block_invoke has ~21 free instructions
// (see patch_block_invoke.py) and dlsym's symbol-name string is built
// on-stack via a single MOVZ immediate, which only fits a 1-2 char name.
void *Q(void)
{
    QTrace("Q: enter");
    @autoreleasepool {
        QTrace("Q: autoreleasepool entered");
        CFMutableDictionaryRef matching = IOServiceMatching(kIOServiceClass);
        if (matching == NULL) {
            QTrace("Q: IOServiceMatching failed");
            return NULL;
        }
        CFMutableDictionaryRef props = CFDictionaryCreateMutable(
            kCFAllocatorDefault, 0, &kCFTypeDictionaryKeyCallBacks,
            &kCFTypeDictionaryValueCallBacks);
        CFDictionarySetValue(props, CFSTR("MetalPluginClassName"),
                              CFSTR("InfernoVGPUMetalDevice"));
        CFDictionarySetValue(matching, CFSTR(kIOPropertyMatchKey), props);
        CFRelease(props);
        QTrace("Q: matching dict built");

        io_iterator_t iter = IO_OBJECT_NULL;
        if (IOServiceGetMatchingServices(MACH_PORT_NULL, matching, &iter) != KERN_SUCCESS) {
            QTrace("Q: IOServiceGetMatchingServices failed");
            return NULL;
        }
        io_service_t service = IOIteratorNext(iter);
        IOObjectRelease(iter);
        if (service == IO_OBJECT_NULL) {
            QTrace("Q: no matching service");
            return NULL;
        }
        QTrace("Q: got service");

        io_connect_t conn = IO_OBJECT_NULL;
        kern_return_t kr = IOServiceOpen(service, mach_task_self(), 0, &conn);
        IOObjectRelease(service);
        if (kr != KERN_SUCCESS) {
            QTrace("Q: IOServiceOpen failed");
            return NULL;
        }
        QTrace("Q: IOServiceOpen ok");

        void *handle = dlopen("/System/Library/Extensions/AGXMetalA13.bundle/AGXMetalA13", RTLD_NOW);
        if (handle == NULL) {
            QTrace("Q: dlopen(AGXMetalA13) failed");
            return NULL;
        }
        QTrace("Q: dlopen(AGXMetalA13) ok");

        Class agxClass = NSClassFromString(@"AGXPrincipalDevice");
        if (agxClass == Nil) {
            QTrace("Q: AGXPrincipalDevice class not found");
            return NULL;
        }
        QTrace("Q: got AGXPrincipalDevice class");
        id device = [agxClass alloc];
        if (device == nil) {
            QTrace("Q: alloc failed");
            return NULL;
        }
        QTrace("Q: alloc ok");
        SEL sel = NSSelectorFromString(@"initWithAcceleratorPort:");
        if (![device respondsToSelector:sel]) {
            QTrace("Q: does not respond to initWithAcceleratorPort:");
            return NULL;
        }
        id (*func)(id, SEL, io_connect_t) =
            (id (*)(id, SEL, io_connect_t))[device methodForSelector:sel];
        QTrace("Q: calling initWithAcceleratorPort:");
        id inited = func(device, sel, conn);
        if (inited == nil) {
            QTrace("Q: initWithAcceleratorPort: returned nil");
            return NULL;
        }
        QTrace("Q: initWithAcceleratorPort: ok, patching methods");
        InfernoPatchMissingDeviceMethods(inited, conn);
        QTrace("Q: InfernoPatchMissingDeviceMethods done, returning");

        // Leak deliberately (CFBridgingRetain / +1 retain): the raw machine
        // code patch calling this has no ARC and just wants a plain,
        // caller-owned pointer it can store straight into Metal's own
        // device-cache field.
        return (__bridge_retained void *)inited;
    }
}
