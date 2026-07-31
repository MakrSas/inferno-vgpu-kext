// Real WidgetKit host<->extension XPC responder -- direct follow-up to
// inferno_widget_host_main.m (kept unmodified alongside this file, same
// precedent as every other "preserve the prior variant" pair in this
// project, e.g. agx_system_metal_test.m / agx_system_metal_test_direct.m).
//
// inferno_widget_host_main.m fixed the entry-point shape (real main(), not
// -e _NSExtensionMain) but deliberately implemented NO real WidgetKit
// registration protocol -- its own file comment says so explicitly, and
// the live test built on top of it (PROJECT_STATUS.md, "Widget-hosted
// Metal compositing: main()-shape fix + live test..." section) confirmed
// the entry-point fix alone is NOT sufficient: WidgetKit's host falls back
// to its own generic placeholder chrome ("No content available") because
// nothing in that process ever answers chronod's real getDescriptors/
// getTimeline XPC calls. This file is the direct next step: implement
// that protocol for real, using the REAL selectors and type-encoding
// strings (not guessed from names) found via dsc_parse.py's new
// objc-protocol command against WidgetKit.framework's own DSC-resident
// ObjC metadata -- see PROJECT_STATUS.md's dated section for this session
// for the full derivation, addresses, and cross-checks.
//
// ---------------------------------------------------------------------
// Real findings this file is built against (all from
// `python3 dsc_parse.py objc-protocol WidgetKit.framework/WidgetKit
// HostToExtensionXPCInterface`, cross-checked against 3 redundant on-disk
// copies of the same protocol_t, all agreeing):
//
//   HostToExtensionXPCInterface (Swift-mangled ObjC name
//   _TtP9WidgetKit27HostToExtensionXPCInterface_) has exactly 7 REQUIRED
//   instance methods, in a classic "big" (pointer-based, NOT small/
//   relative) method_list_t:
//     invalidate                                            v16@0:8
//     performCleanup                                        v16@0:8
//     getDescriptorsWithCompletion:                         v24@0:8@?16
//       completion block: void (^)(NSArray *)
//     getPlaceholdersWithEnvironment:for:completion:        v40@0:8@16@24@?32
//       args: CHKWidgetEnvironment*, NSDictionary*, then
//       completion block void (^)(NSError *)
//     handleURLSessionEventsFor:completion:                 v32@0:8@16@?24
//       args: NSString*, then completion block void (^)(void)
//     attachPreviewAgentWithFrameworkPath:endpoint:handler: v40@0:8@16@24@?32
//       args: NSString*, id (endpoint -- xpc_object_t/NSXPCListenerEndpoint-
//       shaped, no class-name annotation in the real ext_types either, see
//       below), then handler block void (^)(id) [real ext_types names the
//       block arg BSAuditToken*, a private class this file doesn't need to
//       reference directly since it's typed `id` on our side]
//     getTimelineFor:into:environment:isPreview:completion: v52@0:8@16@24@32B40@?44
//       args: CHSWidget*, NSFileHandle*, CHKWidgetEnvironment*, BOOL
//       (encodes as a real one-byte 'B' -- confirmed empirically, matches
//       this arm64 SDK's OBJC_BOOL_IS_BOOL=1 real _Bool BOOL), then
//       completion block void (^)(NSError *)
//
//   ExtensionToHostXPCInterface, by contrast, was found GENUINELY EMPTY
//   (0 methods in either of its 2 on-disk copies, zero protocol
//   refinement list, zero property list) in this exact WidgetKit build
//   (Chrono-97.1) -- i.e. this file does NOT need to implement or consume
//   that interface at all; every reverse callback this protocol needs
//   (delivering descriptors/timeline data/errors back to the host) goes
//   through the completion blocks passed INTO HostToExtensionXPCInterface's
//   own methods, not a separate connection/protocol.
//
// The protocol below is declared locally as
// InfernoHostToExtensionXPCInterface (a different ObjC name from Apple's
// real `_TtP9WidgetKit27HostToExtensionXPCInterface_`) deliberately -- we
// do not need to BE that exact runtime protocol object (NSXPCConnection
// dispatches by selector name across the wire, not by matching protocol_t
// identity between processes; each side independently compiles/loads its
// own local protocol declaration and only needs binary-compatible
// selectors/type encodings, which is exactly what was matched above,
// field-by-field). Giving it our own name avoids any chance of an ObjC
// runtime class/protocol-name collision if WidgetKit.framework's own
// metadata ever gets loaded into this process too (see the ChronoKit/
// ChronoServices dlopen note below, which loads real framework CLASSES,
// though not WidgetKit.framework's own protocol object specifically).
//
// CHSWidget / CHKWidgetEnvironment: forward-declared only, deliberately
// NOT given real @interface bodies -- dsc_parse.py's new objc-class
// command was used to inspect CHSWidget's real class_ro_t this session:
// it is a pure Swift-bridged, ALL-READONLY value type (5 ivars:
// extensionBundleIdentifier/containerBundleIdentifier/kind NSString*,
// family int64, intent INIntent*; 9 properties, every one tagged `R`
// (readonly) with NO ObjC-visible init method in its own class_ro_t
// baseMethods list at all -- 0 entries). This means a real CHSWidget
// instance CANNOT be constructed from hand-written ObjC via KVC/alloc-init
// the way this project's own classes are -- its real designated
// initializer, if any, is a Swift-mangled symbol requiring the Swift
// calling convention, out of scope for this session. This is *why*
// getDescriptorsWithCompletion: below replies with an empty array rather
// than attempting a real descriptor -- a deliberate, evidence-based
// decision, not an oversight; see PROJECT_STATUS.md for the full ivar/
// property dump this conclusion is based on.
//
// Bootstrap/listener mechanism: StocksWidget.appex/Info.plist's own
// CFBundlePackageType is `XPC!` (already on record from a prior session's
// plist dump) -- i.e. structurally, this bundle IS an XPC service package,
// the exact shape the PUBLIC `+[NSXPCListener serviceListener]` API is
// built for (this is the same, standard mechanism Xcode's own "XPC
// Service" template main.m uses; no private WidgetKit/PlugInKit symbols
// needed for the listener setup itself -- this directly answers this
// session's own open design question in the affirmative: PlugInKit/
// Foundation's standard extension-hosting machinery DOES cover this once
// the binary presents the right package shape, no manual mach-service
// bootstrap needed on our side).
//
// Render pipeline: identical dlopen("/b") -> Q() -> device -> texture ->
// two MTLLibrarys -> pipeline -> queue -> per-frame buffer/encoder/draw/
// commit/getBytes sequence as inferno_widget_host_main.m, run on a
// repeating NSTimer once a real run loop is available (this file uses one,
// unlike the plain-loop main_.m variant, because a real run loop is
// mandatory here for XPC message delivery). Kept purely as an ongoing
// liveness signal for now -- actually wiring a rendered frame INTO a real
// WidgetKit timeline response needs a valid NSKeyedArchiver payload
// matching WidgetKit's own (currently unknown) on-wire timeline-entry
// format for the NSFileHandle argument of getTimelineFor:into:..., which
// is real, substantial, separate reverse-engineering work not attempted
// this session -- see PROJECT_STATUS.md's own "concrete next steps" for
// this section.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <dlfcn.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>
#include <math.h>
#include <stdio.h>

