// Tests -[AGXPrincipalDevice initWithAcceleratorPort:] directly, using our
// own already-proven-working IOServiceOpen() connection to InfernoVGPUHello,
// bypassing whatever Metal.framework's own device-discovery logic does
// (which apparently never reaches this initializer at all -- see project
// memory). Isolates: does the patched initializer (a 4-byte live patch in
// the guest's own dyld_shared_cache_arm64e, turning an unconditional
// log-and-return-nil stub into an immediate `return self`) actually work
// when given a real port, independent of discovery?
#import <Foundation/Foundation.h>
#import <IOKit/IOKitLib.h>
#import <Metal/Metal.h>

int main(void)
{
    @autoreleasepool {
        CFMutableDictionaryRef matching = IOServiceMatching(kIOServiceClass);
        CFMutableDictionaryRef props = CFDictionaryCreateMutable(
            kCFAllocatorDefault, 0, &kCFTypeDictionaryKeyCallBacks,
            &kCFTypeDictionaryValueCallBacks);
        CFDictionarySetValue(props, CFSTR("MetalPluginClassName"),
                              CFSTR("InfernoVGPUMetalDevice"));
        CFDictionarySetValue(matching, CFSTR(kIOPropertyMatchKey), props);
        CFRelease(props);

        io_iterator_t iter = IO_OBJECT_NULL;
        if (IOServiceGetMatchingServices(MACH_PORT_NULL, matching, &iter) != KERN_SUCCESS) {
            printf("IOServiceGetMatchingServices failed\n");
            return 1;
        }
        io_service_t service = IOIteratorNext(iter);
        IOObjectRelease(iter);
        if (service == IO_OBJECT_NULL) {
            printf("service not found\n");
            return 1;
        }

        io_connect_t conn = IO_OBJECT_NULL;
        kern_return_t kr = IOServiceOpen(service, mach_task_self(), 0, &conn);
        IOObjectRelease(service);
        if (kr != KERN_SUCCESS) {
            printf("IOServiceOpen failed: 0x%x\n", kr);
            return 1;
        }
        printf("IOServiceOpen succeeded, connection=0x%x\n", conn);

        Class agxClass = NSClassFromString(@"AGXPrincipalDevice");
        printf("AGXPrincipalDevice class -> %p\n", (__bridge void *)agxClass);
        if (agxClass == Nil) {
            printf("class not found\n");
            return 1;
        }

        id device = [agxClass alloc];
        printf("alloc -> %p\n", (__bridge void *)device);
        if (device == nil) {
            return 1;
        }

        SEL sel = NSSelectorFromString(@"initWithAcceleratorPort:");
        if (![device respondsToSelector:sel]) {
            printf("does not respond to initWithAcceleratorPort:\n");
            return 1;
        }
        id (*func)(id, SEL, io_connect_t) =
            (id (*)(id, SEL, io_connect_t))[device methodForSelector:sel];
        id inited = func(device, sel, conn);
        printf("initWithAcceleratorPort: -> %p\n", (__bridge void *)inited);

        if (inited != nil) {
            printf("conformsToProtocol(MTLDevice) -> %d\n",
                   [inited conformsToProtocol:@protocol(MTLDevice)]);
        }
    }
    return 0;
}
