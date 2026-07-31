// Deterministic, on-demand trigger for MapKit's own internal
// MTLCreateSystemDefaultDevice() call, without depending on SpringBoard's
// Today-View widget scheduling (already proven non-deterministic
// boot-to-boot, see PROJECT_STATUS.md's MapKit /b investigation) or
// guessing at in-app UI gestures (already tried against Maps.app itself --
// launching the full app to its default view never touches
// MTLCreateSystemDefaultDevice() at all, per that section's dated
// "active GUI-tap trigger tried against Maps.app itself" update). Both
// historical dmesg hits (`Sandbox: com.apple.MapKit(NNN) deny(1)
// file-read-{metadata,data} /b`) came specifically from
// com.apple.MapKit.SnapshotService.xpc, the distinct, on-demand XPC service
// MapKit spins up to render a map *snapshot image* (e.g. for the
// Today-View widget). MKMapSnapshotter is the real, public, documented
// Apple API that triggers exactly that same XPC service/rendering path on
// demand, deterministically, from any calling process -- this test calls
// it directly instead of waiting on/guessing at whatever triggers it
// indirectly.
//
// This does NOT dlopen("/b")/dlsym("Q") itself and does NOT call
// MTLCreateSystemDefaultDevice() directly -- it only asks MapKit to render
// a snapshot. If MapKit's own internal rendering backend goes on to call
// MTLCreateSystemDefaultDevice() as part of servicing this request (as the
// two historical dmesg hits already proved happens for the widget's own
// snapshot refresh cycle), our already-deployed
// ___MTLCreateSystemDefaultDevice_block_invoke patch (patch_block_invoke.py)
// is what's actually being exercised here, from a completely independent,
// deterministic trigger -- this is the whole point of this test.
//
// Delivery-queue design note: startWithCompletionHandler: (no explicit
// queue) delivers on the main queue, per Apple's docs -- blocking the
// calling/main thread on a semaphore while ALSO expecting the completion
// block to run on that exact same (blocked, non-run-loop-pumped) thread's
// queue would deadlock, since this binary has no run loop pump (no
// CFRunLoopRun()/dispatch_main(), same plain-C-main() idiom as the rest of
// this project's test suite). Sidestepped by using startWithQueue: with an
// explicit GCD *global concurrent* queue instead -- serviced by GCD's own
// thread pool independently of any run loop, so the completion handler can
// fire and signal the semaphore regardless of what the main thread is
// doing while it waits. This is a real, correct, commonly-used pattern for
// bridging async Apple APIs into plain CLI/test binaries, not a hack
// specific to this project.
//
// Plain C main(), ARC, MTrace()-style file logging (identical idiom to
// agx_system_metal_test.m's MTrace helper) since this call can plausibly be
// killed with zero stdout output (e.g. by a still-unpatched sandbox-deny
// gate -- exactly what this whole investigation is chasing) -- bracket
// every stage in /tmp/mapkit_test.log so there's execution-progress
// evidence even if dmesg/breakpoints show nothing.
#import <Foundation/Foundation.h>
#import <MapKit/MapKit.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>

static void MTrace(const char *msg)
{
    int fd = open("/tmp/mapkit_test.log", O_WRONLY | O_CREAT | O_APPEND, 0666);
    if (fd < 0) {
        return;
    }
    write(fd, msg, strlen(msg));
    write(fd, "\n", 1);
    close(fd);
}

