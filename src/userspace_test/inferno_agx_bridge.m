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

void *InfernoGetAGXDevice(void)
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

        // Leak deliberately (CFBridgingRetain / +1 retain): the raw machine
        // code patch calling this has no ARC and just wants a plain,
        // caller-owned pointer it can store straight into Metal's own
        // device-cache field.
        return (__bridge_retained void *)inited;
    }
}
