#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

// Conforms to MTLDevice + MTLDeviceSPI (see MTLDevice.txt / MTLDeviceSPI.txt
// in this directory, dumped from this project's own guest's real
// dyld_shared_cache_arm64e via `ipsw class-dump`). Most of the protocol
// surface is implemented in the generated file (InfernoVGPUMetalDevice_generated.m,
// see gen_stub.py) as safe zero/NO/nil stubs; the handful of methods that
// matter for the device to identify itself sanely to callers are
// hand-written here instead (see REAL_OVERRIDES in gen_stub.py for the
// exact list this file must cover).
@interface InfernoVGPUMetalDevice : NSObject <MTLDevice>

// InfernoVGPUHello's real IOService connection (IOServiceOpen'd once, at
// +[InfernoVGPUMetalDevice registerDevices] / -init time) -- not yet wired
// up to any actual command submission; present so the class already has
// the field real GPU work will need.
@property (nonatomic, assign) io_connect_t vgpuConnection;

@end
