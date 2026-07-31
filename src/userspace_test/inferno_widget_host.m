// Prototype principal class for the "replace an already-installed,
// already-hosted widget .appex's binary in place" strategy -- see
// PROJECT_STATUS.md's "Widget-hosted Metal compositing design and
// prototype (2026-07-31)" section for the full writeup. This is item 4 of
// this project's standing priority list: get real Metal-rendered content
// composited into the live interface by backboardd's existing,
// COMPLETELY UNMODIFIED compositing logic, via CoreAnimation's private
// cross-process CAContext/hostingChain mechanism -- the same one this
// project's own investigation confirmed is already live and in active use,
// right now, by real Today-View widget extensions
// (WeatherWidget/StocksWidget/GeneralMapsWidget/etc.).
//
// STRATEGY, and why this file does NOT touch CAContext/CARenderServer/
// hostingChain APIs directly at all: this binary is meant to *replace* an
// already-installed, already-provisioned, already-proven-to-get-hosted
// widget .appex's compiled executable in place (same bundle path, same
// entitlements/provisioning -- only the executable's bytes change, plus
// possibly one Info.plist string, see below) -- so the REAL, completely
// unmodified UIKit/PlugInKit extension-hosting machinery keeps doing 100%
// of the CAContext creation / hostingChain registration work for us, the
// exact same way it already does for the real widget being replaced. Our
// whole job shrinks to: be a normal, functioning widget-extension
// principal class whose view's CALayer shows this project's real
// Metal-rendered content, refreshed on a timer to prove genuinely ongoing
// (not one-shot) compositing -- as opposed to trying to reimplement any
// part of the private hosting protocol from scratch, which would require
// reverse-engineering a large, undocumented, DSC-resident private API
// surface this project has zero traction on yet (see PROJECT_STATUS.md's
// repeated "ipsw/DSC-parser tooling gap" notes).
//
// DESIGN CHOICE: CGImage-backed layer.contents, not IOSurface. The task
// that produced this file's own design doc explicitly floated
// "IOSurface-backed CALayer" as the most-realistic-sounding shape, flagged
// as "not gospel, validate/adjust as you learn". This file deviates from
// that, on purpose, for three concrete reasons (see PROJECT_STATUS.md for
// the full reasoning): (1) this project's whole Metal render pipeline is
// already fundamentally CPU-round-trip-based end to end (a synchronous
// IOKit call into the host's Vulkan renderer, then a getBytes-style CPU
// copy back -- see inferno_render_encoder.m) -- there is no existing
// GPU-resident buffer this project could hand to IOSurface zero-copy even
// if it wanted to, so IOSurface would buy nothing but risk here; (2)
// CGImage-backed CALayer.contents is 100% public, extremely well-trodden
// API with zero private-API risk, vs. IOSurface pixel-format/lock/
// bytes-per-row semantics this project has never once exercised anywhere
// in its whole test suite (confirmed by grep, see PROJECT_STATUS.md); (3)
// nothing in the actual success criterion ("Metal-rendered content
// composited into the live interface by backboardd's existing, unmodified
// compositing logic") requires IOSurface specifically -- any CALayer
// content type CoreAnimation's own hosting-chain protocol serializes
// across the CAContext boundary satisfies it identically from backboardd's
// point of view. IOSurface remains a reasonable *later* upgrade if a
// zero-copy GPU-resident path is ever built (would need reims-vgpu output
// to land directly in an IOSurface-backed buffer instead of a CPU
// getBytes-style copy -- not the case today).
//
// KNOWN OPEN QUESTIONS -- this file has never been run, only (attempted to
// be) compiled via CI, per this session's hard no-live-guest-access
// constraint. Do not treat it as finished or verified working:
//   1. **The single most important unknown**: whether the target widget's
//      NSExtensionPointIdentifier is the legacy NCWidgetProviding one
//      (com.apple.widget-extension -- what this file assumes/targets) or
//      the modern WidgetKit one (com.apple.widgetkit-extension) -- a
//      fundamentally different, SwiftUI/timeline-snapshot-based
//      architecture with no live-hosted CALayer view at all, which this
//      whole approach would NOT work for. Needs exactly one live guest
//      read of the target .appex's Info.plist before attempting a real
//      deployment -- see PROJECT_STATUS.md for the exact command. Given
//      this project's own "Today-View widget" terminology and the
//      one-appex-per-widget process shape observed (WeatherWidget,
//      StocksWidget, GeneralMapsWidget, PhotosReliveWidget,
//      ScreenTimeWidgetExtension each its own separate process), the
//      legacy NCWidgetProviding model is the better-supported guess (its
//      one-extension-per-widget shape matches what was observed; WidgetKit
//      typically hosts multiple widgets from a single per-app extension
//      process) -- but this is an inference, not a confirmed fact.
//   2. Whether PlugInKit/RunningBoard's own extension-launch validation has
//      entitlement checks beyond this project's 5 known, already-patched
//      SIGKILL gates (all found via bare unsigned standalone test binaries
//      executed at "/", a materially different launch path than however
//      PlugInKit actually spawns an extension process).
//   3. Whether `-Wl,-e,_NSExtensionMain` actually produces a valid,
//      loadable LC_MAIN entry against this exact toolchain/SDK/OS
//      combination -- the first real signal is whether this file's own CI
//      job (widget-host-prototype, .github/workflows/build.yml) links
//      successfully at all. Real Xcode App Extension targets have no
//      visible main() and use exactly this linker flag to make Foundation's
//      exported NSExtensionMain() the entry point (it reads the bundle's
//      own Info.plist at runtime to find NSExtensionPrincipalClass and
//      instantiate it) -- this file follows that same shape, on purpose,
//      rather than writing a custom main() that would have to reimplement
//      whatever handshake NSExtensionMain does with PlugInKit itself.
//
// Rendering pipeline (dlopen("/b") -> Q() -> device -> texture -> two
// libraries -> pipeline -> vertex buffer -> queue -> command buffer ->
// render encoder -> draw -> commit -> getBytes) is copied verbatim in shape
// from the already-proven agx_metal_api_draw_test.m (see PROJECT_STATUS.md's
// "Fully proven, working, verified on the actual guest" section) -- same
// two AIR shaders, same call sequence. The only difference: the vertex
// buffer's horizontal offset is animated by a phase counter incremented
// once per timer tick, specifically so a genuinely live/ongoing render is
// visually distinguishable from a static single frame, if/when this is
// ever actually screenshotted on the guest -- directly contrasting with the
// existing on-screen-triangle milestone's post-hoc genpipe-overwrite
// mechanism (see PROJECT_STATUS.md task 3 of the App-level investigation:
// that mechanism is architecturally a dead end for exactly this kind of
// cooperative, ongoing content -- this file's whole point is to prove the
// opposite, cooperative shape works instead).
#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <QuartzCore/QuartzCore.h>
#import <Metal/Metal.h>
#import <dlfcn.h>
#include <math.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>

