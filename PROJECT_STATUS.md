# Inferno GPU/Metal project — status and playbook

Status and technical writeup, last updated 2026-07-30. Covers what's done,
what's proven, what's broken, and the exact commands to reproduce or
continue the work.

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
    path pointed at a stale `/tmp` scratchpad location; silently fell back
    to the wrong (IMG4-compressed) file when overridden naively, producing
    garbage relocation targets with no clear error. Fixed: the working
    decompressed kernelcache now lives at the durable
    `InfernoData/kernelcache.decompressed` (both scripts default to it),
    and `resolve.py` asserts the Mach-O magic byte up front.
  - Gotcha: QEMU was observed to die silently (no crash/panic in its own
    log, process just vanished) during a long unattended background wait —
    cause unconfirmed (possibly host idle handling), not a kernelcache/
    guest-side bug. If a boot seems stuck with the present-dispatch attempt
    count frozen, check `ps aux | grep qemu-system` before assuming a
    guest-side hang.

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
boot freshness. This affects every new test binary, including ones with
logic identical to previously-working tests.

### UPDATE 2026-07-30: root cause #1 FOUND, confirmed, and live-patched

**The earlier "x2=NULL, no os_reason" note above was WRONG** — it was
reading the wrong register. The real `exit_with_reason` signature (from
`apple-oss-distributions/xnu` tag `xnu-7195.50.7.100.1`,
`bsd/sys/proc_internal.h`) is `exit_with_reason(struct proc *p, int rv,
int *retval, boolean_t thread_can_terminate, boolean_t proc_transiting,
int fd_before_close_count, struct os_reason *exit_reason)` — the
`os_reason_t` is **x6**, not x2, and x6 is **NOT NULL**. Dumping the
struct at x6 (fields per `bsd/sys/reason.h`: `osr_lock` (opaque
`lck_mtx_t`, empirically 16 bytes on this build), `osr_refcount` (u32),
`osr_namespace` (u32), `osr_code` (u64), `osr_flags` (u64), `osr_bufsize`
(u32), ...) decoded cleanly as:
- `osr_namespace = 9` = `OS_REASON_EXEC`
- `osr_code = 1` = `EXEC_EXIT_REASON_BAD_MACHO`
- `osr_flags = 0x40` = `OS_REASON_FLAG_CONSISTENT_FAILURE`

This immediately proved the kill is **not** a raw inlined signal-bit OR at
all — it's a deliberate, structured kill from the kernel's own **Mach-O
image-activation/exec path**, not the codesigning/sandbox/IOKit stack.

**Traced to the exact function and instruction.** Used a two-phase
technique throughout (breakpoint a callee's entry to capture its return
address via LR/x30, then breakpoint that return address to read the real
return value in x0) — this reliably finds call sites and their return
values without needing a real disassembler.

Concretely: `kern_exec.c`'s `exec_mach_imgact()` calls `load_machfile()`
(`bsd/kern/mach_loader.c`), which calls `parse_machfile()`. Breakpointing
`_parse_machfile`'s own return address inside `load_machfile`
(`0xfffffff007eef788`, confirmed via the LR-capture trick) showed **`x0 =
4` = `LOAD_FAILURE`** for `/sigkill_test`, specifically and reproducibly —
verified against a **clean baseline**: 200+ background `load_machfile`
calls from normal system activity over 37s, zero of which ever returned
nonzero. `parse_machfile()`'s source (matching `mach_loader.c` from the
same xnu tag) has exactly one `LOAD_FAILURE` site that fits, right after
its main load-command-processing loop:

```c
if (ret == LOAD_SUCCESS) {
    if (!got_code_signatures && cs_process_global_enforcement()) {
        ret = LOAD_FAILURE;
    }
    ...
}
```

`/sigkill_test` (and every other bare unsigned test binary built by this
project's CI, e.g. via plain `clang -target arm64e-apple-ios14.0 ... -o
out`, no `codesign` step) has **no `LC_CODE_SIGNATURE` load command at
all** (confirmed by manually parsing `inferno_vgpu_test_binary`'s load
commands, and cross-checked against the exact bytes the kernel reads at
its `load_machfile()` breakpoint — bit-for-bit match: `magic=0xfeedfacf
cputype=0x0100000c(ARM64) cpusubtype=0x80000002(ARM64E) filetype=2
ncmds=19 sizeofcmds=1328 flags=0x200085(NOUNDEFS|DYLDLINK|TWOLEVEL|PIE)`,
has proper `LC_MAIN` at `entryoff=0x4000` inside a valid R+X `__TEXT`, so
none of the *other* ~35 `LOAD_FAILURE`/`LOAD_BADMACHO` sites in
`mach_loader.c` apply). So `got_code_signatures` stays `FALSE` for the
whole function, and `cs_process_global_enforcement()` — **confirmed via
live memory read to be a hardcoded `mov w0,#1; ret` stub** (i.e. always
returns true on this build) — makes the check unconditionally fail. This
is why the kill is instant, silent, and 100% content-independent: it
never even gets to reading the binary's actual TEXT, just its load
commands.

