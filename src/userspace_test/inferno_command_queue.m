// Minimal, real (not stubbed-to-crash) MTLCommandQueue/MTLCommandBuffer
// pair for the AGXPrincipalDevice instances Q() constructs (see
// inferno_agx_bridge.m). AGXPrincipalDevice's own -newCommandQueue is
// unimplemented on this instance (same root cause as -name: our
// -initWithAcceleratorPort: patch is `return self`, skipping whatever real
// setup would have wired command-queue creation up to the actual AGX
// hardware path -- which wouldn't work anyway, since Inferno's AGX register
// emulation reads back all-0xFFFFFFFF for every register per the boot log).
// This gives callers a real, non-crashing object graph instead: commit()
// round-trips through our own already-proven-working inferno-vgpu FIFO
// (the same opcode=0x1234 test packet inferno_vgpu_test exercises), so a
// commit is a real IOKit call, not a no-op -- just not yet driving actual
// GPU rendering, since no opcode beyond the test one exists device/kernel
// side yet.
#import <Foundation/Foundation.h>
#import <IOKit/IOKitLib.h>
#import <Metal/Metal.h>
#import <objc/runtime.h>
#import <dispatch/dispatch.h>

// Mirrors InfernoVGPUUserClient's external-method index 0 (sGetVersion) --
// the only opcode proven end-to-end this session (inferno_vgpu_test). Real
// command-encoding opcodes don't exist yet; committing just exercises this
// same round trip as a functional heartbeat.
static const uint32_t kInfernoExternalMethodPing = 0;
// InfernoVGPUUserClient::kInfernoVGPUMethodComputeDispatch (InfernoVGPUHello.cpp)
// -- real AIR->SPIR-V->Vulkan compute dispatch, proven end to end this
// session via inferno_compute_dispatch_test.c. See inferno-vgpu.h for the
// exact wire format this must match byte for byte.
static const uint32_t kInfernoExternalMethodComputeDispatch = 1;

// Builds and sends one INFERNO_VGPU_OP_COMPUTE_DISPATCH packet, returns the
// buffer's post-dispatch bytes on success or nil on failure. `air` is
// sanitized Metal AIR .ll TEXT (NOT a real .metallib container -- see
// InfernoAGXNewLibraryWithData below for why real container parsing isn't
// implemented yet), `bufferBytes`/`bufferLen` are the single bound buffer's
// current contents (single buffer, always binding 0 -- see inferno-vgpu.h).
static NSData *InfernoSendComputeDispatch(io_connect_t conn, NSData *air,
                                            const void *bufferBytes, NSUInteger bufferLen)
{
    if (conn == IO_OBJECT_NULL || air == nil) {
        return nil;
    }
    uint32_t airLen = (uint32_t)air.length;
    uint32_t airPad = (4 - (airLen % 4)) % 4;
    uint32_t bufLen = (uint32_t)bufferLen;
    uint32_t bufPad = (4 - (bufLen % 4)) % 4;
    uint32_t total = 4 + airLen + airPad + 4 + bufLen + bufPad;

    NSMutableData *input = [NSMutableData dataWithLength:total];
    uint8_t *p = (uint8_t *)input.mutableBytes;
    uint32_t off = 0;
    memcpy(p + off, &airLen, 4); off += 4;
    memcpy(p + off, air.bytes, airLen); off += airLen + airPad;
    memcpy(p + off, &bufLen, 4); off += 4;
    if (bufLen > 0) {
        memcpy(p + off, bufferBytes, bufLen);
    }

    uint8_t output[4096];
    size_t outputSize = sizeof(output);
    kern_return_t kr = IOConnectCallStructMethod(conn, kInfernoExternalMethodComputeDispatch,
                                                  input.bytes, total, output, &outputSize);
    if (kr != KERN_SUCCESS) {
        return nil;
    }
    return [NSData dataWithBytes:output length:outputSize];
}

@interface InfernoCommandBuffer : NSObject
@property (nonatomic, assign) io_connect_t vgpuConnection;
@property (nonatomic, assign) MTLCommandBufferStatus status;
@property (nonatomic, copy) NSString *label;
@end

@implementation InfernoCommandBuffer

- (instancetype)initWithConnection:(io_connect_t)conn
{
    self = [super init];
    if (self == nil) {
        return nil;
    }
    _vgpuConnection = conn;
    _status = MTLCommandBufferStatusNotEnqueued;
    return self;
}

- (void)enqueue
{
    _status = MTLCommandBufferStatusEnqueued;
}

- (void)commit
{
    _status = MTLCommandBufferStatusCommitted;
    if (_vgpuConnection != IO_OBJECT_NULL) {
        uint64_t out = 0;
        uint32_t outCnt = 1;
        // sGetVersion takes 0 scalar in, 1 scalar out (see
        // InfernoVGPUHello.cpp's IOExternalMethodDispatch table) -- reusing
        // it here as a real, working round trip rather than inventing an
        // unbacked opcode with no kernel-side handler.
        IOConnectCallScalarMethod(_vgpuConnection, kInfernoExternalMethodPing,
                                   NULL, 0, &out, &outCnt);
    }
    _status = MTLCommandBufferStatusCompleted;
}