// Identical AIR text to agx_metal_api_draw_test.m's kVertAir/kFragAir --
// deliberately NOT re-derived/simplified, to keep this file's rendering
// behavior byte-for-byte traceable back to the already-proven test.
static const char kInfernoVertAir[] =
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

static const char kInfernoFragAir[] =
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

// Diagnostic-only, same spirit/rationale as inferno_agx_bridge.m's QTrace:
// this class can be instantiated in a very fragile, hard-to-observe context
// (a real widget-extension process spawned by PlugInKit, whose stdout isn't
// obviously reachable the way a plain execve()'d test binary's is) -- so
// every step appends one line to a plain file via raw POSIX I/O, cheap
// enough to always run.
static void WTrace(const char *msg)
{
    int fd = open("/tmp/widget_host_trace.log", O_WRONLY | O_CREAT | O_APPEND, 0666);
    if (fd < 0) {
        return;
    }
    write(fd, msg, strlen(msg));
    write(fd, "\n", 1);
    close(fd);
}

// Widget-extension lifecycle selectors below are hand-implemented by
// SELECTOR NAME ONLY, deliberately NOT declared via formal <NCWidgetProviding>
// protocol conformance -- this avoids a compile-time dependency on
// NotificationCenter.framework's NCWidgetProviding.h, which may or may not
// still exist in whatever iOS SDK version the CI runner's Xcode ships
// (NCWidgetProviding has been deprecated since iOS 14 in favor of
// WidgetKit, and could plausibly be removed from a newer SDK header set
// entirely). PlugInKit's own runtime dispatch to a principal class is
// respondsToSelector:-based, not a static protocol-conformance check, so a
// plain implementation with matching selector names and ABI-compatible
// signatures is sufficient and has zero header-availability risk -- same
// "hand-declare what you need instead of depending on an unavailable
// header" pattern this project already uses elsewhere (see
// bash_present_builtin.m's hand-declared `struct builtin`).
@interface InfernoWidgetHost : UIViewController
@end

