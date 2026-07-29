// Standalone harness: dlopen's the deployed inferno_agx_bridge.dylib and
// calls InfernoGetAGXDevice() exactly the way the eventual block_invoke
// machine-code patch will, so the bridge dylib itself is validated before
// ever touching the dyld_shared_cache patch.
#import <Foundation/Foundation.h>
#import <dlfcn.h>

int main(void)
{
    void *handle = dlopen("/b", RTLD_NOW);
    if (handle == NULL) {
        printf("dlopen failed: %s\n", dlerror());
        return 1;
    }
    void *(*fn)(void) = (void *(*)(void))dlsym(handle, "Q");
    if (fn == NULL) {
        printf("dlsym failed: %s\n", dlerror());
        return 1;
    }
    void *device = fn();
    printf("Q() -> %p\n", device);
    if (device == NULL) {
        return 1;
    }
    id obj = (__bridge id)device;
    NSLog(@"device class: %@", NSStringFromClass([obj class]));
    return 0;
}
