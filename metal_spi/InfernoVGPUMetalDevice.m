#import <Foundation/Foundation.h>
#import <IOKit/IOKitLib.h>
#import "InfernoVGPUMetalDevice.h"

// Hand-written half of InfernoVGPUMetalDevice: the "real" overrides
// (REAL_OVERRIDES in gen_stub.py) plus init/dealloc and the +registerDevices
// entry point Metal's plugin loader calls (see MetalDeviceSPI.txt line
// "+ (void)registerDevices;"). The rest of MTLDevice/MTLDeviceSPI is in the
// auto-generated InfernoVGPUMetalDevice_generated.m.

@implementation InfernoVGPUMetalDevice

+ (void)registerDevices
{
    // Called by Metal's generic plugin loader once it has instantiated a
    // class matching this bundle's MetalPluginClassName and confirmed it
    // conforms to MTLDeviceSPI. Real device registration (letting Metal
    // discover *our* instance rather than one it constructs itself) needs
    // whatever internal registration call the real GPU-family plugins use
    // here -- not yet identified. Left as a no-op for now: the loader path
    // itself (does Metal even get this far) is the thing being tested next.
}

- (instancetype)init
{
    self = [super init];
    if (self == nil) {
        return nil;
    }

    _vgpuConnection = IO_OBJECT_NULL;

    CFMutableDictionaryRef matching = IOServiceMatching(kIOServiceClass);
    if (matching != NULL) {
        CFMutableDictionaryRef props = CFDictionaryCreateMutable(
            kCFAllocatorDefault, 0, &kCFTypeDictionaryKeyCallBacks,
            &kCFTypeDictionaryValueCallBacks);
        CFDictionarySetValue(props, CFSTR("MetalPluginClassName"),
                              CFSTR("InfernoVGPUMetalDevice"));
        CFDictionarySetValue(matching, CFSTR(kIOPropertyMatchKey), props);
        CFRelease(props);

        io_iterator_t iter = IO_OBJECT_NULL;
        if (IOServiceGetMatchingServices(MACH_PORT_NULL, matching, &iter) == KERN_SUCCESS) {
            io_service_t service = IOIteratorNext(iter);
            IOObjectRelease(iter);
            if (service != IO_OBJECT_NULL) {
                io_connect_t conn = IO_OBJECT_NULL;
                if (IOServiceOpen(service, mach_task_self(), 0, &conn) == KERN_SUCCESS) {
                    _vgpuConnection = conn;
                }
                IOObjectRelease(service);
            }
        }
    }

    return self;
}

- (void)dealloc
{
    if (_vgpuConnection != IO_OBJECT_NULL) {
        IOServiceClose(_vgpuConnection);
    }
}

- (NSString *)name
{
    return @"Inferno VGPU";
}

- (unsigned long long)registryID
{
    return 0x494e464552;  // "INFER" -- placeholder, real registryID should
                            // come from the IOService's own registry entry.
}

- (NSString *)vendorName
{
    return @"Inferno";
}

- (_Bool)hasUnifiedMemory
{
    return YES;  // QEMU virtual GPU shares host RAM -- genuinely true here,
                  // unlike a real discrete/family GPU.
}

- (_Bool)isLowPower
{
    return NO;
}

- (_Bool)isHeadless
{
    return NO;
}

- (_Bool)isRemovable
{
    return NO;
}

- (unsigned long long)maxBufferLength
{
    return 256ULL * 1024 * 1024;  // 256MB -- matches this project's COMMON_BASE
                                    // scratch carve order of magnitude; revisit
                                    // once real buffer allocation exists.
}

- (unsigned long long)recommendedMaxWorkingSetSize
{
    return 512ULL * 1024 * 1024;
}

- (unsigned long long)currentAllocatedSize
{
    return 0;
}

@end