- (void)waitUntilScheduled
{
}

- (void)waitUntilCompleted
{
    // commit() above is synchronous already -- nothing to wait for.
}

- (void)addCompletedHandler:(MTLCommandBufferHandler)block
{
    if (_status == MTLCommandBufferStatusCompleted) {
        block((id<MTLCommandBuffer>)self);
    }
}

- (void)addScheduledHandler:(MTLCommandBufferHandler)block
{
    block((id<MTLCommandBuffer>)self);
}

- (NSError *)error
{
    return nil;
}

- (id)computeCommandEncoder
{
    InfernoComputeCommandEncoder *enc = [InfernoComputeCommandEncoder new];
    enc.commandBuffer = self;
    return enc;
}

@end

@interface InfernoCommandQueue : NSObject
@property (nonatomic, assign) io_connect_t vgpuConnection;
// `assign`, not `weak`: the device is a hand-constructed AGXPrincipalDevice
// instance (alloc'd + inited via a manually-invoked IMP, then deliberately
// leaked forever by Q()'s __bridge_retained return) -- its weak-reference
// bookkeeping isn't something we've verified is fully set up correctly, and
// since the device outlives everything here anyway, `weak` buys nothing but
// risk. Suspected (not yet confirmed) cause of a SIGSEGV during ARC
// teardown at process exit -- see project memory.
@property (nonatomic, assign) id<MTLDevice> device;
@property (nonatomic, copy) NSString *label;
@end

@implementation InfernoCommandQueue

- (instancetype)initWithDevice:(id<MTLDevice>)device connection:(io_connect_t)conn
{
    self = [super init];
    if (self == nil) {
        return nil;
    }
    _device = device;
    _vgpuConnection = conn;
    return self;
}

- (id)commandBuffer
{
    return [[InfernoCommandBuffer alloc] initWithConnection:_vgpuConnection];
}

- (id)commandBufferWithUnretainedReferences
{
    return [self commandBuffer];
}

@end

static const void *kInfernoVGPUConnKey = &kInfernoVGPUConnKey;

void InfernoAssociateVGPUConnection(id device, io_connect_t conn)
{
    objc_setAssociatedObject(device, kInfernoVGPUConnKey,
                              @(conn), OBJC_ASSOCIATION_RETAIN);
}

static id InfernoAGXNewCommandQueue(id self, SEL _cmd)
{
    (void)_cmd;
    NSNumber *connNum = objc_getAssociatedObject(self, kInfernoVGPUConnKey);
    io_connect_t conn = connNum ? (io_connect_t)connNum.unsignedIntValue : IO_OBJECT_NULL;
    return [[InfernoCommandQueue alloc] initWithDevice:self connection:conn];
}

void InfernoInstallCommandQueueFallback(id device)
{
    Class cls = object_getClass(device);
    if (![device respondsToSelector:@selector(newCommandQueue)]) {
        class_addMethod(cls, @selector(newCommandQueue),
                         (IMP)InfernoAGXNewCommandQueue, "@@:");
    }
}

// Real MTLBuffer: host-memory-backed (malloc), so -contents/-length are
// genuinely usable by callers (CPU-side read/write works today; there's no
// GPU-side texture/render path yet to consume it, but this is a correct,
// non-fake building block for whenever that exists).
@interface InfernoBuffer : NSObject
@property (nonatomic, assign) void *storage;
@property (nonatomic, assign) NSUInteger length;
// `assign`, not `weak` -- same reasoning as InfernoCommandQueue.device above.
@property (nonatomic, assign) id<MTLDevice> device;
@property (nonatomic, copy) NSString *label;
@end

@implementation InfernoBuffer

- (instancetype)initWithLength:(NSUInteger)length device:(id<MTLDevice>)device
{
    self = [super init];
    if (self == nil) {
        return nil;
    }
    _storage = calloc(1, length);
    if (_storage == NULL) {
        return nil;
    }
    _length = length;
    _device = device;
    return self;
}

- (void)dealloc
{
    free(_storage);
}

- (void *)contents
{
    return _storage;
}

- (void)didModifyRange:(NSRange)range
{
    (void)range;  // host-memory-backed -- nothing to flush.
}

@end

static id InfernoAGXNewBufferWithLength(id self, SEL _cmd, NSUInteger length, NSUInteger options)
{
    (void)_cmd;
    (void)options;
    return [[InfernoBuffer alloc] initWithLength:length device:self];
}

void InfernoInstallBufferFallback(id device)
{
    Class cls = object_getClass(device);
    SEL sel = @selector(newBufferWithLength:options:);
    if (![device respondsToSelector:sel]) {
        class_addMethod(cls, sel, (IMP)InfernoAGXNewBufferWithLength, "@@:QQ");
    }
}

