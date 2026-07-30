// Sends a real INFERNO_VGPU_OP_PRESENT through
// InfernoVGPUHello/InfernoVGPUUserClient::sPresentDispatch: the same
// vertex_passthrough/fragment_solid_red triangle already proven end to end
// off-screen (agx_metal_api_draw_test), but this time the device blits the
// rendered frame straight onto the guest's own live display genpipe --
// see adp_v4_present_frame() in hw/display/apple_displaypipe_v4.c and
// inferno-vgpu.c's INFERNO_VGPU_OP_PRESENT handling. Nothing comes back to
// this process except a status code; success means a red triangle should now
// be visible on the actual emulated screen at (dest_x, dest_y).
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <IOKit/IOKitLib.h>
#include <CoreFoundation/CoreFoundation.h>

enum {
    kInfernoVGPUMethodPresentDispatch = 3,
};

static const char kVertAir[] =
    "source_filename = \"vertex_passthrough.metal\"\n"
    "target datalayout = \"e-p:64:64:64\"\n"
    "target triple = \"air64-apple-macosx14.0.0\"\n"
    "\n"
    "define <4 x float> @vmain(<4 x float> %position) local_unnamed_addr #0 {\n"
    "  ret <4 x float> %position\n"
    "}\n"
    "\n"
    "attributes #0 = { nounwind }\n"
    "\n"
    "!air.vertex = !{!0}\n"
    "!0 = !{ptr @vmain, !1, !2}\n"
    "!1 = !{!3}\n"
    "!2 = !{!4}\n"
    "!3 = !{!\"air.position\", !\"air.arg_type_name\", !\"float4\"}\n"
    "!4 = !{i32 0, !\"air.vertex_input\", !\"air.location_index\", i32 0, i32 1, "
    "!\"air.arg_type_name\", !\"float4\", !\"air.arg_name\", !\"position\"}\n";

static const char kFragAir[] =
    "source_filename = \"fragment_solid_red.metal\"\n"
    "target datalayout = \"e-p:64:64:64\"\n"
    "target triple = \"air64-apple-macosx14.0.0\"\n"
    "\n"
    "define <4 x float> @frag(<4 x float> %position) local_unnamed_addr #0 {\n"
    "  %r = insertelement <4 x float> undef, float 1.000000e+00, i64 0\n"
    "  %rg = insertelement <4 x float> %r, float 0.000000e+00, i64 1\n"
    "  %rgb = insertelement <4 x float> %rg, float 0.000000e+00, i64 2\n"
    "  %rgba = insertelement <4 x float> %rgb, float 1.000000e+00, i64 3\n"
    "  ret <4 x float> %rgba\n"
    "}\n"
    "\n"
    "attributes #0 = { nounwind }\n"
    "\n"
    "!air.fragment = !{!0}\n"
    "!0 = !{ptr @frag, !1, !2}\n"
    "!1 = !{!3}\n"
    "!2 = !{!4}\n"
    "!3 = !{i32 0, !\"air.render_target\", i32 0, i32 0, !\"air.arg_type_name\", !\"float4\"}\n"
    "!4 = !{i32 0, !\"air.position\", !\"air.center\", !\"air.arg_type_name\", !\"float4\"}\n";

int main(int argc, char **argv)
{
    kern_return_t kr;
    io_iterator_t iter;
    io_service_t service = IO_OBJECT_NULL;
    io_connect_t conn = IO_OBJECT_NULL;

    uint32_t width = 200, height = 200, dest_x = 50, dest_y = 50;
    if (argc >= 5) {
        width = (uint32_t)atoi(argv[1]);
        height = (uint32_t)atoi(argv[2]);
        dest_x = (uint32_t)atoi(argv[3]);
        dest_y = (uint32_t)atoi(argv[4]);
    }

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

    // Clip-space triangle: top, bottom-left, bottom-right (float4 x,y,z,w).
    float verts[3][4] = {
        {0.0f, 0.6f, 0.0f, 1.0f},
        {-0.6f, -0.6f, 0.0f, 1.0f},
        {0.6f, -0.6f, 0.0f, 1.0f},
    };

    uint32_t vert_len = (uint32_t)(sizeof(kVertAir) - 1);
    uint32_t vert_pad = (4 - (vert_len % 4)) % 4;
    uint32_t frag_len = (uint32_t)(sizeof(kFragAir) - 1);
    uint32_t frag_pad = (4 - (frag_len % 4)) % 4;
    uint32_t vbuf_len = (uint32_t)sizeof(verts);
    uint32_t vbuf_pad = (4 - (vbuf_len % 4)) % 4;
    uint32_t vertex_count = 3;

    uint32_t total = 4 + vert_len + vert_pad + 4 + frag_len + frag_pad +
                     4 + vbuf_len + vbuf_pad + 4 + 4 + 4 + 4 + 4;
    uint8_t *input = calloc(1, total);
    if (input == NULL) {
        fprintf(stderr, "calloc failed\n");
        IOServiceClose(conn);
        return 1;
    }

    uint32_t off = 0;
    memcpy(input + off, &vert_len, 4); off += 4;
    memcpy(input + off, kVertAir, vert_len); off += vert_len + vert_pad;
    memcpy(input + off, &frag_len, 4); off += 4;
    memcpy(input + off, kFragAir, frag_len); off += frag_len + frag_pad;
    memcpy(input + off, &vbuf_len, 4); off += 4;
    memcpy(input + off, verts, vbuf_len); off += vbuf_len + vbuf_pad;
    memcpy(input + off, &width, 4); off += 4;
    memcpy(input + off, &height, 4); off += 4;
    memcpy(input + off, &vertex_count, 4); off += 4;
    memcpy(input + off, &dest_x, 4); off += 4;
    memcpy(input + off, &dest_y, 4); off += 4;

    printf("PRESENT: %ux%u @ (%u,%u), payload=%u bytes\n", width, height, dest_x, dest_y, total);

    uint64_t scalarOutput[1] = {0};
    uint32_t scalarOutputCnt = 1;

    kr = IOConnectCallMethod(conn, kInfernoVGPUMethodPresentDispatch,
                             NULL, 0, input, total,
                             scalarOutput, &scalarOutputCnt, NULL, NULL);
    free(input);

    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "PresentDispatch failed: 0x%x\n", kr);
        IOServiceClose(conn);
        return 1;
    }

    printf("PresentDispatch status = %llu (0 = ok)\n", (unsigned long long)scalarOutput[0]);
    IOServiceClose(conn);
    return (scalarOutput[0] == 0) ? 0 : 2;
}