// Forward declarations only -- see the file-level comment above for why
// full @interface bodies are neither available nor needed. Using real
// forward-declared ObjC classes here (rather than typing these params as
// `id`) makes clang emit the real class-name-free `@` encoding in the
// method's plain `types` string (matching the real protocol's plain types
// exactly, confirmed identical either way) while still giving
// NSXPCInterface's own runtime introspection a concrete static type to
// key its default NSSecureCoding allowed-class inference off of.
@class CHKWidgetEnvironment;
@class CHSWidget;

static void WTrace(const char *msg)
{
    int fd = open("/tmp/widget_host_xpc_trace.log", O_WRONLY | O_CREAT | O_APPEND, 0666);
    if (fd < 0) {
        return;
    }
    char pidbuf[64];
    int n = snprintf(pidbuf, sizeof(pidbuf), "[pid %d] ", getpid());
    if (n > 0) {
        write(fd, pidbuf, (size_t)n);
    }
    write(fd, msg, strlen(msg));
    write(fd, "\n", 1);
    close(fd);
}

#pragma mark - Real Metal render pipeline (identical to inferno_widget_host_main.m)

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

static id<MTLDevice> gDevice;
static id<MTLLibrary> gVertLib;
static id<MTLLibrary> gFragLib;
static id<MTLRenderPipelineState> gPipeline;
static id<MTLCommandQueue> gQueue;
static id<MTLTexture> gTarget;
static const NSUInteger kTexWidth = 64;
static const NSUInteger kTexHeight = 64;
static unsigned long gFrameIndex = 0;
static double gPhase = 0.0;