@implementation InfernoWidgetHost {
    id<MTLDevice> _device;
    id<MTLLibrary> _vertLib;
    id<MTLLibrary> _fragLib;
    id<MTLRenderPipelineState> _pipeline;
    id<MTLCommandQueue> _queue;
    id<MTLTexture> _target;
    NSTimer *_timer;
    double _phase;
    NSUInteger _texWidth;
    NSUInteger _texHeight;
}

- (void)viewDidLoad
{
    [super viewDidLoad];
    WTrace("viewDidLoad: enter");
    _texWidth = 64;
    _texHeight = 64;
    self.view.backgroundColor = [UIColor blackColor];

    if ([self inferno_setUpDevice]) {
        WTrace("viewDidLoad: device set up ok, rendering first frame");
        [self inferno_renderAndPresent];
        // 1s cadence: slow enough to stay cheap given each frame is a real
        // synchronous IOKit round trip into the host's Vulkan renderer
        // (see PROJECT_STATUS.md's render pipeline notes), fast enough to
        // be visually obviously "live" rather than static if/when this is
        // ever actually screenshotted.
        _timer = [NSTimer scheduledTimerWithTimeInterval:1.0
                                                    target:self
                                                  selector:@selector(inferno_timerTick:)
                                                  userInfo:nil
                                                   repeats:YES];
    } else {
        WTrace("viewDidLoad: device set up FAILED, no rendering will happen");
    }
}

- (void)inferno_timerTick:(NSTimer *)timer
{
    (void)timer;
    _phase += 0.3;
    [self inferno_renderAndPresent];
}