**Why `dlopen()` was immune (explains the existing workaround below):**
`dlopen()` is 100% userspace (dyld parsing the Mach-O itself) and never
calls the kernel's `load_machfile()`/`parse_machfile()` at all — only
`execve()`-driven process activation does. No new/separate "third
mechanism" needed to explain this; it falls straight out of the finding.

**Confirmed NOT the cause** (unchanged from before, still valid): AMFI's
`mac_vnode_check_signature` MACF hook (returns 0/allowed — it's a
*different*, pluggable policy layer, not this hardcoded structural
check), a userspace `kill()`, and `psignal`/`cs_invalid_page`/
`memorystatus_kill_proc`/`proc_exit` called by name.

**Live-patched and verified end-to-end.** Two GDB patches applied this
session (both **in-memory only, do NOT survive a QEMU restart** — see
"Environment reference" below for why):
1. `_cs_process_global_enforcement` (`0xfffffff007e3f914`): original bytes
   `mov w0,#1; ret` → patched to `mov w0,#0; ret`. **Turned out to be a
   no-op for this specific bug** — confirmed via a dedicated breakpoint
   that the standalone symbol is **never actually called** during a
   triggered exec (0 hits across 10 trigger attempts over 60s watching
   its entry address), meaning the compiler constant-folded the trivial
   `return 1;` body directly into each of its ~4 call sites at compile
   time (this kernel is presumably built with LTO/whole-module
   optimization). Harmless to leave patched (correct in intent for any
   non-inlined callers elsewhere), but not the effective fix.
2. **The actual effective fix**: found the real compiled branch by
   dumping and hand-disassembling the instructions around
   `load_machfile`'s `parse_machfile` call site (wrote a minimal ARM64
   disassembler, `/tmp/.../scratchpad/mini_disasm.py`, covering
   MOVZ/MOVN/MOVK, CBZ/CBNZ, TBZ/TBNZ, B/BL/B.cond, RET, LDR/STR/LDP/STP,
   ADD/SUB/CMP imm, ORR/MOV reg — enough to read compiler-generated
   control flow without a real disassembler on this Linux host). Found
   exactly:
   ```
   0xfffffff007eef784: bl   _parse_machfile
   0xfffffff007eef788: cbz  w0, 0xfffffff007eef79c   ; if success, skip failure path
   0xfffffff007eef78c: mov  x27, x0                   ; (failure path)
   0xfffffff007eef790: mov  x0, x28
   0xfffffff007eef794: bl   vm_map_deallocate
   0xfffffff007eef798: b    0xfffffff007eef85c        ; return lret
   0xfffffff007eef79c: ...                            ; success path continues
   ```
   Patched the `cbz w0, 0xfffffff007eef79c` (bytes `a0 00 00 34`) at
   `0xfffffff007eef788` into an **unconditional branch to the exact same
   target**, `b 0xfffffff007eef79c` (bytes `05 00 00 14`) — i.e. `load_
   machfile` now always takes the "parse succeeded" continuation,
   regardless of what `parse_machfile()` actually returned. (Note: my
   first instinct — overwrite with `mov w0,#0` — would have been WRONG,
   since that address holds a *branch* instruction, not a value consumer;
   always disassemble/verify before patching a conditional branch, don't
   assume the "obvious" register-clearing patch applies.)
   - **Verified `/sigkill_test`'s original failure mode (instant silent
     `Killed: 9`, zero output) is GONE** after this patch.
   - System stability double-checked afterward: `uptime`, `ps aux` (174
     procs), `dmesg` all normal, no panics — the patch is narrowly scoped
     (only changes behavior for execs where `parse_machfile` would
     otherwise have failed *after* its main loop, i.e. after segments/
     entry-point/thread-state were already validly set up, so it's safe
     even though broader than the ideal minimal fix — see caveat below).

