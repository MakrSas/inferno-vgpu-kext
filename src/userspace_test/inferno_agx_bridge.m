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

// Our -initWithAcceleratorPort: patch (see project memory) is `return self`,
// skipping whatever real setup Apple's internal build had disabled -- so
// some MTLDevice protocol methods the real init would have wired up (e.g.
// -name) come back "unrecognized selector" on the constructed instance.
// Patch in safe, minimal fallbacks for the ones callers are likely to hit
// immediately, but only if the class doesn't already implement them --
// this must stay additive, never override a real Apple implementation.
static NSString *InfernoAGXName(id self, SEL _cmd)
{
    (void)self;
    (void)_cmd;
    return @"Inferno AGX";
}

// Defined in inferno_command_queue.m -- gives -newCommandQueue a real,
// non-crashing implementation backed by our own inferno-vgpu IOKit
// connection instead of AGXPrincipalDevice's (nonfunctional, given
// Inferno's current AGX register emulation) real hardware path.
extern void InfernoAssociateVGPUConnection(id device, io_connect_t conn);
extern void InfernoInstallCommandQueueFallback(id device);

static void InfernoPatchMissingDeviceMethods(id device, io_connect_t conn)
{
    Class cls = object_getClass(device);
    if (![device respondsToSelector:@selector(name)]) {
        class_addMethod(cls, @selector(name), (IMP)InfernoAGXName, "@@:");
    }
    InfernoAssociateVGPUConnection(device, conn);
    InfernoInstallCommandQueueFallback(device);
}

// Exported as "Q": the raw machine-code patch in
// ___MTLCreateSystemDefaultDevice_block_invoke has ~21 free instructions
// (see patch_block_invoke.py) and dlsym's symbol-name string is built
// on-stack via a single MOVZ immediate, which only fits a 1-2 char name.
void *Q(void)
{
    @autoreleasepool {
        CFMutableDictionaryRef matching = IOServiceMatching(kIOServiceClass);
        if (matching == NULL) {
            return NULL;
        }
        CFMutableDictionaryRef props = CFDictionaryCreateMutable(
            kCFAllocatorDefault, 0, &kCFTypeDictionaryKeyCallBacks,
            &kCFTypeDictionaryValueCallBacks);
        CFDictionarySetValue(props, CFSTR("MetalPluginClassName"),
                              CFSTR("InfernoVGPUMetalDevice"));
        CFDictionarySetValue(matching, CFSTR(kIOPropertyMatchKey), props);
        CFRelease(props);

        io_iterator_t iter = IO_OBJECT_NULL;
        if (IOServiceGetMatchingServices(MACH_PORT_NULL, matching, &iter) != KERN_SUCCESS) {
            return NULL;
        }
        io_service_t service = IOIteratorNext(iter);
        IOObjectRelease(iter);
        if (service == IO_OBJECT_NULL) {
            return NULL;
        }

        io_connect_t conn = IO_OBJECT_NULL;
        kern_return_t kr = IOServiceOpen(service, mach_task_self(), 0, &conn);
        IOObjectRelease(service);
        if (kr != KERN_SUCCESS) {
            return NULL;
        }

        void *handle = dlopen("/System/Library/Extensions/AGXMetalA13.bundle/AGXMetalA13", RTLD_NOW);
        if (handle == NULL) {
            return NULL;
        }

        Class agxClass = NSClassFromString(@"AGXPrincipalDevice");
        if (agxClass == Nil) {
            return NULL;
        }
        id device = [agxClass alloc];
        if (device == nil) {
            return NULL;
        }
        SEL sel = NSSelectorFromString(@"initWithAcceleratorPort:");
        if (![device respondsToSelector:sel]) {
            return NULL;
        }
        id (*func)(id, SEL, io_connect_t) =
            (id (*)(id, SEL, io_connect_t))[device methodForSelector:sel];
        id inited = func(device, sel, conn);
        if (inited == nil) {
            return NULL;
        }
        InfernoPatchMissingDeviceMethods(inited, conn);

        // Leak deliberately (CFBridgingRetain / +1 retain): the raw machine
        // code patch calling this has no ARC and just wants a plain,
        // caller-owned pointer it can store straight into Metal's own
        // device-cache field.
        return (__bridge_retained void *)inited;
    }
}