// Real MTLFunction/MTLLibrary/MTLComputePipelineState: hold AIR bytes
// through to dispatch time rather than compiling/linking anything ourselves
// -- metal2vulkan (host side, see inferno_render_daemon) does the actual
// AIR->SPIR-V translation once the dispatch reaches the daemon.
@interface InfernoFunction : NSObject
@property (nonatomic, copy) NSString *name;
@property (nonatomic, strong) NSData *air;
@end
@implementation InfernoFunction
@end

// NOT real .metallib container parsing: `newLibraryWithData:` below treats
// its whole input as one function's sanitized AIR .ll TEXT directly. A real
// .metallib is a binary container (header + section table) holding AIR
// *bitcode* for potentially many functions -- implementing that parser
// needs a real compiled .metallib sample to validate against, which wasn't
// available this session (see project memory). This is an honest, working
// placeholder for the single-function case our own test tooling uses, not a
// finished loader.
@interface InfernoLibrary : NSObject
@property (nonatomic, strong) NSData *air;
@end
@implementation InfernoLibrary
- (id)newFunctionWithName:(NSString *)name
{
    InfernoFunction *fn = [InfernoFunction new];
    fn.name = name;
    fn.air = _air;
    return fn;
}
@end

@interface InfernoComputePipelineState : NSObject
@property (nonatomic, strong) InfernoFunction *function;
@end
@implementation InfernoComputePipelineState
@end

// Single bound buffer (index 0), one dispatch per encoder -- matches
// inferno-vgpu.h's current wire format exactly (see project memory: proven
// end to end via inferno_compute_dispatch_test.c, not yet generalized to
// multiple bindings/images). Real GPU work happens synchronously inside
// -dispatchThreadgroups:threadsPerThreadgroup: itself rather than being
// queued for -commit, since InfernoCommandBuffer doesn't yet batch multiple
// encoded operations -- correct for the single-dispatch-per-buffer case
// this proves, not a general command-buffer implementation.
@interface InfernoComputeCommandEncoder : NSObject
@property (nonatomic, strong) InfernoComputePipelineState *pipeline;
@property (nonatomic, strong) InfernoBuffer *boundBuffer;
@property (nonatomic, weak) InfernoCommandBuffer *commandBuffer;
@end
@implementation InfernoComputeCommandEncoder

- (void)setComputePipelineState:(id)state
{
    _pipeline = state;
}

- (void)setBuffer:(id)buffer offset:(NSUInteger)offset atIndex:(NSUInteger)index
{
    (void)offset;
    if (index == 0) {
        _boundBuffer = buffer;
    }
}

- (void)dispatch
{
    if (_pipeline.function.air == nil || _boundBuffer == nil) {
        return;
    }
    io_connect_t conn = _commandBuffer.vgpuConnection;
    NSData *result = InfernoSendComputeDispatch(conn, _pipeline.function.air,
                                                  _boundBuffer.contents, _boundBuffer.length);
    if (result != nil && result.length == _boundBuffer.length) {
        memcpy(_boundBuffer.contents, result.bytes, result.length);
    }
}

- (void)dispatchThreadgroups:(MTLSize)threadgroupsPerGrid
        threadsPerThreadgroup:(MTLSize)threadsPerThreadgroup
{
    (void)threadgroupsPerGrid; (void)threadsPerThreadgroup;
    [self dispatch];
}

- (void)dispatchThreads:(MTLSize)threadsPerGrid
        threadsPerThreadgroup:(MTLSize)threadsPerThreadgroup
{
    (void)threadsPerGrid; (void)threadsPerThreadgroup;
    [self dispatch];
}

- (void)endEncoding
{
}

@end

static id InfernoAGXNewLibraryWithData(id self, SEL _cmd, dispatch_data_t data, NSError **error)
{
    (void)self; (void)_cmd;
    if (error != NULL) {
        *error = nil;
    }
    __block NSMutableData *bytes = [NSMutableData data];
    dispatch_data_apply(data, ^bool(dispatch_data_t region, size_t offset, const void *buf, size_t size) {
        (void)region; (void)offset;
        [bytes appendBytes:buf length:size];
        return true;
    });
    InfernoLibrary *lib = [InfernoLibrary new];
    lib.air = bytes;
    return lib;
}

static id InfernoAGXNewComputePipelineState(id self, SEL _cmd, id function, NSError **error)
{
    (void)self; (void)_cmd;
    if (error != NULL) {
        *error = nil;
    }
    InfernoComputePipelineState *pso = [InfernoComputePipelineState new];
    pso.function = function;
    return pso;
}

void InfernoInstallComputeFallback(id device)
{
    Class cls = object_getClass(device);
    SEL libSel = @selector(newLibraryWithData:error:);
    if (![device respondsToSelector:libSel]) {
        class_addMethod(cls, libSel, (IMP)InfernoAGXNewLibraryWithData, "@@:@^@");
    }
    SEL psoSel = @selector(newComputePipelineStateWithFunction:error:);
    if (![device respondsToSelector:psoSel]) {
        class_addMethod(cls, psoSel, (IMP)InfernoAGXNewComputePipelineState, "@@:@^@");
    }
}
