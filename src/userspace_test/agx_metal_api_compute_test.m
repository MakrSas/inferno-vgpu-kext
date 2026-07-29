// Exercises the REAL, standard Metal compute API surface end to end --
// -newLibraryWithData:, -newFunctionWithName:, -newComputePipelineState...,
// -computeCommandEncoder, -setComputePipelineState:/-setBuffer:.../
// -dispatchThreadgroups:.../-endEncoding, -commit -- instead of calling
// IOConnectCallStructMethod directly like inferno_compute_dispatch_test.c
// does. Proves the whole ObjC fallback layer (inferno_command_queue.m) uses
// the compute-dispatch opcode correctly through a real app's normal calling
// pattern, not just through our own low-level test harness.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <dlfcn.h>
#import <dispatch/dispatch.h>

static const char kAirText[] =
    "; Owned synthetic fixture for public drift / A/B samples.\n"
    "; Not derived from a third-party metallib.\n"
    "source_filename = \"kernel_store_const.metal\"\n"
    "\n"
    "define void @store_const(ptr addrspace(1) %out) {\n"
    "  store i32 42, ptr addrspace(1) %out, align 4\n"
    "  ret void\n"
    "}\n"
    "\n"
    "!air.kernel = !{!0}\n"
    "!0 = !{ptr @store_const, !1, !2}\n"
    "!1 = !{}\n"
    "!2 = !{!3}\n"
    "!3 = !{i32 0, !\"air.buffer\", !\"air.buffer_size\", i32 4, "
    "!\"air.location_index\", i32 0, i32 1, !\"air.read_write\", "
    "!\"air.address_space\", i32 1, !\"air.arg_type_name\", !\"int\", "
    "!\"air.arg_name\", !\"out\"}\n";

#define CHECK(cond, fmt, ...) do { \
    if (cond) { printf("OK   " fmt "\n", ##__VA_ARGS__); } \
    else { printf("FAIL " fmt "\n", ##__VA_ARGS__); ok = 0; } \
} while (0)

int main(void)
{
    int ok = 1;
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

        dispatch_data_t airData = dispatch_data_create(
            kAirText, sizeof(kAirText) - 1, dispatch_get_main_queue(), DISPATCH_DATA_DESTRUCTOR_DEFAULT);

        NSError *error = nil;
        id<MTLLibrary> library = [device newLibraryWithData:airData error:&error];
        CHECK(library != nil, "newLibraryWithData: -> %p", (void *)library);

        id<MTLFunction> function = [library newFunctionWithName:@"store_const"];
        CHECK(function != nil, "newFunctionWithName: -> %p", (void *)function);

        id<MTLComputePipelineState> pipeline =
            [device newComputePipelineStateWithFunction:function error:&error];
        CHECK(pipeline != nil, "newComputePipelineStateWithFunction: -> %p", (void *)pipeline);

        id<MTLBuffer> buffer = [device newBufferWithLength:4 options:0];
        CHECK(buffer != nil, "newBufferWithLength: -> %p", (void *)buffer);
        memset(buffer.contents, 0, 4);

        id<MTLCommandQueue> queue = [device newCommandQueue];
        CHECK(queue != nil, "newCommandQueue -> %p", (void *)queue);

        id<MTLCommandBuffer> cmdBuf = [queue commandBuffer];
        CHECK(cmdBuf != nil, "commandBuffer -> %p", (void *)cmdBuf);

        id<MTLComputeCommandEncoder> encoder = [cmdBuf computeCommandEncoder];
        CHECK(encoder != nil, "computeCommandEncoder -> %p", (void *)encoder);

        [encoder setComputePipelineState:pipeline];
        [encoder setBuffer:buffer offset:0 atIndex:0];
        MTLSize one = {1, 1, 1};
        [encoder dispatchThreadgroups:one threadsPerThreadgroup:one];
        [encoder endEncoding];

        [cmdBuf commit];
        [cmdBuf waitUntilCompleted];

        int32_t result = 0;
        memcpy(&result, buffer.contents, 4);
        CHECK(result == 42, "buffer.contents after dispatch = %d (expect 42)", result);

        printf("\n%s\n", ok ? "ALL CHECKS PASSED" : "SOME CHECKS FAILED");
    }
    return ok ? 0 : 1;
}