**Caveat / next step for a permanent fix:** this patch is **broader than
ideal** — it makes `load_machfile` ignore *any* `parse_machfile` failure
reaching that return point, not just the specific `got_code_signatures`
gate. The exact internal branch instruction that tests `got_code_
signatures` itself (inside `parse_machfile`'s own ~0x1db8-byte compiled
body, `0xfffffff007eef880`–`0xfffffff007ef1638`) was not pinpointed within
this session's time budget — the tail of the function interleaves with an
inlined recursive dylinker-loading path (`parse_machfile` calling itself
for `LC_LOAD_DYLINKER`, also inlined) that made isolating the one check
from its neighbors nontrivial by hand. A future pass should either finish
that disassembly (the dumped bytes are saved at
`/tmp/.../scratchpad/parse_machfile_tail.bin`, offset `0xfffffff007ef1200`)
or accept the current caller-side patch as adequate (it only ever matters
for genuinely-failing execs, which in this project's controlled test
scenario is exactly what we want to allow through).

### UPDATE, same investigation: a SECOND, independent gate exists

After patch #1 above, `/sigkill_test` **no longer fails silently** — it
now fails with an explicit, visible kernel log line first:

```
AMFI: hook..execve() killing pid 465: dyld signature cannot be verified.
You either have a corrupt system image or are trying to run an unsigned
application outside of a supported development configuration.
Killed: 9
```

(still `Killed: 9` / exit 137, but now with a real diagnostic — a huge
improvement for whoever debugs this next). This is a genuinely **separate,
independent** code-signature enforcement layer from the `parse_machfile`/
`cs_process_global_enforcement` gate just fixed, living in
`AppleMobileFileIntegrity.kext` (confirmed the exact string exists once,
literally, in the kernelcache via `strings kernelcache.decompressed |
grep "dyld signature cannot be verified"`; found many neighboring
`AppleMobileFileIntegrity::*` C++ symbols in `kernel-symbols.txt`, e.g.
`__ZN24AppleMobileFileIntegrity17validateSignatureE...`, but did **not**
find an exact symbol match for the emitting function itself within this
session's time budget — the literal C function name behind the "hook..
execve()" log text wasn't in the demangled symbol list under an obvious
name). **This is the clear next step**: locate this string's virtual
address (file-offset → VA, same math `resolve.py`/`patch_kernelcache.py`
already do), find what function references it (an `ADRP`+`ADD` pair
loading the string's address, likely for a `printf`/`os_log` call, findable
by scanning nearby `.text` for that pattern or just breakpointing candidate
`AppleMobileFileIntegrity` symbols one at a time and checking `dmesg`
after each triggered `/sigkill_test`), then apply the **same
verify-then-patch approach** used above (disassemble first, find the exact
conditional branch guarding the kill decision, patch *that* branch to be
unconditional in the safe direction — don't guess at register-clearing
patches blindly).

Also worth doing as a quick follow-up: re-capture `exit_with_reason`'s x6
`os_reason` now that patch #1 is in place, to see this *second* kill's
reason (likely `OS_REASON_CODESIGNING` given the AMFI-specific message,
which would be a nice confirming cross-check).

**Also ruled out** (from earlier in the investigation, still valid): SEP
state corruption (tried resetting `sep_nvram`/`sep_ssc` to blank per the
official setup guide's method — this actually **crashed the whole QEMU
process**, not just the guest; reverted from backup, confirmed working
again — **do not retry blind SEP resets on an already-installed system**,
blank init only works at true first-boot).

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
   as `IOServiceOpen` earlier in the project).
   - Found the responsible kernel function via `kernel-symbols.txt`:
     `_hook_iokit_check_open` (a Sandbox.kext MACF policy hook,
     `PACIBSP`-prologued real function). Live-patched it in the running
     kernel via GDB (`mov x0, #0; ret` at its entry — same
     always-allow-in-place technique the project's own
     `kernel_patches.c` already uses for AMFI/SEP bypasses).
   - **Patch did NOT fix it.** Confirmed via a live breakpoint that
     `_hook_iokit_check_open` genuinely IS being called (hit fired, PC
     matched exactly) and does return cleanly — yet the exact same deny +
     entitlement message still appeared afterward. So either this isn't
     the actual enforcement point for the entitlement-specific message (a
     different function may own the `com.apple.security.iokit-user-client-
     class` string check specifically — possibly plain IOKit C++ code,
     e.g. `IOUserClient::copyClientEntitlement`/`clientHasPrivilege`, not
     a MAC policy hook at all), or there's a second, independent gate.
     **Not yet found — next step: search for the literal string
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
   it survives scratch/tmp cleanups (an earlier copy that only lived under
   a `/tmp/.../scratch/`-style path was lost this way once already —
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
  failure mode that has happened more than once.
