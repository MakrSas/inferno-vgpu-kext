// Sends a real INFERNO_VGPU_OP_COMPUTE_DISPATCH through
// InfernoVGPUHello/InfernoVGPUUserClient::sComputeDispatch: AIR text for the
// trivial `store_const` kernel (stores 42 into a bound buffer) + a 4-byte
// zeroed buffer. The kernel forwards this to inferno-vgpu.c's device model,
// which forwards it to inferno-render-daemon (metal2vulkan + reims-vgpu's
// Vulkan engine, on the real host GPU), and the result should come back
// as the real GPU-computed value. See inferno-vgpu.h for the wire format.
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <IOKit/IOKitLib.h>
#include <CoreFoundation/CoreFoundation.h>

enum {
    kInfernoVGPUMethodComputeDispatch = 1,
};

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

int main(void)
{
    kern_return_t kr;
    io_iterator_t iter;
    io_service_t service = IO_OBJECT_NULL;
    io_connect_t conn = IO_OBJECT_NULL;

    CFMutableDictionaryRef matching = IOServiceMatching(kIOServiceClass);
    CFMutableDictionaryRef props = CFDictionaryCreateMutable(
        kCFAllocatorDefault, 0, &kCFTypeDictionaryKeyCallBacks,
        &kCFTypeDictionaryValueCallBacks);
    CFDictionarySetValue(props, CFSTR("MetalPluginClassName"),
                          CFSTR("InfernoVGPUMetalDevice"));
    CFDictionarySetValue(matching, CFSTR(kIOPropertyMatchKey), props);
    CFRelease(props);

    kr = IOServiceGetMatchingServices(MACH_PORT_NULL, matching, &iter);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "IOServiceGetMatchingServices failed: 0x%x\n", kr);
        return 1;
    }
    service = IOIteratorNext(iter);
    IOObjectRelease(iter);
    if (service == IO_OBJECT_NULL) {
        fprintf(stderr, "InfernoVGPUHello not found\n");
        return 1;
    }

    kr = IOServiceOpen(service, mach_task_self(), 0, &conn);
    IOObjectRelease(service);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "IOServiceOpen failed: 0x%x\n", kr);
        return 1;
    }
    printf("IOServiceOpen succeeded, connection=0x%x\n", conn);

    // Build the wire payload: u32 air_len, air_bytes (4-byte padded),
    // u32 buf_len, buf_bytes (4-byte padded).
    uint32_t air_len = (uint32_t)(sizeof(kAirText) - 1); // exclude NUL
    uint32_t air_pad = (4 - (air_len % 4)) % 4;
    uint32_t buf_len = 4;
    uint32_t buf_pad = 0; // already 4-byte aligned

    uint32_t total = 4 + air_len + air_pad + 4 + buf_len + buf_pad;
    uint8_t *input = calloc(1, total);
    if (input == NULL) {
        fprintf(stderr, "calloc failed\n");
        IOServiceClose(conn);
        return 1;
    }

    uint32_t off = 0;
    memcpy(input + off, &air_len, 4); off += 4;
    memcpy(input + off, kAirText, air_len); off += air_len + air_pad;
    memcpy(input + off, &buf_len, 4); off += 4;
    memset(input + off, 0, buf_len); // zeroed input buffer

    uint8_t output[64];
    size_t outputSize = sizeof(output);

    kr = IOConnectCallStructMethod(conn, kInfernoVGPUMethodComputeDispatch,
                                    input, total, output, &outputSize);
    free(input);

    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "ComputeDispatch failed: 0x%x\n", kr);
        IOServiceClose(conn);
        return 1;
    }

    printf("ComputeDispatch returned %zu bytes\n", outputSize);
    if (outputSize == 4) {
        int32_t result;
        memcpy(&result, output, 4);
        printf("result = %d (expect 42)\n", result);
        IOServiceClose(conn);
        return (result == 42) ? 0 : 2;
    }

    IOServiceClose(conn);
    return 3;
}
