// Real (not stubbed-to-crash) MTLRenderPipelineState/MTLRenderCommandEncoder/
// MTLTexture for the AGXPrincipalDevice instances Q() constructs. Mirrors
// inferno_command_queue.m's compute-side pattern exactly: real work happens
// synchronously at the draw call itself (no batching across encoders/command
// buffers yet), round-tripping through InfernoVGPUHello's
// kInfernoVGPUMethodDrawDispatch (index 2) -> INFERNO_VGPU_OP_DRAW ->
// inferno-vgpu.c -> inferno_render_daemon -> metal2vulkan + reims-vgpu's
// Vulkan engine on the real host GPU. See inferno-vgpu.h for the exact wire
// format. MTLRenderPassDescriptor/MTLRenderPipelineDescriptor/
// MTLTextureDescriptor are real, standalone Apple value classes (not
// device-tied) -- used as-is, no fallback needed for them.
#import <Foundation/Foundation.h>
#import <IOKit/IOKitLib.h>
#import <Metal/Metal.h>
#import <objc/runtime.h>

// InfernoVGPUUserClient::kInfernoVGPUMethodDrawDispatch (InfernoVGPUHello.cpp).
static const uint32_t kInfernoExternalMethodDrawDispatch = 2;

static id InfernoLeakForeverR(id obj)
{
    if (obj != nil) {
        CFBridgingRetain(obj);
    }
    return obj;
}

// Builds and sends one INFERNO_VGPU_OP_DRAW packet, returns RGBA8 pixel
// bytes (width*height*4) on success or nil on failure.
static NSData *InfernoSendDrawDispatch(io_connect_t conn, NSData *vertAir, NSData *fragAir,
                                         NSData *vbuf, uint32_t width, uint32_t height,
                                         uint32_t vertexCount)
{
    if (conn == IO_OBJECT_NULL || vertAir == nil || fragAir == nil) {
        return nil;
    }
    uint32_t vertLen = (uint32_t)vertAir.length;
    uint32_t vertPad = (4 - (vertLen % 4)) % 4;
    uint32_t fragLen = (uint32_t)fragAir.length;
    uint32_t fragPad = (4 - (fragLen % 4)) % 4;
    uint32_t vbufLen = (uint32_t)vbuf.length;
    uint32_t vbufPad = (4 - (vbufLen % 4)) % 4;
    uint32_t total = 4 + vertLen + vertPad + 4 + fragLen + fragPad + 4 + vbufLen + vbufPad + 12;

    NSMutableData *input = [NSMutableData dataWithLength:total];
    uint8_t *p = (uint8_t *)input.mutableBytes;
    uint32_t off = 0;
    memcpy(p + off, &vertLen, 4); off += 4;
    memcpy(p + off, vertAir.bytes, vertLen); off += vertLen + vertPad;
    memcpy(p + off, &fragLen, 4); off += 4;
    memcpy(p + off, fragAir.bytes, fragLen); off += fragLen + fragPad;
    memcpy(p + off, &vbufLen, 4); off += 4;
    if (vbufLen > 0) {
        memcpy(p + off, vbuf.bytes, vbufLen);
    }
    off += vbufLen + vbufPad;
    memcpy(p + off, &width, 4); off += 4;
    memcpy(p + off, &height, 4); off += 4;
    memcpy(p + off, &vertexCount, 4); off += 4;

    uint32_t pixelBytes = width * height * 4;
    NSMutableData *output = [NSMutableData dataWithLength:pixelBytes];
    size_t outputSize = pixelBytes;

    kern_return_t kr = IOConnectCallStructMethod(conn, kInfernoExternalMethodDrawDispatch,
                                                  input.bytes, total,
                                                  output.mutableBytes, &outputSize);
    if (kr != KERN_SUCCESS || outputSize != pixelBytes) {
        return nil;
    }
    return output;
}

// Real MTLTexture: host-memory-backed RGBA8 pixels. Only what's needed to
// receive a draw's output and read it back -- no mipmaps, no non-RGBA8
// formats, no GPU-side sampling yet (see project memory for scope notes).
@interface InfernoTexture : NSObject
@property (nonatomic, assign) NSUInteger width;
@property (nonatomic, assign) NSUInteger height;
@property (nonatomic, strong) NSMutableData *pixelData;
@end
@implementation InfernoTexture

- (instancetype)initWithWidth:(NSUInteger)width height:(NSUInteger)height
{
    self = [super init];
    if (self == nil) {
        return nil;
    }
    _width = width;
    _height = height;
    _pixelData = [NSMutableData dataWithLength:width * height * 4];
    return self;
}

- (void)getBytes:(void *)pixelBytes bytesPerRow:(NSUInteger)bytesPerRow
       fromRegion:(MTLRegion)region mipmapLevel:(NSUInteger)level
{
    (void)region; (void)level;
    NSUInteger rowBytes = _width * 4;
    NSUInteger copyRowBytes = (bytesPerRow < rowBytes) ? bytesPerRow : rowBytes;
    const uint8_t *src = (const uint8_t *)_pixelData.bytes;
    uint8_t *dst = (uint8_t *)pixelBytes;
    for (NSUInteger y = 0; y < _height; y++) {
        memcpy(dst + y * bytesPerRow, src + y * rowBytes, copyRowBytes);
    }
}

@end

static id InfernoAGXNewTextureWithDescriptor(id self, SEL _cmd, MTLTextureDescriptor *desc)
{
    (void)self; (void)_cmd;
    return InfernoLeakForeverR([[InfernoTexture alloc] initWithWidth:desc.width height:desc.height]);
}