// One-time setup: dlopen("/b") -> Q() -> device, then build the (fixed,
// reused-every-frame) texture/library/pipeline/queue objects. Splitting
// this from the per-frame render step (unlike agx_metal_api_draw_test.m,
// which does everything once and exits) matters here specifically because
// this class's whole point is to render MANY times across this process's
// live, ongoing lifetime -- rebuilding the pipeline/libraries from scratch
// on every 1s tick would work but wastes real IOKit/dispatch overhead for
// no reason.
- (BOOL)inferno_setUpDevice
{
    void *handle = dlopen("/b", RTLD_NOW);
    if (handle == NULL) {
        WTrace("inferno_setUpDevice: dlopen(/b) failed");
        return NO;
    }
    void *(*fn)(void) = (void *(*)(void))dlsym(handle, "Q");
    void *raw = fn ? fn() : NULL;
    if (raw == NULL) {
        WTrace("inferno_setUpDevice: Q() returned NULL");
        return NO;
    }
    _device = (__bridge id<MTLDevice>)raw;
    WTrace("inferno_setUpDevice: got device");

    MTLTextureDescriptor *texDesc = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                      width:_texWidth height:_texHeight mipmapped:NO];
    _target = [_device newTextureWithDescriptor:texDesc];
    if (_target == nil) {
        WTrace("inferno_setUpDevice: newTextureWithDescriptor failed");
        return NO;
    }

    dispatch_data_t vertData = dispatch_data_create(
        kInfernoVertAir, sizeof(kInfernoVertAir) - 1,
        dispatch_get_main_queue(), DISPATCH_DATA_DESTRUCTOR_DEFAULT);
    dispatch_data_t fragData = dispatch_data_create(
        kInfernoFragAir, sizeof(kInfernoFragAir) - 1,
        dispatch_get_main_queue(), DISPATCH_DATA_DESTRUCTOR_DEFAULT);
    NSError *error = nil;
    _vertLib = [_device newLibraryWithData:vertData error:&error];
    _fragLib = [_device newLibraryWithData:fragData error:&error];
    if (_vertLib == nil || _fragLib == nil) {
        WTrace("inferno_setUpDevice: newLibraryWithData failed");
        return NO;
    }

    id<MTLFunction> vertFn = [_vertLib newFunctionWithName:@"vmain"];
    id<MTLFunction> fragFn = [_fragLib newFunctionWithName:@"frag"];
    if (vertFn == nil || fragFn == nil) {
        WTrace("inferno_setUpDevice: newFunctionWithName failed");
        return NO;
    }

    MTLRenderPipelineDescriptor *pDesc = [MTLRenderPipelineDescriptor new];
    pDesc.vertexFunction = vertFn;
    pDesc.fragmentFunction = fragFn;
    _pipeline = [_device newRenderPipelineStateWithDescriptor:pDesc error:&error];
    if (_pipeline == nil) {
        WTrace("inferno_setUpDevice: newRenderPipelineStateWithDescriptor failed");
        return NO;
    }

    _queue = [_device newCommandQueue];
    if (_queue == nil) {
        WTrace("inferno_setUpDevice: newCommandQueue failed");
        return NO;
    }
    WTrace("inferno_setUpDevice: all objects built ok");
    return YES;
}

// Real per-frame work: build a fresh vertex buffer (horizontal offset
// driven by _phase), encode+commit+wait one draw exactly like
// agx_metal_api_draw_test.m's proven sequence, read the result back to CPU,
// and hand it to the view's layer as a CGImage.
- (void)inferno_renderAndPresent
{
    if (_device == nil || _pipeline == nil || _queue == nil || _target == nil) {
        return;
    }

    float dx = 0.35f * (float)sin(_phase);
    float verts[3][4] = {
        {0.0f + dx, 0.6f, 0.0f, 1.0f},
        {-0.6f + dx, -0.6f, 0.0f, 1.0f},
        {0.6f + dx, -0.6f, 0.0f, 1.0f},
    };
    id<MTLBuffer> vbuf = [_device newBufferWithLength:sizeof(verts) options:0];
    if (vbuf == nil) {
        WTrace("inferno_renderAndPresent: newBufferWithLength failed");
        return;
    }
    memcpy(vbuf.contents, verts, sizeof(verts));

    id<MTLCommandBuffer> cmdBuf = [_queue commandBuffer];
    if (cmdBuf == nil) {
        WTrace("inferno_renderAndPresent: commandBuffer failed");
        return;
    }

    MTLRenderPassDescriptor *passDesc = [MTLRenderPassDescriptor renderPassDescriptor];
    passDesc.colorAttachments[0].texture = _target;
    passDesc.colorAttachments[0].loadAction = MTLLoadActionClear;
    passDesc.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 1);

    id<MTLRenderCommandEncoder> encoder = [cmdBuf renderCommandEncoderWithDescriptor:passDesc];
    if (encoder == nil) {
        WTrace("inferno_renderAndPresent: renderCommandEncoderWithDescriptor failed");
        return;
    }
    [encoder setRenderPipelineState:_pipeline];
    [encoder setVertexBuffer:vbuf offset:0 atIndex:0];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [encoder endEncoding];

    [cmdBuf commit];
    [cmdBuf waitUntilCompleted];

    NSUInteger bytesPerRow = _texWidth * 4;
    NSMutableData *pixels = [NSMutableData dataWithLength:bytesPerRow * _texHeight];
    [_target getBytes:pixels.mutableBytes bytesPerRow:bytesPerRow
           fromRegion:MTLRegionMake2D(0, 0, _texWidth, _texHeight) mipmapLevel:0];

    [self inferno_presentPixels:pixels width:_texWidth height:_texHeight bytesPerRow:bytesPerRow];
    WTrace("inferno_renderAndPresent: frame presented");
}

