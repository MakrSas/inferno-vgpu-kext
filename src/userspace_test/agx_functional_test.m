// End-to-end functional test of the fallback MTLDevice surface installed by
// inferno_agx_bridge.m's Q(): not just respondsToSelector checks (that's
// agx_introspect's job) but actually calling each method and checking the
// result makes sense, including a real commandQueue -> commandBuffer ->
// commit round trip and a real buffer write/read-back.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <dlfcn.h>

static int gFailures = 0;

#define CHECK(cond, fmt, ...) do { \
    if (cond) { \
        printf("OK   " fmt "\n", ##__VA_ARGS__); \
    } else { \
        printf("FAIL " fmt "\n", ##__VA_ARGS__); \
        gFailures++; \
    } \
} while (0)

int main(void)
{
    @autoreleasepool {
        void *handle = dlopen("/b", RTLD_NOW);
        if (handle == NULL) {
            printf("dlopen failed: %s\n", dlerror());
            return 1;
        }
        void *(*fn)(void) = (void *(*)(void))dlsym(handle, "Q");
        if (fn == NULL) {
            printf("dlsym failed: %s\n", dlerror());
            return 1;
        }
        void *raw = fn();
        CHECK(raw != NULL, "Q() returned non-nil");
        if (raw == NULL) {
            return 1;
        }
        id<MTLDevice> device = (__bridge id<MTLDevice>)raw;

        NSString *name = device.name;
        CHECK(name != nil && name.length > 0, "device.name = %s", name.UTF8String ?: "(nil)");

        NSString *vendor = device.vendorName;
        CHECK(vendor != nil, "device.vendorName = %s", vendor.UTF8String ?: "(nil)");

        unsigned long long regID = device.registryID;
        CHECK(regID != 0, "device.registryID = 0x%llx", regID);

        CHECK(device.hasUnifiedMemory == YES, "device.hasUnifiedMemory = %d", device.hasUnifiedMemory);
        CHECK(device.maxBufferLength > 0, "device.maxBufferLength = %llu", device.maxBufferLength);

        id<MTLCommandQueue> queue = [device newCommandQueue];
        CHECK(queue != nil, "newCommandQueue -> %p", queue);

        if (queue != nil) {
            id<MTLCommandBuffer> cmdbuf = [queue commandBuffer];
            CHECK(cmdbuf != nil, "commandBuffer -> %p", cmdbuf);
            if (cmdbuf != nil) {
                [cmdbuf commit];
                CHECK(cmdbuf.status == MTLCommandBufferStatusCompleted,
                      "commit -> status=%ld (expect Completed=%ld)",
                      (long)cmdbuf.status, (long)MTLCommandBufferStatusCompleted);
            }
        }

        id<MTLBuffer> buf = [device newBufferWithLength:1024 options:0];
        CHECK(buf != nil, "newBufferWithLength:1024 -> %p", buf);
        if (buf != nil) {
            CHECK(buf.length == 1024, "buf.length = %lu", (unsigned long)buf.length);
            void *contents = buf.contents;
            CHECK(contents != NULL, "buf.contents = %p", contents);
            if (contents != NULL) {
                memset(contents, 0xAB, 1024);
                unsigned char *bytes = (unsigned char *)contents;
                int ok = (bytes[0] == 0xAB && bytes[1023] == 0xAB);
                CHECK(ok, "buf.contents round-trip write/read (byte[0]=%02x byte[1023]=%02x)",
                      bytes[0], bytes[1023]);
            }
        }

        printf("\n%d failure(s)\n", gFailures);
    }
    return gFailures == 0 ? 0 : 1;
}