void InfernoInstallTextureFallback(id device)
{
    Class cls = object_getClass(device);
    SEL sel = @selector(newTextureWithDescriptor:);
    if (![device respondsToSelector:sel]) {
        class_addMethod(cls, sel, (IMP)InfernoAGXNewTextureWithDescriptor, "@@:@");
    }
}

// Real MTLRenderPipelineState: just holds onto the vertex/fragment
// InfernoFunctions (their AIR text) through to draw time -- metal2vulkan
// does the actual translation once the draw reaches the daemon, same as
// the compute pipeline state.
@interface InfernoRenderPipelineState : NSObject
@property (nonatomic, strong) id vertexFunction;
@property (nonatomic, strong) id fragmentFunction;
@end
@implementation InfernoRenderPipelineState
@end

static id InfernoAGXNewRenderPipelineState(id self, SEL _cmd,
                                             MTLRenderPipelineDescriptor *desc, NSError **error)
{
    (void)self; (void)_cmd;
    if (error != NULL) {
        *error = nil;
    }
    InfernoRenderPipelineState *pso = [InfernoRenderPipelineState new];
    pso.vertexFunction = desc.vertexFunction;
    pso.fragmentFunction = desc.fragmentFunction;
    return InfernoLeakForeverR(pso);
}

void InfernoInstallRenderPipelineFallback(id device)
{
    Class cls = object_getClass(device);
    SEL sel = @selector(newRenderPipelineStateWithDescriptor:error:);
    if (![device respondsToSelector:sel]) {
        class_addMethod(cls, sel, (IMP)InfernoAGXNewRenderPipelineState, "@@:@^@");
    }
}

// Single draw per encoder, single vertex buffer at index 0 -- matches
// inferno-vgpu.h's current INFERNO_VGPU_OP_DRAW wire format exactly (see
// project memory: proven end to end locally via host_render_poc/try_draw.rs
// before this file existed). Real GPU work happens synchronously inside
// -drawPrimitives:vertexStart:vertexCount: itself, same pattern as
// InfernoComputeCommandEncoder.
@interface InfernoRenderCommandEncoder : NSObject
@property (nonatomic, strong) InfernoRenderPipelineState *pipeline;
@property (nonatomic, strong) id vertexBuffer;
@property (nonatomic, strong) InfernoTexture *target;
// `assign`, not `weak`/`strong` -- see InfernoComputeCommandEncoder.commandBuffer
// (project memory: weak alone didn't stop a real SIGSEGV-on-exit; every
// object here is leaked forever via InfernoLeakForeverR instead, so the
// exact ownership of this back-pointer no longer matters for correctness).
@property (nonatomic, assign) id commandBuffer;
@end
@implementation InfernoRenderCommandEncoder

- (void)setRenderPipelineState:(id)state
{
    _pipeline = state;
}

- (void)setVertexBuffer:(id)buffer offset:(NSUInteger)offset atIndex:(NSUInteger)index
{
    (void)offset;
    if (index == 0) {
        _vertexBuffer = buffer;
    }
}

- (void)drawPrimitives:(MTLPrimitiveType)primitiveType
            vertexStart:(NSUInteger)vertexStart
            vertexCount:(NSUInteger)vertexCount
{
    (void)primitiveType; (void)vertexStart;
    if (_pipeline.vertexFunction == nil || _pipeline.fragmentFunction == nil ||
       _vertexBuffer == nil || _target == nil) {
        return;
    }
    io_connect_t conn = 0;
    // `_commandBuffer` is `id` (cross-file, InfernoCommandBuffer's real
    // @interface isn't visible in this translation unit) -- read its
    // vgpuConnection via KVC rather than a forward-declared selector
    // category (simpler than inferno_command_queue.m's
    // NSObject-category trick for a single property read).
    NSNumber *connNum = [_commandBuffer valueForKey:@"vgpuConnection"];
    conn = connNum ? (io_connect_t)connNum.unsignedIntValue : IO_OBJECT_NULL;

    NSData *vertAir = [_pipeline.vertexFunction valueForKey:@"air"];
    NSData *fragAir = [_pipeline.fragmentFunction valueForKey:@"air"];
    NSData *vbufData = [NSData dataWithBytes:[_vertexBuffer valueForKey:@"contents"]
                                       length:[[_vertexBuffer valueForKey:@"length"] unsignedIntegerValue]];

    NSData *pixels = InfernoSendDrawDispatch(conn, vertAir, fragAir, vbufData,
                                              (uint32_t)_target.width, (uint32_t)_target.height,
                                              (uint32_t)vertexCount);
    if (pixels != nil && pixels.length == _target.pixelData.length) {
        [_target.pixelData replaceBytesInRange:NSMakeRange(0, pixels.length) withBytes:pixels.bytes];
    }
}

- (void)endEncoding
{
}

@end

static id InfernoAGXRenderCommandEncoder(id self, SEL _cmd, MTLRenderPassDescriptor *desc)
{
    (void)_cmd;
    id enc = InfernoLeakForeverR([NSClassFromString(@"InfernoRenderCommandEncoder") new]);
    [enc setValue:self forKey:@"commandBuffer"];
    [enc setValue:desc.colorAttachments[0].texture forKey:@"target"];
    return enc;
}

void InfernoInstallRenderCommandEncoderFallback(id commandBuffer)
{
    Class cls = object_getClass(commandBuffer);
    SEL sel = @selector(renderCommandEncoderWithDescriptor:);
    if (![commandBuffer respondsToSelector:sel]) {
        class_addMethod(cls, sel, (IMP)InfernoAGXRenderCommandEncoder, "@@:@");
    }
}
