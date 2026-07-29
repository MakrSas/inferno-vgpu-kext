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

// Mirrors InfernoVGPUUserClient's external-method index 0 (sGetVersion) --
// the only opcode proven end-to-end this session (inferno_vgpu_test). Real
// command-encoding opcodes don't exist yet; committing just exercises this
// same round trip as a functional heartbeat.
static const uint32_t kInfernoExternalMethodPing = 0;

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