static BOOL SetUpDevice(void)
{
    void *handle = dlopen("/b", RTLD_NOW);
    if (handle == NULL) {
        WTrace("SetUpDevice: dlopen(/b) failed");
        return NO;
    }
    void *(*fn)(void) = (void *(*)(void))dlsym(handle, "Q");
    void *raw = fn ? fn() : NULL;
    if (raw == NULL) {
        WTrace("SetUpDevice: Q() returned NULL");
        return NO;
    }
    gDevice = (__bridge id<MTLDevice>)raw;
    WTrace("SetUpDevice: got device");

    MTLTextureDescriptor *texDesc = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                      width:kTexWidth height:kTexHeight mipmapped:NO];
    gTarget = [gDevice newTextureWithDescriptor:texDesc];
    if (gTarget == nil) {
        WTrace("SetUpDevice: newTextureWithDescriptor failed");
        return NO;
    }

    dispatch_data_t vertData = dispatch_data_create(
        kVertAir, sizeof(kVertAir) - 1,
        dispatch_get_main_queue(), DISPATCH_DATA_DESTRUCTOR_DEFAULT);
    dispatch_data_t fragData = dispatch_data_create(
        kFragAir, sizeof(kFragAir) - 1,
        dispatch_get_main_queue(), DISPATCH_DATA_DESTRUCTOR_DEFAULT);
    NSError *error = nil;
    gVertLib = [gDevice newLibraryWithData:vertData error:&error];
    gFragLib = [gDevice newLibraryWithData:fragData error:&error];
    if (gVertLib == nil || gFragLib == nil) {
        WTrace("SetUpDevice: newLibraryWithData failed");
        return NO;
    }

    id<MTLFunction> vertFn = [gVertLib newFunctionWithName:@"vmain"];
    id<MTLFunction> fragFn = [gFragLib newFunctionWithName:@"frag"];
    if (vertFn == nil || fragFn == nil) {
        WTrace("SetUpDevice: newFunctionWithName failed");
        return NO;
    }

    MTLRenderPipelineDescriptor *pDesc = [MTLRenderPipelineDescriptor new];
    pDesc.vertexFunction = vertFn;
    pDesc.fragmentFunction = fragFn;
    gPipeline = [gDevice newRenderPipelineStateWithDescriptor:pDesc error:&error];
    if (gPipeline == nil) {
        WTrace("SetUpDevice: newRenderPipelineStateWithDescriptor failed");
        return NO;
    }

    gQueue = [gDevice newCommandQueue];
    if (gQueue == nil) {
        WTrace("SetUpDevice: newCommandQueue failed");
        return NO;
    }
    WTrace("SetUpDevice: all objects built ok");
    return YES;
}

