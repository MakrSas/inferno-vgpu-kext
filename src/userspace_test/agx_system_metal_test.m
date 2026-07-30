// The actual "does this work system-wide" proof: unlike every other test in
// this directory, this one does NOT dlopen("/b")/dlsym("Q")/call it directly.
// It calls the plain, standard, public MTLCreateSystemDefaultDevice() --
// exactly what any unmodified real app/framework on the system calls -- and
// relies entirely on the already-deployed ___MTLCreateSystemDefaultDevice_
// block_invoke patch (patch_block_invoke.py) to route that call to our
// bridge. If this test passes, every real Metal-using process on the system
// (not just our own custom test harnesses) gets a real, working device with
// the full compute+render fallback surface -- because the patch calls the
// exact same Q() (inferno_agx_bridge.m) our other tests call explicitly, so
// redeploying /b's dylib updates system-wide behavior without re-patching
// machine code.
//
// Exercises the full render pipeline (same shape as agx_metal_api_draw_test)
// to prove it's not just device discovery that works system-wide, but the
// entire object graph through to a real rasterized GPU result.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

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

#define CHECK(cond, fmt, ...) do { \
    if (cond) { printf("OK   " fmt "\n", ##__VA_ARGS__); } \
    else { printf("FAIL " fmt "\n", ##__VA_ARGS__); ok = 0; } \
} while (0)

int main(void)
{
    int ok = 1;
    @autoreleasepool {
        // The whole point: no dlopen, no dlsym, no Q(). Just the standard,
        // public entry point any app on the system uses.
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        CHECK(device != nil, "MTLCreateSystemDefaultDevice() -> %p", (__bridge void *)device);
        if (device == nil) {
            printf("\nSOME CHECKS FAILED\n");
            return 1;
        }
        printf("device.name = %s\n", device.name.UTF8String);

        const NSUInteger kWidth = 16, kHeight = 16;

        MTLTextureDescriptor *texDesc = [MTLTextureDescriptor
            texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                          width:kWidth height:kHeight mipmapped:NO];
        id<MTLTexture> target = [device newTextureWithDescriptor:texDesc];
        CHECK(target != nil, "newTextureWithDescriptor: -> %p", (__bridge void *)target);

        dispatch_data_t vertData = dispatch_data_create(
            kVertAir, sizeof(kVertAir) - 1, dispatch_get_main_queue(), DISPATCH_DATA_DESTRUCTOR_DEFAULT);
        dispatch_data_t fragData = dispatch_data_create(
            kFragAir, sizeof(kFragAir) - 1, dispatch_get_main_queue(), DISPATCH_DATA_DESTRUCTOR_DEFAULT);
        NSError *error = nil;
        id<MTLLibrary> vertLib = [device newLibraryWithData:vertData error:&error];
        id<MTLLibrary> fragLib = [device newLibraryWithData:fragData error:&error];
        CHECK(vertLib != nil && fragLib != nil, "newLibraryWithData: (vert+frag) -> %p, %p",
              (__bridge void *)vertLib, (__bridge void *)fragLib);

        id<MTLFunction> vertFn = [vertLib newFunctionWithName:@"vmain"];
        id<MTLFunction> fragFn = [fragLib newFunctionWithName:@"frag"];
        CHECK(vertFn != nil && fragFn != nil, "newFunctionWithName: (vert+frag) -> %p, %p",
              (__bridge void *)vertFn, (__bridge void *)fragFn);

        MTLRenderPipelineDescriptor *pDesc = [MTLRenderPipelineDescriptor new];
        pDesc.vertexFunction = vertFn;
        pDesc.fragmentFunction = fragFn;
        id<MTLRenderPipelineState> pipeline =
            [device newRenderPipelineStateWithDescriptor:pDesc error:&error];
        CHECK(pipeline != nil, "newRenderPipelineStateWithDescriptor: -> %p", (__bridge void *)pipeline);

        float verts[3][4] = {
            {0.0f, 0.6f, 0.0f, 1.0f},
            {-0.6f, -0.6f, 0.0f, 1.0f},
            {0.6f, -0.6f, 0.0f, 1.0f},
        };
        id<MTLBuffer> vbuf = [device newBufferWithLength:sizeof(verts) options:0];
        memcpy(vbuf.contents, verts, sizeof(verts));
        CHECK(vbuf != nil, "newBufferWithLength: -> %p", (__bridge void *)vbuf);

        id<MTLCommandQueue> queue = [device newCommandQueue];
        id<MTLCommandBuffer> cmdBuf = [queue commandBuffer];
        CHECK(queue != nil && cmdBuf != nil, "newCommandQueue/commandBuffer -> %p, %p",
              (__bridge void *)queue, (__bridge void *)cmdBuf);

        MTLRenderPassDescriptor *passDesc = [MTLRenderPassDescriptor renderPassDescriptor];
        passDesc.colorAttachments[0].texture = target;
        passDesc.colorAttachments[0].loadAction = MTLLoadActionClear;
        passDesc.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 0);

        id<MTLRenderCommandEncoder> encoder = [cmdBuf renderCommandEncoderWithDescriptor:passDesc];
        CHECK(encoder != nil, "renderCommandEncoderWithDescriptor: -> %p", (__bridge void *)encoder);

        [encoder setRenderPipelineState:pipeline];
        [encoder setVertexBuffer:vbuf offset:0 atIndex:0];
        [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
        [encoder endEncoding];

        [cmdBuf commit];
        [cmdBuf waitUntilCompleted];

        uint8_t pixels[16 * 16 * 4];
        [target getBytes:pixels bytesPerRow:16 * 4
               fromRegion:MTLRegionMake2D(0, 0, kWidth, kHeight) mipmapLevel:0];

        uint8_t *center = &pixels[(8 * 16 + 8) * 4];
        uint8_t *corner = &pixels[(1 * 16 + 1) * 4];
        printf("center pixel RGBA = %d,%d,%d,%d\n", center[0], center[1], center[2], center[3]);
        printf("corner pixel RGBA = %d,%d,%d,%d\n", corner[0], corner[1], corner[2], corner[3]);
        CHECK(center[0] == 255 && center[1] == 0 && center[2] == 0 && center[3] == 255,
              "center pixel is solid red (inside triangle)");
        CHECK(corner[0] == 0 && corner[1] == 0 && corner[2] == 0 && corner[3] == 0,
              "corner pixel is cleared (outside triangle)");

        printf("\n%s\n", ok ? "ALL CHECKS PASSED" : "SOME CHECKS FAILED");
    }
    return ok ? 0 : 1;
}
