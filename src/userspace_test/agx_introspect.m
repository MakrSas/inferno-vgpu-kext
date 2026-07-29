// Enumerates the real, live method list of the AGXPrincipalDevice instance
// our bridge constructs (see inferno_agx_bridge.m / project memory) via the
// ObjC runtime directly -- faster and more authoritative than static
// class-dump for a class whose metadata ipsw's dumper doesn't fully surface.
#import <Foundation/Foundation.h>
#import <dlfcn.h>
#import <objc/runtime.h>

int main(void)
{
    @autoreleasepool {
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
        void *raw = fn();
        if (raw == NULL) {
            printf("Q() -> nil\n");
            return 1;
        }
        id device = (__bridge id)raw;
        Class cls = object_getClass(device);
        printf("instance class: %s\n", class_getName(cls));

        unsigned int count = 0;
        Class walk = cls;
        while (walk != Nil) {
            Method *methods = class_copyMethodList(walk, &count);
            printf("-- %s (%u methods) --\n", class_getName(walk), count);
            for (unsigned int i = 0; i < count; i++) {
                SEL sel = method_getName(methods[i]);
                printf("  %s\n", sel_getName(sel));
            }
            if (methods) free(methods);
            walk = class_getSuperclass(walk);
        }

        printf("respondsToSelector(name) = %d\n", [device respondsToSelector:@selector(name)]);
        printf("respondsToSelector(registryID) = %d\n", [device respondsToSelector:@selector(registryID)]);
        printf("respondsToSelector(newCommandQueue) = %d\n", [device respondsToSelector:@selector(newCommandQueue)]);
    }
    return 0;
}
