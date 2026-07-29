// Minimal probe: does Metal's generic plugin loader find and instantiate
// InfernoVGPUMetalDevice from /System/Library/Extensions/InfernoVGPUMetal.bundle?
// If it does, MTLCopyAllDevices() (or MTLCreateSystemDefaultDevice()) should
// include/return an object whose -name responds "Inferno VGPU".
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

int main(void)
{
    @autoreleasepool {
        // MTLCopyAllDevices() is macOS/macCatalyst-only (API_UNAVAILABLE(ios)
        // in the real SDK header) -- MTLCreateSystemDefaultDevice() is the
        // only enumeration entry point on iOS.
        id<MTLDevice> def = MTLCreateSystemDefaultDevice();
        if (def == nil) {
            printf("MTLCreateSystemDefaultDevice() -> nil\n");
        } else {
            printf("MTLCreateSystemDefaultDevice() -> %s\n", def.name.UTF8String);
        }
    }
    return 0;
}