static void RenderOneFrame(void)
{
    if (gDevice == nil || gPipeline == nil || gQueue == nil || gTarget == nil) {
        return;
    }

    float dx = 0.35f * (float)sin(gPhase);
    float verts[3][4] = {
        {0.0f + dx, 0.6f, 0.0f, 1.0f},
        {-0.6f + dx, -0.6f, 0.0f, 1.0f},
        {0.6f + dx, -0.6f, 0.0f, 1.0f},
    };
    id<MTLBuffer> vbuf = [gDevice newBufferWithLength:sizeof(verts) options:0];
    if (vbuf == nil) {
        WTrace("RenderOneFrame: newBufferWithLength failed");
        return;
    }
    memcpy(vbuf.contents, verts, sizeof(verts));

    id<MTLCommandBuffer> cmdBuf = [gQueue commandBuffer];
    if (cmdBuf == nil) {
        WTrace("RenderOneFrame: commandBuffer failed");
        return;
    }

    MTLRenderPassDescriptor *passDesc = [MTLRenderPassDescriptor renderPassDescriptor];
    passDesc.colorAttachments[0].texture = gTarget;
    passDesc.colorAttachments[0].loadAction = MTLLoadActionClear;
    passDesc.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 1);

    id<MTLRenderCommandEncoder> encoder = [cmdBuf renderCommandEncoderWithDescriptor:passDesc];
    if (encoder == nil) {
        WTrace("RenderOneFrame: renderCommandEncoderWithDescriptor failed");
        return;
    }
    [encoder setRenderPipelineState:gPipeline];
    [encoder setVertexBuffer:vbuf offset:0 atIndex:0];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [encoder endEncoding];

    [cmdBuf commit];
    [cmdBuf waitUntilCompleted];

    NSUInteger bytesPerRow = kTexWidth * 4;
    static uint8_t pixels[64 /* kTexWidth */ * 4 * 64 /* kTexHeight */];
    [gTarget getBytes:pixels bytesPerRow:bytesPerRow
           fromRegion:MTLRegionMake2D(0, 0, kTexWidth, kTexHeight) mipmapLevel:0];

    char msg[160];
    snprintf(msg, sizeof(msg),
             "RenderOneFrame: frame %lu ok, pixel[0]=%02x%02x%02x%02x",
             gFrameIndex, pixels[0], pixels[1], pixels[2], pixels[3]);
    WTrace(msg);
    gFrameIndex++;
    gPhase += 0.3;
}

#pragma mark - Real HostToExtensionXPCInterface-shaped protocol

// See the file-level comment block above for the derivation of every
// selector/type below -- transcribed field-by-field from
// `dsc_parse.py objc-protocol WidgetKit.framework/WidgetKit
// HostToExtensionXPCInterface`, not guessed from selector names.
@protocol InfernoHostToExtensionXPCInterface <NSObject>
- (void)invalidate;
- (void)performCleanup;
- (void)getDescriptorsWithCompletion:(void (^)(NSArray *descriptors))completion;
- (void)getPlaceholdersWithEnvironment:(CHKWidgetEnvironment *)environment
                                    for:(NSDictionary *)forDict
                             completion:(void (^)(NSError *error))completion;
- (void)handleURLSessionEventsFor:(NSString *)identifier
                        completion:(void (^)(void))completion;
- (void)attachPreviewAgentWithFrameworkPath:(NSString *)frameworkPath
                                    endpoint:(id)endpoint
                                     handler:(void (^)(id auditToken))handler;
- (void)getTimelineFor:(CHSWidget *)widget
                   into:(NSFileHandle *)fileHandle
            environment:(CHKWidgetEnvironment *)environment
              isPreview:(BOOL)isPreview
             completion:(void (^)(NSError *error))completion;
@end

@interface InfernoWidgetXPCResponder : NSObject <InfernoHostToExtensionXPCInterface, NSXPCListenerDelegate>
@end

@implementation InfernoWidgetXPCResponder

- (void)invalidate
{
    WTrace("CALL invalidate");
}

- (void)performCleanup
{
    WTrace("CALL performCleanup");
}

- (void)getDescriptorsWithCompletion:(void (^)(NSArray *))completion
{
    // See the file-level comment above: real CHSWidget instances are
    // Swift-native, all-readonly, no ObjC-visible initializer -- not
    // constructible from here this session. Empty array is the safe,
    // valid (non-crashing, correctly-typed) minimal response: "this
    // extension declares zero widget kinds". Whether WidgetKit's host
    // still calls getTimelineFor:... after this, or stops here, is itself
    // useful information -- logged either way via this same trace file.
    WTrace("CALL getDescriptorsWithCompletion: -> replying with empty array (see file comment: CHSWidget is not constructible from ObjC here)");
    if (completion) {
        completion(@[]);
    }
    WTrace("getDescriptorsWithCompletion: completion invoked");
}

- (void)getPlaceholdersWithEnvironment:(CHKWidgetEnvironment *)environment
                                    for:(NSDictionary *)forDict
                             completion:(void (^)(NSError *))completion
{
    char msg[192];
    snprintf(msg, sizeof(msg),
             "CALL getPlaceholdersWithEnvironment:for:completion:  environment=%s  forDict=%s",
             environment ? [[(id)environment description] UTF8String] : "(nil)",
             forDict ? [[(id)forDict description] UTF8String] : "(nil)");
    WTrace(msg);
    if (completion) {
        completion(nil);
    }
    WTrace("getPlaceholdersWithEnvironment:for:completion: completion invoked (nil error)");
}

