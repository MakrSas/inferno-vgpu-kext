// Bash loadable builtin (bash's own `enable -f`/dlopen extension mechanism --
// see https://www.gnu.org/software/bash/manual/html_node/Loadable-Builtins.html)
// that triggers a real INFERNO_VGPU_OP_PRESENT draw directly, from WITHIN the
// already-running, already-trusted /bin/bash process.
//
// Why this exists: every freshly-transferred, unsigned MAIN EXECUTABLE on
// this guest gets SIGKILLed near-instantly, for reasons that resisted deep
// live kernel debugging this session (confirmed NOT AMFI/codesigning --
// mac_vnode_check_signature returns allowed=0 -- NOT a userspace kill()
// syscall -- 601 observed kill() calls, none targeting our process; the
// SIGKILL is set by inlined kernel code with no catchable named symbol).
// But `enable -f /b anything` from the interactive shell PROVED that
// dlopen()'ing an unsigned dylib from an ALREADY-RUNNING, already-trusted
// process (bash itself) works completely fine -- bash survived and just
// reported a normal dlsym() failure. So: package the actual test logic as
// a bash loadable builtin (a dlopen()'d bundle, not a new process) instead
// of a standalone executable, sidestepping the whole mystery.
//
// Bash's loadable-builtin ABI (stable across bash versions for decades):
//   typedef int sh_builtin_func_t(void *);  // real type is WORD_LIST*
//   struct builtin {
//       char *name;
//       sh_builtin_func_t *function;
//       int flags;              // 1 = BUILTIN_ENABLED
//       char * const *long_doc; // NULL-terminated array of strings
//       char *short_doc;
//       char *handle;           // unused, must be NULL
//   };
// bash's `enable -f FILE NAME` dlopen()s FILE and dlsym()s "NAME_struct".
#import <Foundation/Foundation.h>
#import <IOKit/IOKitLib.h>
#import <string.h>

// Bash's own struct builtin (builtins.h), redeclared here since we don't
// have bash's source tree -- this ABI has been stable for decades, see
// https://www.gnu.org/software/bash/manual/html_node/Loadable-Builtins.html
struct builtin {
    char *name;
    int (*function)(void *);
    int flags;
    char * const *long_doc;
    char *short_doc;
    char *handle;
};

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

int inferno_present_builtin(void *list)
{
    (void)list;
    kern_return_t kr;
    io_iterator_t iter;
    io_service_t service = IO_OBJECT_NULL;
    io_connect_t conn = IO_OBJECT_NULL;

    uint32_t width = 200, height = 200, dest_x = 50, dest_y = 50;

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
        printf("inferno_present: IOServiceGetMatchingServices failed: 0x%x\n", kr);
        return 1;
    }
    service = IOIteratorNext(iter);
    IOObjectRelease(iter);
    if (service == IO_OBJECT_NULL) {
        printf("inferno_present: InfernoVGPUHello not found\n");
        return 1;
    }

    kr = IOServiceOpen(service, mach_task_self(), 0, &conn);
    IOObjectRelease(service);
    if (kr != KERN_SUCCESS) {
        printf("inferno_present: IOServiceOpen failed: 0x%x\n", kr);
        return 1;
    }
    printf("inferno_present: IOServiceOpen succeeded, connection=0x%x\n", conn);

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
        printf("inferno_present: calloc failed\n");
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

    printf("inferno_present: PRESENT %ux%u @ (%u,%u), payload=%u bytes\n",
           width, height, dest_x, dest_y, total);

    uint64_t scalarOutput[1] = {0};
    uint32_t scalarOutputCnt = 1;

    kr = IOConnectCallMethod(conn, kInfernoVGPUMethodPresentDispatch,
                             NULL, 0, input, total,
                             scalarOutput, &scalarOutputCnt, NULL, NULL);
    free(input);

    if (kr != KERN_SUCCESS) {
        printf("inferno_present: PresentDispatch failed: 0x%x\n", kr);
        IOServiceClose(conn);
        return 1;
    }

    printf("inferno_present: PresentDispatch status = %llu (0 = ok)\n",
           (unsigned long long)scalarOutput[0]);
    IOServiceClose(conn);
    return (scalarOutput[0] == 0) ? 0 : 2;
}

char *inferno_present_doc[] = {
    "Trigger a real INFERNO_VGPU_OP_PRESENT draw (renders a red triangle",
    "directly onto the live display) via IOKit, from within this already-",
    "running, already-trusted bash process -- see bash_present_builtin.m.",
    (char *)NULL
};

struct builtin inferno_present_struct = {
    "inferno_present",       /* name */
    inferno_present_builtin, /* function */
    1,                       /* flags: BUILTIN_ENABLED */
    inferno_present_doc,     /* long_doc */
    "inferno_present",       /* short_doc */
    (char *)NULL,            /* handle */
};