int main(void)
{
    MTrace("main: start");
    printf("mapkit_snapshotter_test: start\n");

    @autoreleasepool {
        MTrace("main: building MKMapSnapshotOptions (plain struct literals, no CL/CG helper-fn calls)");

        // Plain-old-data struct literals throughout (CLLocationCoordinate2D/
        // MKCoordinateSpan/MKCoordinateRegion/CGSize are just structs) --
        // deliberately avoids calling CLLocationCoordinate2DMake/
        // MKCoordinateRegionMake/CGSizeMake so this binary doesn't need an
        // explicit -framework CoreLocation/CoreGraphics link, keeping the
        // link line minimal (-framework Foundation -framework MapKit only),
        // matching this task's own instruction to use that exact shape.
        CLLocationCoordinate2D center;
        center.latitude = 37.3349;
        center.longitude = -122.0090; // arbitrary real coordinate (Cupertino area)
        MKCoordinateSpan span;
        span.latitudeDelta = 0.05;
        span.longitudeDelta = 0.05;
        MKCoordinateRegion region;
        region.center = center;
        region.span = span;

        MKMapSnapshotOptions *options = [[MKMapSnapshotOptions alloc] init];
        options.region = region;
        CGSize size;
        size.width = 256;
        size.height = 256;
        options.size = size;
        options.scale = 1.0;
        MTrace("main: options configured (region+size+scale)");
        printf("mapkit_snapshotter_test: options configured\n");

        MKMapSnapshotter *snapshotter = [[MKMapSnapshotter alloc] initWithOptions:options];
        MTrace("main: MKMapSnapshotter constructed");
        if (snapshotter == nil) {
            MTrace("main: MKMapSnapshotter alloc/initWithOptions: returned nil, aborting");
            printf("FAIL: MKMapSnapshotter initWithOptions: returned nil\n");
            printf("MAPKIT SNAPSHOTTER TEST DONE\n");
            return 1;
        }

        dispatch_semaphore_t sema = dispatch_semaphore_create(0);
        __block BOOL succeeded = NO;
        __block BOOL handlerRan = NO;
        __block NSString *errDesc = nil;

        dispatch_queue_t bgQueue = dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0);

        MTrace("main: calling startWithQueue:completionHandler: (background GCD global queue, avoids main-queue deadlock)");
        printf("mapkit_snapshotter_test: calling startWithQueue:completionHandler:\n");
        [snapshotter startWithQueue:bgQueue completionHandler:^(MKMapSnapshot * _Nullable snapshot, NSError * _Nullable error) {
            MTrace("completion: handler invoked");
            handlerRan = YES;
            if (error != nil) {
                errDesc = [error localizedDescription];
                const char *edesc = [[NSString stringWithFormat:@"completion: error = %@", error] UTF8String];
                MTrace(edesc ? edesc : "completion: error (description unavailable)");
            } else if (snapshot != nil) {
                succeeded = YES;
                // Deliberately not touching snapshot.image (UIImage) here --
                // its interface isn't explicitly imported (kept the link
                // line minimal, -framework Foundation -framework MapKit
                // only, per this task's instructions) and a bare non-nil
                // MKMapSnapshot is already sufficient proof the snapshot
                // pipeline ran to completion; the point of this test is
                // reaching MTLCreateSystemDefaultDevice(), not inspecting
                // the resulting image.
                MTrace("completion: snapshot != nil, succeeded = YES");
            } else {
                MTrace("completion: both snapshot and error are nil (unexpected)");
            }
            dispatch_semaphore_signal(sema);
        }];
        MTrace("main: startWithQueue:completionHandler: call returned (async -- waiting on semaphore now)");
        printf("mapkit_snapshotter_test: waiting on completion (up to 120s)\n");

        // Bounded wait so the process always exits on its own even if the
        // completion handler never fires (e.g. this test's own trigger
        // hangs the same way the widget's snapshot refresh's real-world
        // cadence is loosely ~31 minutes apart per PROJECT_STATUS.md --
        // 120s is generous for a direct API call, which shouldn't need
        // anywhere near that, but bounded is safer than unbounded).
        dispatch_time_t timeout = dispatch_time(DISPATCH_TIME_NOW, (int64_t)(120 * NSEC_PER_SEC));
        long waitResult = dispatch_semaphore_wait(sema, timeout);

        if (waitResult != 0) {
            MTrace("main: TIMEOUT waiting for completion handler (120s elapsed, handler never fired)");
            printf("TIMEOUT: completion handler never fired within 120s (handlerRan=%d)\n", handlerRan);
        } else if (succeeded) {
            MTrace("main: semaphore signaled, completion handler ran, SUCCEEDED");
            printf("SNAPSHOT SUCCEEDED\n");
        } else {
            MTrace("main: semaphore signaled, completion handler ran, FAILED");
            printf("SNAPSHOT FAILED: %s\n", errDesc ? [errDesc UTF8String] : "(no error description)");
        }
    }

    MTrace("main: end of @autoreleasepool, returning");
    printf("MAPKIT SNAPSHOTTER TEST DONE\n");
    return 0;
}