- (void)handleURLSessionEventsFor:(NSString *)identifier
                        completion:(void (^)(void))completion
{
    char msg[192];
    snprintf(msg, sizeof(msg), "CALL handleURLSessionEventsFor:completion:  identifier=%s",
             identifier ? [identifier UTF8String] : "(nil)");
    WTrace(msg);
    if (completion) {
        completion();
    }
    WTrace("handleURLSessionEventsFor:completion: completion invoked");
}

- (void)attachPreviewAgentWithFrameworkPath:(NSString *)frameworkPath
                                    endpoint:(id)endpoint
                                     handler:(void (^)(id))handler
{
    char msg[192];
    snprintf(msg, sizeof(msg),
             "CALL attachPreviewAgentWithFrameworkPath:endpoint:handler:  path=%s",
             frameworkPath ? [frameworkPath UTF8String] : "(nil)");
    WTrace(msg);
    if (handler) {
        handler(nil);
    }
    WTrace("attachPreviewAgentWithFrameworkPath:endpoint:handler: handler invoked (nil)");
}

- (void)getTimelineFor:(CHSWidget *)widget
                   into:(NSFileHandle *)fileHandle
            environment:(CHKWidgetEnvironment *)environment
              isPreview:(BOOL)isPreview
             completion:(void (^)(NSError *))completion
{
    char msg[256];
    snprintf(msg, sizeof(msg),
             "CALL getTimelineFor:into:environment:isPreview:completion:  widget=%s  isPreview=%d",
             widget ? [[(id)widget description] UTF8String] : "(nil)", (int)isPreview);
    WTrace(msg);

    // Force one extra render right now so the trace log timestamps a real
    // GPU round trip at the exact moment a real timeline request arrives
    // (on top of the ongoing per-second timer tick) -- purely a liveness/
    // correlation signal for reading the log afterward.
    RenderOneFrame();

    // We do NOT know WidgetKit's real on-wire timeline-archive format for
    // the NSFileHandle payload (would need real CHSWidget/TimelineEntry/
    // WidgetKit private NSSecureCoding key names -- not reverse-engineered
    // this session, see PROJECT_STATUS.md's own next-steps). Write nothing
    // and close the handle to signal a clean (if empty) EOF to whatever is
    // reading the other end, then report success via the completion block
    // -- the most information-dense honest attempt available without that
    // format: if WidgetKit's host crashes/logs a parse error on an empty
    // payload, that's itself a real, useful signal about the format's
    // shape (e.g. whether it tolerates/expects a valid-but-empty archive).
    if (fileHandle) {
        @try {
            [fileHandle closeFile];
            WTrace("getTimelineFor:...: closed fileHandle with zero bytes written");
        } @catch (NSException *exc) {
            char emsg[192];
            snprintf(emsg, sizeof(emsg), "getTimelineFor:...: closeFile raised: %s",
                     [[exc description] UTF8String]);
            WTrace(emsg);
        }
    } else {
        WTrace("getTimelineFor:...: fileHandle was nil");
    }

    if (completion) {
        completion(nil);
    }
    WTrace("getTimelineFor:into:environment:isPreview:completion: completion invoked (nil error)");
}

#pragma mark - NSXPCListenerDelegate

- (BOOL)listener:(NSXPCListener *)listener shouldAcceptNewConnection:(NSXPCConnection *)newConnection
{
    WTrace("shouldAcceptNewConnection: incoming connection");
    NSXPCInterface *iface =
        [NSXPCInterface interfaceWithProtocol:@protocol(InfernoHostToExtensionXPCInterface)];
    newConnection.exportedInterface = iface;
    newConnection.exportedObject = self;
    newConnection.invalidationHandler = ^{
        WTrace("connection invalidationHandler fired");
    };
    newConnection.interruptionHandler = ^{
        WTrace("connection interruptionHandler fired");
    };
    [newConnection resume];
    WTrace("shouldAcceptNewConnection: exportedInterface/exportedObject set, resumed, returning YES");
    return YES;
}

@end

#pragma mark - main()

