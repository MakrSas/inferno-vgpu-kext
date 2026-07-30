# Inferno GPU/Metal project — status and playbook

Written 2026-07-30, for continuity across context resets (weekly usage limit /
account switch). If you're picking this up cold: read this whole file before
touching anything. It tells you what's done, what's proven, what's broken,
and the exact commands to keep going.

## The big picture goal

[Inferno](https://github.com/ChefKissInc/Inferno) is a QEMU fork that boots a
real iOS 14 (iPhone 11 / T8030) kernelcache. It has **zero real GPU support**
out of the box (`sgx`/AGX register reads all return `0xFFFFFFFF`). The goal
of this whole side-project (tracked in this repo, `inferno-vgpu-kext`) is:
**get real, standard Metal API calls in the guest to actually execute on
real GPU hardware (this host's AMD Radeon, via Vulkan) — and eventually get
that rendered output to show up as the guest's actual on-screen interface**,
not just an isolated test-harness readback.

## Where things stand right now (2026-07-30)

**Fully proven, working, verified on the actual guest:**
- Real Metal **compute** pipeline: device → library → function → pipeline
  state → queue → command buffer → compute encoder → dispatch → commit,
  driving real AIR→SPIR-V translation (`metal2vulkan`) and real GPU compute
  execution (`reims-vgpu`'s Vulkan engine), correct results, no crashes.
- Real Metal **render/draw** pipeline: same shape, ending in a correctly
  rasterized triangle read back via `getBytes` (off-screen texture only,
  not on-screen yet — see below).
- The **system-wide patch**: `___MTLCreateSystemDefaultDevice_block_invoke`
  inside the guest's own `dyld_shared_cache_arm64e` is hand-patched (raw
  ARM64 machine code, see `patch_block_invoke.py`) to redirect to our own
  bridge dylib (`/b` on the guest, built from `inferno_agx_bridge.m` +
  `inferno_command_queue.m` + `inferno_render_encoder.m`). Confirmed the
  patch bytes are still intact in the guest's DSC. **This means ANY real,
  unmodified app calling the standard public `MTLCreateSystemDefaultDevice()`
  gets our full device** — no dlopen tricks needed on the caller's side.
  `agx_system_metal_test.m` is the test proving this (written, built,
  **not yet live-verified** — blocked by the SIGKILL issue below until the
  bash-builtin workaround, which itself hasn't been tried against this
  specific test yet either).

**CONFIRMED WORKING, live, visually verified via QEMU screendump (2026-07-30):**
- `INFERNO_VGPU_OP_PRESENT` renders and blits a real triangle (AIR→SPIR-V via
  `metal2vulkan` → real GPU draw via `reims-vgpu` → RGBA→display-format
  convert → `adp_v4_present_frame()`) directly into the guest's LIVE display
  genpipe buffer — the actual framebuffer the real iOS display driver scans
  out. **Screendump shows the red triangle rendered on top of the Apple boot
  logo, on the actual emulated iPhone screen.** This is the first concrete
  proof that Metal-rendered pixels can reach the real screen in this project.
  - Trigger path: kernel-context only (bypasses the SIGKILL/sandbox maze
    below entirely) — `InfernoVGPUHello::start()` spawns a detached kernel
    thread (`kernel_thread_start`/`presentRetryThreadMain` in
    `InfernoVGPUHello.cpp`) that retries `submitBootPresentDispatch()` every
    3s (up to 100x/5min) until the display genpipe becomes active (~24-70
    attempts observed in practice, i.e. well under a minute once boot
    reaches that point), then switches to presenting every 1s **forever**
    (does not stop) — required because a one-shot present gets overwritten
    by the very next frame the real display driver draws, so without
    continuous re-presenting the frame is visible for at most one frame and
    isn't reliably catchable in an externally-timed screendump.
  - This is NOT the real app-facing path — it's a fixed pair of hardcoded
    test shaders (`vertex_passthrough`/`fragment_solid_red`), driven
    entirely from kernel context, with no involvement of the real Metal
    framework, CAMetalLayer, IOSurface, or WindowServer compositing. See
    "Not started" below for what's still needed for real system-wide usage
    (arbitrary apps' actual Metal calls, blur effects, etc.).
  - Gotcha hit getting here: `resolve.py`/`patch_kernelcache.py`'s `KC`
    path pointed at a long-gone prior session's `/tmp` scratchpad; silently
    fell back to the wrong (IMG4-compressed) file when overridden naively,
    producing garbage relocation targets with no clear error. Fixed: the
    working decompressed kernelcache now lives at the durable
    `InfernoData/kernelcache.decompressed` (both scripts default to it),
    and `resolve.py` asserts the Mach-O magic byte up front.
  - Gotcha: QEMU was observed to die silently (no crash/panic in its own
    log, process just vanished) during a long unattended background wait
    once this session — cause unconfirmed (possibly host/session idle
    handling), not a kernelcache/guest-side bug. If a boot seems stuck with
    the present-dispatch attempt count frozen, check `ps aux | grep
    qemu-system` before assuming a guest-side hang.

**Not started / explicitly out of scope so far:**
- Real `.metallib` binary container parsing (only raw AIR `.ll` text
  supported).
- Multiple vertex attributes/buffers, textures/samplers in a draw, depth/
  stencil, blending, multiple draws per encoder. `reims-vgpu`'s underlying
  engine supports essentially all of this — the gap is purely in this
  project's own wire format / ObjC encoder classes.
- **Getting the REAL system compositor (WindowServer/backboardd) to
  actually drive Metal rendering for the real interface.** This is a much
  bigger, still-unscoped task. Key finding: on macOS, `reims-vgpu` achieves
  full real desktop compositing because macOS *ships a real client driver*
  (`AppleParavirtGPU.kext`) for that exact paravirt protocol. **iOS has no
  such driver at all** — real iPhones are never paravirtualized, so Apple
  never had a reason to write one. That's why this whole project takes the
  "hand-patch Metal.framework + custom kext" shortcut instead of implementing
  a real protocol. Getting the actual interface to render through Metal
  would need either (a) confirming `backboardd`/`WindowServer` even attempts
  to use Metal in this build (unlikely — it probably already fell back to
  software compositing given AGX has never worked), or (b) building out our
  `AGXPrincipalDevice` fallback layer's `MTLDeviceSPI` conformance far
  enough (~523 private methods beyond the 112 public `MTLDevice` ones) that
  it could actually be trusted for real compositing. Not attempted yet.

## CRITICAL: the SIGKILL mystery and its workaround

**Every freshly-transferred, unsigned MAIN EXECUTABLE binary on the guest
gets `Killed: 9` instantly (sub-second), with ZERO output**, regardless of
content (MD5-verified correct), file path, kernelcache/dylib version, or
boot freshness. This affects ALL new test binaries built this session,
including ones with logic identical to previously-working tests.

**Root cause NOT found**, despite very extensive live kernel debugging
(QEMU's own gdbstub — see `guest_tools/gdb_rsp2.py`). What WAS ruled out,
definitively, via live breakpoints on the running kernel:
- NOT AMFI/code-signing rejection — `mac_vnode_check_signature` (the actual
  gate) returns 0 (allowed) for our binaries.
- NOT a userspace `kill()` syscall from another process (runningboardd or
  otherwise) — breakpointed `_kill` itself, 601 calls observed, all
  unrelated noise (`signum=0` existence probes), none targeting our process.
- NOT `psignal`/`psignal_locked`/`cs_invalid_page`/`memorystatus_kill_proc`/
  `proc_exit` called by name — none of these breakpoints ever fired for this
  kill.
- IS delivered via the normal signal path (confirmed: breakpoint on
  `exit_with_reason`, called from `postsig_locked`, catches it — `x1=9`
  (SIGKILL), `x2=NULL` (no structured `os_reason`, unlike real AMFI/jetsam
  kills which normally attach one)).
- Conclusion: the SIGKILL bit is being set by inlined kernel code with no
  catchable named-symbol call site — would need a hardware watchpoint on the
  live process's actual `proc_t.p_siglist` field to find it, which needs an
  exact byte offset that couldn't be reliably computed by hand (see XNU
  source notes below). **Not pursued further — deprioritized in favor of
  the actual goal.**
- Also ruled out: SEP state corruption (tried resetting `sep_nvram`/
  `sep_ssc` to blank per the official setup guide's method — this actually
  **crashed the whole QEMU process**, not just the guest; reverted from
  backup, confirmed working again — **do not retry blind SEP resets on an
  already-installed system**, blank init only works at true first-boot).

**THE WORKAROUND (use this for all new guest-side test logic going
forward):** the kill only affects **new process exec()**, not **`dlopen()`
of an unsigned dylib from an already-running, already-trusted process**.
Proven directly: `enable -f /b someRandomName` from the interactive root
bash shell (bash's own
[loadable-builtins](https://www.gnu.org/software/bash/manual/html_node/Loadable-Builtins.html)
mechanism) — bash **survives**, the dlopen() genuinely succeeds (gets as far
as a normal `dlsym(...): symbol not found` error), no kill at all.

So: **package new test/trigger logic as a bash loadable builtin (a
`-dynamiclib` implementing bash's `struct builtin` ABI), not a standalone
executable.** See `src/userspace_test/bash_present_builtin.m` for a working
example (hand-declares bash's `struct builtin` since bash's own headers
aren't available in this cross-compile environment):

```objc
struct builtin {
    char *name;
    int (*function)(void *);
    int flags;              // 1 = BUILTIN_ENABLED
    char * const *long_doc; // NULL-terminated array of strings
    char *short_doc;
    char *handle;           // must be NULL
};
int my_thing_builtin(void *list) { /* real logic here */ return 0; }
char *my_thing_doc[] = { "docstring", (char *)NULL };
struct builtin my_thing_struct = {
    "my_thing", my_thing_builtin, 1, my_thing_doc, "my_thing", (char *)NULL,
};
```

Build with `clang -target arm64e-apple-ios14.0 -isysroot "$SDK" -fobjc-arc
-framework Foundation -framework IOKit -dynamiclib -install_name
/usr/lib/whatever.dylib -o whatever.dylib src/....m` (see the
`agx-bridge-dylib` CI job for the exact pattern already wired up). Transfer
to the guest like any other file, then from the guest shell:

```
enable -f /path/to/whatever.dylib my_thing
my_thing            # runs it, exactly like any other bash builtin
```

**Update, same day**: tried this against `bash_present_builtin.m` (real
`INFERNO_VGPU_OP_PRESENT` call) — loading via `enable -f` from `/` (NOT
`/tmp` — see next paragraph) DOES succeed and DOES survive (`ENABLE_RC=0`,
no kill), confirming the workaround itself is sound. But
`inferno_present_builtin`'s own `IOServiceOpen()` call then failed with
`0xe00002e2` (`kIOReturnNotPermitted`). This is a **third, independent**
security layer, distinct from both AMFI/codesigning (already confirmed
passing) and from the process-exec SIGKILL mystery:

1. First hurdle: `enable -f /tmp/whatever.dylib name` fails with `file
   system sandbox blocked mmap() of '/private/var/tmp/whatever.dylib'` —
   **`/tmp` (== `/private/var/tmp`) is sandbox-blocked for executable
   mmap**, but `/` (root) is not. Fix: `cp` the dylib from `/tmp` to `/`
   guest-side (cheap, no re-transfer needed) before `enable -f`.
2. Second hurdle, still unresolved: even loaded from `/`, the actual
   `IOServiceOpen()` call inside the builtin gets denied. `dmesg` shows
   exactly why: `System Policy: bash(31) deny(1) iokit-open IOUserClient` /
   `This must be in your com.apple.security.iokit-user-client-class
   entitlement.` — **`/bin/bash` is a real, signed platform binary with an
   actual enforced sandbox profile** (unlike raw unsigned standalone test
   binaries, which apparently run with no sandbox profile attached at all
   — that asymmetry is *why* this only shows up now, via the bash-builtin
   path, and never did for the standalone-executable tests that got as far
   as `IOServiceOpen` in much earlier sessions).
   - Found the responsible kernel function via `kernel-symbols.txt`:
     `_hook_iokit_check_open` (a Sandbox.kext MACF policy hook,
     `PACIBSP`-prologued real function). Live-patched it in the running
     kernel via GDB (`mov x0, #0; ret` at its entry — same
     always-allow-in-place technique the project's own
     `kernel_patches.c` already uses for AMFI/SEP bypasses) — **this
     requires the user to have Bypass Permissions enabled**, the normal
     auto-mode classifier blocks raw kernel memory patches like this by
     design; ask first if it's not already on.
   - **Patch did NOT fix it.** Confirmed via a live breakpoint that
     `_hook_iokit_check_open` genuinely IS being called (hit fired, PC
     matched exactly) and does return cleanly — yet the exact same deny +
     entitlement message still appeared afterward. So either this isn't
     the actual enforcement point for the entitlement-specific message (a
     different function may own the `com.apple.security.iokit-user-client-
     class` string check specifically — possibly plain IOKit C++ code,
     e.g. `IOUserClient::copyClientEntitlement`/`clientHasPrivilege`, not
     a MAC policy hook at all), or there's a second, independent gate.
     **Not yet found — next session should search for the literal string
     `"com.apple.security.iokit-user-client-class"` in the kernelcache
     (or the log format string around it) to locate the real check.**
   - Operational note: this GDB round left QEMU in a genuinely stuck
     `paused (debug)` loop (breakpoint-removal `z0` returned `E22`
     repeatedly, `cont` immediately re-triggered `STOP`) that a plain QMP
     `cont` could NOT clear. **Only a full QEMU kill+relaunch recovered
     it** — if a future GDB session gets stuck the same way, don't spend
     time trying to un-wedge it via more RSP commands, just restart QEMU
     (cheap, kernelcache file is unaffected, only in-memory patches/state
     are lost).
   - A live in-memory GDB patch like this one **does not survive a QEMU
     restart** — it was lost by the forced restart above and was not yet
     re-applied or made permanent (e.g. baked into the kernelcache file
     the same way `patch_block_invoke.py`/`patch_kernelcache.py` do, or
     added properly to `kernel_patches.c` for a QEMU rebuild) as of this
     writing.

Bottom line: **the bash-loadable-builtin technique itself works and is the
right approach** — what remains is finding the actual `com.apple.security.
iokit-user-client-class` entitlement check and neutralizing that too
(or granting our `InfernoVGPUUserClient` class some other form of
exemption, e.g. an IOKit-side property, which might be more surgical than
another kernel patch — not yet investigated).

## Environment reference

- Working dir: `/home/makr/Documents/Inferno/InfernoData` (QEMU launch
  scripts, disk images, kernelcache, `launch_shell.sh`).
- QEMU fork source: `/home/makr/Documents/Inferno/InfernoData/Inferno` (a
  real git repo, mostly *uncommitted* local changes — that's normal for
  this project, don't be alarmed by `git status` showing a lot).
- This repo (`inferno-vgpu-kext`): the kext/bridge/daemon source, has a
  real `origin` remote (`https://github.com/MakrSas/inferno-vgpu-kext.git`)
  — **GitHub Actions (`macos-14` runners) is the ONLY way to compile
  arm64e-apple-ios/kernel-context Mach-O code** — this host is Linux, no
  local compilation possible for anything guest-side. Push → `gh run list`
  / `gh run view <id> --json status,conclusion` → `gh run download <id> -n
  agx-bridge-dylib -D <dir>` once done.
- Guest serial console: plain TCP to `127.0.0.1:4444` (see
  `guest_tools/shell_cmd.py`). Gives a real interactive root bash shell.
  **After every fresh boot, `/sbin/mount -uw /` must be rerun** before any
  guest-side writes work (silent symptom otherwise: transfers report
  `SIZE MISMATCH: expected N, got None`).
- QMP socket: `InfernoData/shell-qmp.sock` (see `guest_tools/qmp_client.py`)
  — use for `info status`, `cont` (resume a paused VM), `gdbserver
  tcp::1234` (arm the kernel debugger).
- **`/tmp` on the guest is wiped on every reboot** (both a guest-internal
  reboot and a full QEMU relaunch). Files placed directly under `/` (e.g.
  `/b`) survive, since they're on the persistent `root` NVMe disk image.
- **The kernel image is loaded by QEMU ONCE at process start, from the host
  file `kernelcache.vgpu2.patched`.** A guest-internal reboot does **not**
  re-read it — any kernel-side (`InfernoVGPUHello.cpp`) change needs a full
  `kill <qemu-pid>` + relaunch of `launch_shell.sh`, in addition to rerunning
  the resolve/patch pipeline below.
- Guest→host file transfer: chunked `printf '\xHH...' >> file` over the
  serial console, `CHUNK=100` bytes (see `guest_tools/transfer_binary3.py`)
  — proven safe; `CHUNK=150/300` caused silent data corruption in earlier
  testing (a serial TTY line-length limit). Expect roughly 1–1.5 minutes per
  10KB — a 100KB dylib takes ~15–20 minutes. **Never open a second serial
  connection while a transfer is in flight** — disrupts/slows it.
- This exact kernel build's version string (read live via GDB from symbol
  `_version`): `Darwin Kernel Version 20.0.0: Wed Aug 12 22:56:55 PDT 2020;
  root:xnu-7195.0.33~64/RELEASE_ARM64_T8030`. No exact matching tag is
  published on `apple-oss-distributions/xnu`; closest available is
  `xnu-7195.50.7.100.1` (useful for looking up real struct layouts, though
  a few field sizes can't be trusted without compiling against the exact
  build — see the SIGKILL section above).

## Playbook: redeploying a kernel-side change (InfernoVGPUHello.cpp)

1. Edit `src/InfernoVGPUHello.cpp` in this repo, commit, push.
2. `gh run list` → wait for the `iokit-class-probe` job of the new run to
   succeed → `gh run download <id> -n iokit-class-object -D <dir>` (gives
   `InfernoVGPUHello.o`).
3. `cp <dir>/InfernoVGPUHello.o obj/InfernoVGPUHello.o` (in this repo).
4. `python3 parse_obj.py obj/InfernoVGPUHello.o` — regenerates
   `obj_sections.json`/`obj_symtab.json`/`obj_relocs.json`.
5. `python3 resolve.py` — `KC` now defaults (in `resolve.py` itself) to
   `/home/makr/Documents/Inferno/InfernoData/kernelcache.decompressed`, a
   durable copy kept in the project's own data directory specifically so
   it survives session/scratchpad resets (an earlier copy that only lived
   under `/tmp/claude-*/.../scratchpad/` was lost this way once already —
   don't repeat that, always keep the working copy under `InfernoData/`,
   never scratch-only). Only pass `KC=...` to override. Should print `OK:
   wrote resolved_blob.bin, N bytes, all M relocations resolved` and `18
   call site(s) fixed, 0 unrecognized`. `resolve.py` asserts the file's
   magic is `0xfeedfacf` (decompressed Mach-O) up front — if that assert
   fires, you pointed it at the raw IMG4 file from `InfernoData/Restore/`
   by mistake (magic `IM4P`), which otherwise fails silently deep inside
   vtable-slot resolution with bogus huge file offsets instead of a clear
   error.
   **If `InfernoData/kernelcache.decompressed` is gone**, you'll need to
   re-extract the pristine kernelcache per the
   [chefkiss.dev setup guide](https://chefkiss.dev/guides/inferno/file-setup/)
   (`img4lib`, SEP ticket, etc.) — this file is a plain Mach-O (magic
   `0xfeedfacf`), decompressed, unpatched. Save the result straight to
   `InfernoData/kernelcache.decompressed`.
6. `python3 patch_kernelcache.py` — writes
   `/home/makr/Documents/Inferno/InfernoData/kernelcache.vgpu2.patched`.
7. Kill the running QEMU process (`ps aux | grep qemu-system`, `kill
   <pid>`), relaunch via `cd InfernoData && nohup ./launch_shell.sh >
   launch_shell_stdout<NEXT_LETTER>.log 2>&1 & disown`.
8. Wait for boot (watch the log for `RTKit boot done`, then just try
   connecting via `guest_tools/shell_cmd.py` — the shell often comes up
   readable even before that line, and the line alone doesn't guarantee
   full boot either — just try it).
9. `/sbin/mount -uw /` on the guest before any writes.

## Playbook: redeploying the userspace bridge (`/b`)

1. Edit `inferno_agx_bridge.m` / `inferno_command_queue.m` /
   `inferno_render_encoder.m`, commit, push.
2. Wait for the `agx-bridge-dylib` CI job, download the `agx-bridge-dylib`
   artifact — contains `inferno_agx_bridge.dylib` plus all the test
   binaries/builtins from that job.
3. `python3 guest_tools/transfer_binary3.py <path>/inferno_agx_bridge.dylib
   /b` — no kernel/QEMU restart needed, `/b` is dlopen'd fresh every time
   (by the block_invoke patch AND by anything else that dlopens it).

## Playbook: running new test logic (use the bash-builtin pattern!)

1. Write a new `.m` file implementing bash's `struct builtin` ABI (see
   `src/userspace_test/bash_present_builtin.m`).
2. Add a CI step compiling it as `-dynamiclib` (see the `agx-bridge-dylib`
   job in `.github/workflows/build.yml` for the pattern).
3. Transfer the resulting `.dylib` to the guest (any path, `/tmp` is fine
   since it only needs to survive until you load it, not across reboots).
4. On the guest: `enable -f /tmp/whatever.dylib my_builtin_name` then just
   run `my_builtin_name`.

## `guest_tools/` scripts in this repo

- `shell_cmd.py <cmd> [idle_s] [total_deadline_s]` — send one command over
  the serial console, print whatever comes back.
- `transfer_binary3.py <local_path> <remote_path>` — chunked transfer,
  CHUNK=100, verifies size on completion.
- `qmp_client.py '<hmp command>'` — run one HMP command via QMP (`info
  status`, `cont`, `gdbserver tcp::1234`, etc).
- `gdb_rsp2.py` — minimal hand-rolled GDB Remote Serial Protocol client
  (no system gdb with aarch64 support was available/installable — no sudo
  password). Import `RSP` and `qmp_cont` from it for ad-hoc kernel
  debugging; **always wrap usage in try/finally calling `qmp_cont()`** — a
  dangling paused VM from a crashed/interrupted debug script is a real
  failure mode that happened twice this session.