// Builds a CGImage directly over `pixels`'s bytes (no extra copy) and
// assigns it as the view's backing layer's contents -- the one and only
// point of contact with CoreAnimation in this whole file. Everything else
// about getting this layer's content across the process boundary into
// backboardd's compositing is handled entirely by UIKit/PlugInKit's own,
// completely unmodified extension-hosting machinery (see the file-level
// comment above) -- deliberately nothing here touches CAContext,
// CARenderServer, or hostingChain directly.
- (void)inferno_presentPixels:(NSData *)pixels width:(NSUInteger)width
                        height:(NSUInteger)height bytesPerRow:(NSUInteger)bytesPerRow
{
    CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();
    // `pixels` (an NSMutableData, ARC-retained by being a parameter/local
    // for the duration of this call) is kept alive by the CFDataRef bridge
    // below for exactly as long as CGDataProviderCreateWithCFData needs it
    // -- using the CFData-based provider constructor instead of the raw
    // CGDataProviderCreateWithData(NULL, bytes, ...) form specifically so
    // the provider itself keeps the backing store alive (via CFRetain)
    // instead of requiring the caller to guarantee `pixels`' bytes outlive
    // the CGImageRef by some other, easier-to-get-wrong means.
    CGDataProviderRef provider = CGDataProviderCreateWithCFData((__bridge CFDataRef)pixels);
    CGImageRef image = CGImageCreate(width, height, 8, 32, bytesPerRow, cs,
                                      kCGImageAlphaPremultipliedLast, provider,
                                      NULL, false, kCGRenderingIntentDefault);
    CGDataProviderRelease(provider);
    CGColorSpaceRelease(cs);
    if (image != NULL) {
        self.view.layer.contents = (__bridge id)image;
        CGImageRelease(image);
    } else {
        WTrace("inferno_presentPixels: CGImageCreate failed");
    }
}

// --- Widget-extension lifecycle selectors PlugInKit's own runtime dispatch
// looks for (see the file-level comment above for why these are plain,
// header-independent selector implementations rather than a formally
// declared <NCWidgetProviding> conformance). NCUpdateResult's real
// definition is `typedef NS_ENUM(NSInteger, NCUpdateResult)` with
// NCUpdateResultNewData == 0 in the real header -- hand-using a plain
// NSInteger with the literal value keeps the ABI identical without needing
// the header.

- (void)widgetPerformUpdateWithCompletionHandler:(void (^)(NSInteger))completionHandler
{
    WTrace("widgetPerformUpdateWithCompletionHandler: called");
    [self inferno_renderAndPresent];
    if (completionHandler != nil) {
        completionHandler(0 /* NCUpdateResultNewData */);
    }
}

- (UIEdgeInsets)widgetMarginInsetsForProposedMarginInsets:(UIEdgeInsets)defaultMarginInsets
{
    return defaultMarginInsets;
}

- (BOOL)widgetAllowsEditingForCompactMode
{
    return NO;
}

@end

// Deliberately NO main()/NSExtensionMain() reimplementation here -- see the
// file-level comment above. The real entry point is Foundation's own
// exported NSExtensionMain(), wired up via this project's CI job
// (widget-host-prototype in .github/workflows/build.yml) using
// `-Wl,-e,_NSExtensionMain`, exactly matching how real Xcode-built App
// Extension targets link (they have no main.m of their own either). At
// runtime, NSExtensionMain() reads the hosting bundle's own Info.plist to
// find NSExtensionPrincipalClass and instantiates it -- meaning this file
// never needs to know or care what its own bundle's path/UUID is; that's
// entirely PlugInKit/Foundation's job, unchanged from how it already works
// for the real widget this binary is meant to replace.