// Real, plain, compiled main() -- same entry-point shape as
// inferno_widget_host_main.m and the real StocksWidget binary's own
// LC_MAIN (see that file's own header comment for the full derivation of
// why this matters). Unlike that file, this one DOES run a real run loop
// (dispatch_main(), never returns) -- mandatory here since NSXPCListener/
// NSXPCConnection message delivery depends on one; the plain sleep()-loop
// design was specific to that file's own "no hosted anything, just prove
// liveness" scope, which no longer applies once we're actually trying to
// receive real XPC messages.
int main(void)
{
    @autoreleasepool {
        char msg[64];
        snprintf(msg, sizeof(msg), "main: enter, pid=%d", getpid());
        WTrace(msg);
    }

    // Best-effort: load the real ChronoKit/ChronoServices private
    // frameworks so their real CHKWidgetEnvironment/CHSWidget ObjC class
    // implementations are registered with this process's ObjC runtime
    // before any XPC message might try to decode an instance of them as
    // an incoming argument (getPlaceholdersWithEnvironment:for:/
    // getTimelineFor:into:environment:...). Without this, NSXPCConnection
    // would have no real class to instantiate for those specific
    // arguments and would very likely fail to decode that one message
    // (a contained failure, not expected to crash this process -- but
    // loading the real classes first removes that whole failure mode).
    // Both are ordinary, Apple-signed system frameworks at well-known
    // paths -- not this project's own /b bridge -- so a plain dlopen from
    // any process, including this one, is expected to work the same way
    // it does for any real app that happens to link them.
    @autoreleasepool {
        void *ck = dlopen("/System/Library/PrivateFrameworks/ChronoKit.framework/ChronoKit", RTLD_NOW);
        void *cs = dlopen("/System/Library/PrivateFrameworks/ChronoServices.framework/ChronoServices", RTLD_NOW);
        char msg[128];
        snprintf(msg, sizeof(msg), "main: dlopen ChronoKit=%p ChronoServices=%p", ck, cs);
        WTrace(msg);
    }

    BOOL ready = NO;
    @autoreleasepool {
        ready = SetUpDevice();
        WTrace(ready ? "main: SetUpDevice succeeded"
                     : "main: SetUpDevice FAILED (continuing anyway -- XPC responder doesn't strictly need it)");
    }

    static InfernoWidgetXPCResponder *gResponder;
    @autoreleasepool {
        gResponder = [InfernoWidgetXPCResponder new];

        // Per the file-level comment: StocksWidget.appex/Info.plist's
        // CFBundlePackageType is `XPC!`, matching the public
        // +[NSXPCListener serviceListener] mechanism exactly (the same one
        // Xcode's own "XPC Service" template main.m uses) -- no manual
        // mach-service bootstrap needed on our side.
        NSXPCListener *listener = [NSXPCListener serviceListener];
        if (listener == nil) {
            WTrace("main: [NSXPCListener serviceListener] returned nil -- this process's launch context is not XPC-service-shaped; falling back to an anonymous listener has no host to connect to it, so this is a hard stop for the XPC path (render loop below still runs as a liveness signal)");
        } else {
            listener.delegate = gResponder;
            [listener resume];
            WTrace("main: NSXPCListener serviceListener resumed, delegate set");
        }
    }

    // Periodic render tick (same purpose as inferno_widget_host_main.m's
    // per-second render: an ongoing, timestamped, independently-legible
    // proof this process is alive and doing real GPU work for as long as
    // it survives) plus a coarse idle heartbeat every 10s either way.
    dispatch_source_t timer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0,
                                                       dispatch_get_main_queue());
    dispatch_source_set_timer(timer, dispatch_time(DISPATCH_TIME_NOW, 0),
                               1 * NSEC_PER_SEC, 0);
    dispatch_source_set_event_handler(timer, ^{
        @autoreleasepool {
            if (ready) {
                RenderOneFrame();
            } else if (gFrameIndex % 10 == 0) {
                char msg[64];
                snprintf(msg, sizeof(msg), "main: idle tick %lu, still alive", gFrameIndex);
                WTrace(msg);
                gFrameIndex++;
            }
        }
    });
    dispatch_resume(timer);

    WTrace("main: entering dispatch_main()");
    dispatch_main(); // never returns
    return 0;         // unreachable
}
