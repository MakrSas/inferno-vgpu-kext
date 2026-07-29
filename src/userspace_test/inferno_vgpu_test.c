// Minimal userspace test: open InfernoVGPUHello via IOServiceOpen and call
// its GetVersion external method. Proves IOServiceOpen -> newUserClient()
// (kernel side, already boot-tested) actually works end to end from real
// userspace, independent of everything else. Deliberately tiny -- no
// framework beyond IOKit, so it's easy to reason about if something fails.
//
// Build (userspace arm64e-ios context, NOT -mkernel/-fapple-kext, see the
// project's iokit-class-probe workflow for the kernel-context build --
// this is the opposite: a normal, real userspace Mach-O binary):
//   clang -target arm64e-apple-ios14.0 -isysroot <iPhoneOS SDK> \
//     -framework IOKit -framework CoreFoundation \
//     -o inferno_vgpu_test inferno_vgpu_test.c

#include <stdio.h>
#include <stdint.h>
#include <IOKit/IOKitLib.h>

// Must match InfernoVGPUUserClient's kInfernoVGPUMethodGetVersion (=0) in
// src/InfernoVGPUHello.cpp.
enum {
    kInfernoVGPUMethodGetVersion = 0,
};

int main(void)
{
    kern_return_t kr;
    io_iterator_t iter;
    io_service_t service = IO_OBJECT_NULL;
    io_connect_t conn = IO_OBJECT_NULL;

    // MetalPluginClassName's value ("InfernoVGPUMetalDevice") is a property,
    // not the IOClass -- match by the real class name instead.
    CFMutableDictionaryRef matching = IOServiceMatching("InfernoVGPUHello");
    if (matching == NULL) {
        fprintf(stderr, "IOServiceMatching failed\n");
        return 1;
    }

    kr = IOServiceGetMatchingServices(kIOMasterPortDefault, matching, &iter);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "IOServiceGetMatchingServices failed: 0x%x\n", kr);
        return 1;
    }

    service = IOIteratorNext(iter);
    IOObjectRelease(iter);
    if (service == IO_OBJECT_NULL) {
        fprintf(stderr, "InfernoVGPUHello not found in IORegistry\n");
        return 1;
    }
    printf("found InfernoVGPUHello service\n");

    kr = IOServiceOpen(service, mach_task_self(), 0, &conn);
    IOObjectRelease(service);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "IOServiceOpen failed: 0x%x\n", kr);
        return 1;
    }
    printf("IOServiceOpen succeeded, connection=0x%x\n", conn);

    uint64_t version = 0;
    uint32_t outputCount = 1;
    kr = IOConnectCallScalarMethod(conn, kInfernoVGPUMethodGetVersion,
                                    NULL, 0, &version, &outputCount);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "GetVersion failed: 0x%x\n", kr);
        IOServiceClose(conn);
        return 1;
    }

    printf("GetVersion -> 0x%llx (expect 0x10000)\n", (unsigned long long)version);

    IOServiceClose(conn);
    return (version == 0x10000) ? 0 : 2;
}
