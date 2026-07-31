# Inferno GPU/Metal project — status and playbook

Status and technical writeup, last updated 2026-07-31. Covers what's done,
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
- The **5 SIGKILL-gate patches are now PERMANENT**, baked directly into
  `kernelcache.vgpu2.patched` by `patch_kernelcache.py` (no longer
  live-memory/GDB-only). Verified with a real, full QEMU kill+relaunch
  (not just code review): after the fresh boot, `/sigkill_test` goes
  straight to `Segmentation fault: 11` (the same benign unrelated-bug
  failure mode documented below, NOT `Killed: 9`) with zero GDB attached,
  and `/compute_test`/`/draw_test` both run to completion with fully
  correct results via plain `execve()`, also with zero GDB attached. See
  the dedicated dated update at the end of the SIGKILL section below for
  full detail.
- The **system-wide patch**: `___MTLCreateSystemDefaultDevice_block_invoke`
  inside the guest's own `dyld_shared_cache_arm64e` is hand-patched (raw
  ARM64 machine code, see `patch_block_invoke.py`) to redirect to our own
  bridge dylib (`/b` on the guest, built from `inferno_agx_bridge.m` +
  `inferno_command_queue.m` + `inferno_render_encoder.m`). Confirmed the
  patch bytes are still intact in the guest's DSC, including after the
  fresh reboot above (it's disk-resident, not memory-only, so this was
  expected but was re-verified anyway). **This means ANY real, unmodified
  app calling the standard public `MTLCreateSystemDefaultDevice()` gets our
  full device** — no dlopen tricks needed on the caller's side — and this
  IS observed happening for real, unrelated system processes (e.g. `dmesg`
  shows `com.apple.MapKit` hitting our patch's `dlopen("/b")` call and
  getting a sandbox deny reading `/b`, proving MapKit's own, real,
  unmodified call to `MTLCreateSystemDefaultDevice()` really does reach our
  patch).
  **`agx_system_metal_test.m` (the test that was supposed to prove this end
  to end via its own dedicated process) has now been run — it does NOT
  pass, in either of its two variants.** It crashes (`EXC_BAD_ACCESS`/
  `SIGSEGV`) before it ever reaches `MTLCreateSystemDefaultDevice()` at all
  (confirmed via live kernel-GDB breakpoints at the real function's outer
  entry, its inner `block_invoke`, and every step of our own patch — none
  of them ever fire), i.e. **this is not a bug in our patch** — something
  earlier in process launch is faulting. The leading hypothesis (dyld
  eager-binding of the direct C-symbol reference) was tested empirically by
  rewriting the test to use `dlsym(RTLD_DEFAULT, "MTLCreateSystemDefaultDevice")`
  instead of a direct call (preserving the original as
  `agx_system_metal_test_direct.m`) — **it crashes identically either way**,
  which disproves that hypothesis; the real cause is still unidentified.
  Full diagnostic writeup, what's ruled out, and next steps are in a new
  section right after this one, "`agx_system_metal_test` crash investigation
  (2026-07-30)".

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
  to use Metal in this build — **now answered, see "`backboardd`/compositor
  Metal-reach investigation (2026-07-31)" below: it does not, in this
  build** — or (b) building out our `AGXPrincipalDevice` fallback layer's
  `MTLDeviceSPI` conformance far enough (~523 private methods beyond the 112
  public `MTLDevice` ones) that it could actually be trusted for real
  compositing. Not attempted yet. **New strategic direction instead,
  investigated in "App-level Metal reach, IOSurface hand-off, and genpipe
  layering investigation (2026-07-31)" below**: leave backboardd untouched
  and get individual apps' own Metal-rendered content into backboardd's
  existing compositing via CoreAnimation's private cross-process
  `CAContext`/layer-hosting mechanism (the same one already used, live, by
  this build's own Today-View widgets) — a real, concrete interception
  point, and a materially different (cooperative, not adversarial/one-shot)
  architecture than the genpipe-overwrite trick the on-screen-triangle
  milestone above uses.

## `backboardd`/compositor Metal-reach investigation (2026-07-31)

Direct follow-up to the "Not started" bullet above ("confirming
`backboardd`/`WindowServer` even attempts to use Metal in this build").
**Answer: no — `backboardd`, the real iOS compositor daemon, never calls
`MTLCreateSystemDefaultDevice()` in this build.** Three independent lines
of evidence converge on this, detailed below. This was purely an
investigate-and-report task, per the task's own scope — no code changes.

**Step 0: process names, verified first.** `ps auxww` on the live guest
(QEMU had been up since the prior session, no restart needed — confirmed
`/sigkill_test` still gave `Segmentation fault: 11`, not `Killed: 9`,
before touching anything) shows **there is no `WindowServer` process on
this iOS build at all** — `WindowServer` is a macOS-only concept. The two
real candidates are:
- **`backboardd`** (`/usr/libexec/backboardd`, pid 60 in this boot) — the
  actual compositor/render-server daemon on iOS; it's what macOS's
  `WindowServer` corresponds to here.
- **`SpringBoard`** (`/System/Library/CoreServices/SpringBoard.app/SpringBoard`,
  pid 57) — the home-screen/app-launcher UI process, a *client* of
  backboardd's render server, not the compositor itself.

**Step 1: passive `dmesg` scan — negative, with a working positive
control.** Scanned the full captured kernel-log window (guest uptime
~108s through current, i.e. from shortly after boot through the time of
this investigation) for `metal`/`agx`/`MTLCreate` (any case): **zero
matches for any process.** Critically, this same log window *does* contain
the already-documented `com.apple.MapKit` hit (`Sandbox: com.apple.MapKit
(363) deny(1) file-read-metadata /b` etc., at guest-uptime ~1031s) — i.e.
the exact sandbox-deny-on-`/b` signature our block_invoke patch's
`dlopen("/b")` produces for a real process that *does* reach it. This is a
genuine positive control: the capture methodology is proven to surface
this signature when it happens, and it never happens for `backboardd`
(pid 60) or `SpringBoard` (pid 57) anywhere in the window.

**Step 2: static Mach-O analysis of `backboardd` itself — negative, and
explains *why*.** No `otool`/`nm`/`strings`/`ipsw` available (guest has
none of them; this Linux host still doesn't have `ipsw` either — same gap
noted in the crash-investigation section above). Worked around it the same
way this project always has for binary-structure questions: dumped the
Mach-O header + load commands via the guest shell's own `dd`/`od` (both
present) over the serial console, and parsed the hex dump with a small
throwaway Python load-command walker (same spirit as `resolve.py`/
`parse_obj.py`'s existing segment-walk code, not committed — one-off).

- `/usr/libexec/backboardd` is a **real, full, standalone Mach-O**, not a
  dyld_shared_cache stub (`__TEXT` vmsize `0xc8000` ≈ 800KB, 62 load
  commands, file size 1,121,392 bytes) — unlike `SpringBoard` (the on-disk
  file is a thin 16KB-`__TEXT` launcher stub, `LC_LOAD_DYLIB
  SpringBoard.framework/SpringBoard`, whose real code lives entirely
  in the dyld_shared_cache and has no standalone on-disk file at all —
  not statically inspectable this session without a DSC symbol/export-trie
  extractor, i.e. the same `ipsw`-shaped gap as elsewhere in this doc).
- `backboardd` **hard-links** (`LC_LOAD_DYLIB`, not weak, not
  `LC_REEXPORT_DYLIB`) `/System/Library/Frameworks/Metal.framework/Metal`
  — alongside `QuartzCore.framework`, `IOMobileFramebuffer.framework`,
  `IOSurface.framework`, and `GraphicsServices.framework`, a dylib set
  entirely consistent with `backboardd` being the real compositor (it's
  also where the extensive private `CARenderServer`/`CAContext*` C-symbol
  surface lives, e.g. `CARenderServerRenderDisplay`, confirming it hosts
  the actual, private render-server API — the direct macOS-`WindowServer`
  equivalent).
- Grepped `backboardd`'s raw bytes (`grep -a`, confirmed working via
  positive controls: found real, expected symbol counts for
  `objc_msgSend`, `dispatch_once`, `IOSurfaceCreate`, etc.) for every
  Metal-related symbol name that would have to appear literally in
  `__LINKEDIT`'s string table if referenced: **`MTLCreateSystemDefaultDevice`,
  `MTLDevice`, `CAMetalLayer`, `MTLCommandQueue`, `MTLTexture` — zero
  matches, all of them.** The **only** two Metal-prefixed symbols present
  anywhere in the whole binary are `MTLSetShaderCachePath` and
  `MTLMakeShaderCacheWritableByAllUsers` — housekeeping calls that
  configure where/how the on-disk Metal shader-compiler cache lives, not
  device-creation or rendering calls. This is presumably why
  `backboardd` links `Metal.framework` at all: system-level shader-cache
  directory administration, unrelated to whether it renders anything
  through Metal itself.
- By contrast, `backboardd` has **extensive, real usage** of
  `IOSurfaceCreate/Lock/Unlock/GetBaseAddress/GetBytesPerRow/GetWidth/
  GetHeight/...` (13 distinct symbols) and
  `IOMobileFramebufferOpen/SetDebugFlags` — i.e. its actual compositing
  path pushes raw pixel buffers through `IOSurface` straight into
  `IOMobileFramebuffer`, the classic CPU/software-composited-then-blit
  pattern, not a `CAMetalLayer`/`MTLDevice`-backed GPU path. This
  directly confirms the project's long-standing suspicion ("it probably
  already fell back to software compositing") with real evidence instead
  of just plausibility.
- Caveat: this only proves `backboardd`'s **own** compiled TEXT never
  references the symbol. It says nothing about whether `QuartzCore.framework`'s
  own private rendering backend (invoked *from* `backboardd`'s
  `CARenderServer` machinery, but living in the dyld_shared_cache, not
  `backboardd`'s own binary) might call it internally as an
  implementation detail — that's exactly what step 3 was for.

**Step 3: live kernel-GDB breakpoint sweep — negative, over a 515-second
steady-state window.** Armed the debug port (already listening on 1234
from an earlier session — no VM restart needed), connected with
`guest_tools/gdb_rsp2.py`'s `RSP` class, and set breakpoints at all six
addresses from the existing crash-investigation sweep above: the real
outer entry `_MTLCreateSystemDefaultDevice` (`0x1970505d0`), the
`dispatch_once` block's entry `___MTLCreateSystemDefaultDevice_block_invoke`
(`0x1970506e4`), our own patch body (`0x1970506fc`), the two stub call
targets our patch invokes (`dlopen` `0x1970a5cc0`, `dlsym` `0x1970a5cd0`),
and the block's shared epilogue (`0x197050750`). Proactively tried
clearing the known-hot stale `_arm64_retention_wfi` breakpoint first
(per this doc's own methodology note) — got `E22` (not currently set,
harmless). Watched 40 rounds of `continue` (515.2s real elapsed), only
ever inspecting PCs that matched one of the six armed addresses (per the
doc's own documented SMP-safety fix: never touch/step an unrecognized
stop). **Zero hits on any of the six addresses.** All six breakpoints
cleanly removed afterward (`z0` on each, all `OK`), `qmp_cont()` issued
and confirmed. Post-run sanity: QMP `info status` → `running` (not
paused), `/sigkill_test` → `Segmentation fault: 11` (gate patches still
intact), `/compute_test` → `result = 42 (expect 42)` (guest undisturbed),
`dmesg` scanned for panics/asserts (none, only ordinary
`memorystatus:`/`tx_flush:` chatter).

**Honest residual gap.** This window was run at guest uptime ~21-40
minutes, well after `backboardd`/`SpringBoard` (pid 60/57) had already
completed their own process launch (~T+0-2min, per their `ps` start
times). `___MTLCreateSystemDefaultDevice_block_invoke` sits behind a
`dispatch_once`, which is once-per-*process* (the predicate token lives
in a per-process COW `__DATA` page, not shared globally), so this
steady-state window **cannot** rule out a call that already happened
earlier in `backboardd`'s own lifetime — only a breakpoint armed *before*
a fresh boot, watching the actual boot sequence, closes that gap fully.
Not attempted this session: it requires a full QEMU kill+relaunch with
the breakpoint pre-armed before the guest even starts booting, which is
meaningfully higher cost/risk (boot-timing coordination, the documented
"QEMU dies silently during a long unattended wait" gotcha, the SMP
misattribution gotcha) for a check that steps 1 and 2 already answered
with high confidence by an entirely different, cheaper method (static
absence of the symbol from `backboardd`'s own compiled code is a much
stronger signal than a timing-dependent runtime observation would have
been anyway). Flagged here explicitly rather than silently assumed away,
per this task's own instructions.

**Bottom line.** Static evidence (no symbol reference anywhere in
`backboardd`'s own Mach-O), passive evidence (no sandbox-deny-on-`/b`
signature in the boot log, with a working positive control proving the
method would have caught it), and live evidence (zero breakpoint hits
over an 8.5-minute steady-state window) all agree: **`backboardd` does
not attempt to use the public Metal device-creation API in this build.**
It already has, and uses, a complete non-Metal compositing path
(`IOSurface` + `IOMobileFramebuffer`) — this isn't a partial/lazy Metal
adoption that just hasn't fired yet, it's a structurally different
rendering pipeline. The sandbox-allow-`/b`-for-backboardd idea (the
natural next step *if* it had reached the patch) doesn't apply — there is
no `dlopen("/b")` attempt to unblock, because there is no
`MTLCreateSystemDefaultDevice()` call to redirect in the first place.

**Concrete next steps for whoever picks this up:**
1. **Close the residual dispatch_once gap** (optional, lower priority
   given how convergent the evidence already is): arm the same six
   breakpoints, then do a full QEMU kill+relaunch (patches are baked into
   the kernelcache file now, so this is safe/cheap) and watch boot from
   the very start, to rule out a call in the first ~2 minutes of
   `backboardd`'s life with the same rigor as the rest of this
   investigation.
2. **Chase the QuartzCore-internal angle**: `backboardd` hosts
   `CARenderServer`, and `QuartzCore.framework`'s own private rendering
   backend (which actually decides Metal-vs-software per render context)
   lives in the dyld_shared_cache, not in `backboardd`'s own TEXT — this
   session's static check can't see inside it. Determining whether
   `CARenderServer`'s backend-selection logic has some *other*, private
   entry point into Metal (distinct from the public
   `MTLCreateSystemDefaultDevice()` symbol this whole project's patch
   targets) would need real DSC introspection tooling (`ipsw`, still not
   installed on this Linux host) or a hand-rolled DSC symbol/export-trie
   parser (this project has adjacent pieces already: `resolve.py`/
   `off2va.py`). This is genuinely the same class of gap as the existing
   `MTLDeviceSPI` private-surface note in the "Not started" section above,
   just for QuartzCore's internal backend rather than application code.
3. **A structurally different idea this session's findings directly
   suggest**: rather than trying to get `backboardd` to call into a Metal
   path it structurally doesn't use, meet it where it already is — it's
   already doing real `IOSurfaceCreate`/`IOMobileFramebufferOpen`-based
   compositing today. Intercepting/augmenting at that layer (e.g. wrapping
   or replacing what backs an `IOSurface` backboardd already creates, or
   hooking `IOMobileFramebuffer`'s presentation call) is a materially
   different integration point than "make Metal work end to end" — worth
   a real design pass of its own before committing to it, since it sidesteps
   the entire "does the real compositor even try Metal" question this
   session was scoped to answer, rather than resolving it. Not scoped or
   attempted this session, flagged only as an option raised directly by
   the evidence gathered.

Environment left clean: QMP `info status` confirmed `running`, all six
GDB breakpoints removed, `/sigkill_test`/`/compute_test` sanity-checked
(both as documented above), `dmesg` scanned for panics/asserts (none).
No kernel-side or userspace-side files were changed this session — purely
investigative, per the task's own scope.

## App-level Metal reach, IOSurface hand-off, and genpipe layering
## investigation (2026-07-31)

Direct follow-up to the backboardd investigation immediately above, and to
this project's new strategic direction: since backboardd itself never
touches Metal, can an individual **app** get Metal-rendered content into
backboardd's existing, unmodified compositing anyway? Three questions,
answered in order below. QEMU was already running from the prior session
(same pid); reused it rather than restarting, per the coordination note.
Debug port 1234 (GDB) was **not used at all this session** — every finding
below came from the guest serial console (`ps`, `grep -a`/`dd`/`od` on
binaries, `dmesg`) and from reading this project's own existing source
(`apple_displaypipe_v4.c`, `t8030.c`). Purely investigative — no kernel or
userspace files changed.

### 1. Does any app in this build reach CAMetalLayer/IOSurface-for-Metal-rendering code?

**This project's own test suite: confirmed no, by source inspection.**
Grepped every file in `src/userspace_test/` (all 16 `.m`/`.c` files,
including `agx_metal_api_compute_test.m`, `agx_metal_api_draw_test.m`,
`agx_functional_test.m`, `agx_system_metal_test.m`/`_direct.m`, and the
bridge itself) for `CAMetalLayer`/`IOSurface`: **zero matches, in every
single file.** This confirms in the strongest possible terms what
`PROJECT_STATUS.md`'s own "Fully proven" section already implied (offscreen
`MTLTexture` + `getBytes` readback only): the `CAMetalLayer` → `IOSurface`
→ backboardd path has **literally never been exercised anywhere in this
project**, test or bridge code alike. It isn't a matter of verifying
existing coverage — it would need to be built from scratch.

**Live process inventory, this exact boot (`ps auxww`, ~174 processes).**
This is a genuinely minimal research boot: no user-facing foreground app is
running at all (no Safari/Messages/Camera/etc., nothing in the
foreground). The only non-daemon, real app-bundle code actually executing
is a handful of Today-View widget extension processes SpringBoard's home
screen hosts: `WeatherWidget` (pid 223), `StocksWidget` (pid 360),
`PhotosReliveWidget` (pid 357), `GeneralMapsWidget` (pid 352),
`ScreenTimeWidgetExtension` (pid 349) — each a real `.appex` under
`/private/var/containers/Bundle/Application/<UUID>/<App>.app/PlugIns/`,
plus an ephemeral `com.apple.MapKit.SnapshotService.xpc` process spawned
per snapshot request (pid 598 at the time of this check; two *earlier*
instances, pids 363/364, are the ones with the interesting evidence
below). `SpringBoard` itself (pid 57) is of course also running, per the
prior investigation.

**Static analysis, same technique as backboardd (no otool/ipsw; `dd`+`od`
header dump and `grep -a` raw-byte symbol search over the guest serial
console) — negative across the board, but for an interesting, generalizable
reason.** Checked `SpringBoard`, `com.apple.MapKit.SnapshotService.xpc`,
and `GeneralMapsWidget` for the same symbol set the backboardd
investigation used (`CAMetalLayer`, `MTLCreateSystemDefaultDevice`,
`MTLDevice`, `MTLCommandQueue`, `MTLTexture`, `IOSurfaceCreate`,
`CARenderServer`), with `objc_msgSend` as a positive control for "does
this file contain any real compiled code at all":

| binary | size on disk | Metal/IOSurface symbols | `objc_msgSend` hits |
|---|---|---|---|
| `SpringBoard` | 58,928 B | 0 | **0** |
| `com.apple.MapKit.SnapshotService.xpc` | 72,848 B | 0 | 1 |
| `GeneralMapsWidget` (in Maps.app) | 738,448 B | 0 | 2 |

Every single app-level binary checked is a **thin dyld_shared_cache stub**,
not a real standalone Mach-O like backboardd was. `SpringBoard`'s zero
`objc_msgSend` hits is the sharpest confirmation: a file with *zero*
references to ObjC's own message-dispatch stub contains essentially no
compiled logic of its own at all — pure launcher stub, matching (and now
directly confirming, rather than inferring from segment size) the prior
investigation's characterization. `GeneralMapsWidget`, despite being 10-13x
bigger on disk than the other two, still only has 2 `objc_msgSend`
references — the extra size is almost certainly `Info.plist`/asset/
entitlement data and `NSExtension` boilerplate, not code. **This
generalizes the backboardd investigation's SpringBoard-specific caveat to
the whole app tier of this build**: static on-disk analysis of application
code is structurally blocked here, full stop, not just unlucky for one
binary — real app logic universally lives in the dyld_shared_cache, and
nothing short of a DSC symbol/export-trie parser (`ipsw`, still not
installed on this Linux host) will see inside it.

**Live/dynamic evidence closes part of the gap the static analysis
couldn't, and directly answers the core question: yes.** Re-ran (fresh
this session, not just citing the prior investigation's single already-
documented hit) `dmesg | grep -i mapkit` and found **two independent,
reproducible hits**, on two different ephemeral pids, each bearing this
project's own established positive-control signature for "a real,
unmodified process's own call to `MTLCreateSystemDefaultDevice()` reached
our `___MTLCreateSystemDefaultDevice_block_invoke` patch and attempted
`dlopen("/b")`":

```
[ 1031.098551]: Sandbox: com.apple.MapKit(363) deny(1) file-read-metadata /b
                 Sandbox: com.apple.MapKit(363) deny(1) file-read-data /b
                 ... (repeats) ... memorystatus: ... for StocksWidget:360
[ 2894.476932]: Sandbox: com.apple.MapKit(364) deny(1) file-read-metadata /b
                 Sandbox: com.apple.MapKit(364) deny(1) file-read-data /b
                 ... (repeats) ... memorystatus: ... for GeneralMapsWidget:352
```

Cross-checked this is genuinely unique to MapKit: `grep -oE 'Sandbox:
[A-Za-z.]+\([0-9]+\) deny\(1\) [a-z-]+ /b'` over the **entire** captured
dmesg buffer returns matches **only** for `com.apple.MapKit` — not
SpringBoard, not any other widget, not backboardd. **MapKit's map-tile/
snapshot rendering is the one and only real, currently-live iOS subsystem
in this build confirmed to attempt Metal device creation** — a completely
independent, real Apple framework, not our own test code, doing so
unprompted as part of its normal operation (rendering a map snapshot for
the `GeneralMapsWidget` Today-View widget). Because our sandbox profile
doesn't permit `com.apple.MapKit`'s XPC service to read `/b` (unlike
`/bin/bash`'s root shell, already confirmed permitted), the `dlopen` fails
and MapKit never actually obtains our bridge device — so this proves the
code path is **live and real**, not that it currently **succeeds**.

Whether MapKit goes on to construct an actual `CAMetalLayer`/Metal-backed
`IOSurface` after this failed device-creation attempt (as opposed to
falling back to CPU tile rendering, the same way backboardd's own
compositor already does) could not be determined this session — would need
either DSC introspection tooling for MapKit.framework's own private
rendering backend, or a live GDB session timed to catch one of these
~31-minute-apart refresh cycles in the act (not attempted — out of this
session's read-only/investigative scope, and the existing evidence already
answers the question this task actually asked). Also worth recording:
`dmesg` contains **zero** matches for `IOSurface` anywhere in the whole
buffer, for any process — IOSurface calls apparently aren't sandbox-logged
at all (unlike the Metal-via-`/b` signature), so this passive channel is a
dead end for extending the investigation any further this way.

### 2. How does backboardd discover/receive surfaces from other processes?

Extended the prior investigation's own step-2 symbol sweep of backboardd's
Mach-O (same `dd`/`od` + `grep -a` technique, no new tooling) with a much
wider symbol set, specifically targeting the cross-process hand-off
question the prior session flagged as unresolved.

**backboardd's own `IOSurface*` symbol set is narrow, and shaped like
"create and read my own surface," not "receive someone else's":**
`_IOSurfaceCreate`, `_IOSurfaceLock`, `_IOSurfaceUnlock`,
`_IOSurfaceGetBaseAddress`, `_IOSurfaceGetBytesPerRow`,
`_IOSurfaceGetWidth`, `_IOSurfaceGetHeight`, `_IOSurfaceGetAllocSize` (plus
the `IOSurfaceWidth`/`Height`/`PixelFormat`/`BytesPerElement`/
`BytesPerRow`/`AllocSize` CFDictionary property-key strings passed to
`IOSurfaceCreate`). **Zero matches** for any of the classic IOSurface
cross-process-handoff API: `IOSurfaceCreateXPCObject`, `IOSurfaceLookup`,
`IOSurfaceLookupFromMachPort`, `IOSurfaceCreateMachPort`. Conclusion:
**backboardd's own compiled code never receives a surface handle from
another process via the public IOSurface sharing API** — whatever it
composites into is a surface it made itself.

**backboardd DOES reference a small, specific, and very telling
CoreAnimation private-render-server symbol set.** `_CARenderServerRenderDisplay`
(the well-known private "ask the render server to redraw a display" entry
point — the same one macOS's `WindowServer` exposes; internal string
evidence, `com.apple.CoreAnimation.CAWindowServer.SecureModeV...`, shows
Apple's own code still calls this subsystem "CAWindowServer" internally
even on iOS, where the hosting *process* is backboardd, not a process named
WindowServer); `_CATransaction`/`_wrapInCATransaction`/
`formSynchronizedWithCATransaction`; and a cluster of private `kCAContext*`
CFString option-key constants (`kCAContextDisplayId`, `kCAContextSecure`,
`kCAContextDisableGroupOpacity`, `kCAContextIgnoresHitTest`, ...) plus
`tokenForIdentifierOfCAContext` — i.e. backboardd's own code constructs/
looks up `CAContext` objects by a numeric context identifier.

**The single most direct finding, and the answer to this task's core
question**: backboardd's own binary contains
`hostContextIDForEmbeddedContextID`, `contextIdHostingContextId`,
`hostingChain`, `hostingChainIndex`, `setHostingChain`,
`cancelsTouchesInHostedContent`, `hostCanRequireTouchesFromHostedContent`.
backboardd's own code implements/consumes CoreAnimation's private
cross-process **layer-hosting** mechanism: one process's `CAContext`
(identified by a numeric `contextId`) can be registered as "embedded"
within another process's ("host") context, forming a `hostingChain`. This
is Apple's standard, project-independent mechanism for exactly this kind
of cross-process UI composition — e.g. the Today-View widget extensions
this session found actually running (`WeatherWidget`/`StocksWidget`/
`GeneralMapsWidget`/etc.) render their own layer trees, which get embedded
into SpringBoard's UI and are ultimately drawn by backboardd's
`CARenderServerRenderDisplay` — **without backboardd ever needing to call
`IOSurfaceLookup` itself**, because the actual surface hand-off plumbing
lives one level down, inside QuartzCore's own private `CARenderServer`
implementation (which, per the prior investigation, is dyld_shared_cache-
resident and outside what static analysis of backboardd's own TEXT can
see — same tooling gap as section 1 above, now localized to a specific,
named subsystem instead of "somewhere in QuartzCore").

Supporting negative result: no bespoke "submit new layer content" IPC
service name was found — a literal `backboard_svc` search returned 0
matches, and the extensive `com.apple.backboard.*`/`com.apple.backboardd.*`
namespace found (~60+ distinct strings: touch delivery, HID, orientation,
haptics, display brightness, watchdogs, ...) contains nothing that looks
like a content-submission entry point. This reinforces that content
submission genuinely goes through CoreAnimation's own `CAContext`/hosting-
chain protocol, not a bespoke backboardd-specific channel. Also:
`IOMobileFramebuffer` symbols in backboardd remain limited to
`_IOMobileFramebufferOpen`/`_IOMobileFramebufferSetDebugFlags` (matching
the prior investigation) — no `SwapBuffer`/`DisplaySurface`-style symbol
found either, suggesting the final swap/present call is *also* issued from
inside QuartzCore's DSC-resident `CARenderServer` code (triggered by
`CARenderServerRenderDisplay`), not directly from backboardd's own
hand-written code. The overall shape: backboardd is a fairly thin
orchestrator around QuartzCore's private render server, which does
essentially all of the actual surface-handling and framebuffer-pushing
work internally.

**Bottom line for task 2**: there is a plausible, concrete interception
point, and it is *not* raw IOSurface-port-passing at the backboardd level
— it's CoreAnimation's own private cross-process `CAContext`/layer-hosting
protocol (`CAContext` + numeric `contextId` + `hostingChain`). A process
that (a) creates a `CAContext`, (b) renders into a `CALayer` whose backing
store is a Metal/Vulkan-produced `IOSurface` within that context, and (c)
gets that context registered into another process's hosting chain (exactly
the way the Today-widget extensions already, actually are, right now, in
this exact boot) would have its content picked up and drawn by
backboardd's existing, **completely unmodified** `CARenderServerRenderDisplay`
call, the same as any other embedded content. This isn't a hypothetical
mechanism reasoned about from first principles — it's Apple's own, already
proven live and in active use by real running processes in this session's
own `ps` output. Residual gap, same shape as the prior investigation's
"chase the QuartzCore-internal angle" item: the actual surface-lookup/
Mach-port mechanics *inside* CARenderServer's own DSC-resident
implementation weren't traced further this session (needs `ipsw`/a DSC
parser, same known gap as ever). What this session adds beyond the prior
investigation is the exact vocabulary to target once that gap closes
(`CAContext`, `contextId`, `hostingChain`), plus independent confirmation
that this mechanism is live and actively exercised in this exact build
right now, not just theoretically present.

### 3. Is genpipe pre- or post-backboardd-compositing?

Read `hw/display/apple_displaypipe_v4.c` (1093 lines) and the relevant
`t8030.c` wiring in full — no live debugging needed, confirming the task's
own suggestion that source-reading would be more productive here.

- `AppleDisplayPipeV4State` models **one** hardware display pipe.
  `t8030.c` instantiates exactly one (`object_property_add_child(OBJECT(t8030),
  "disp0", OBJECT(sbd))`, wired from the device tree's single
  `arm-io/disp0` node) — this is the one, real, physical ADP (Apple
  Display Pipe) scanout engine T8030 silicon has, not a general per-app/
  per-window compositor abstraction. It has 2 `genpipe`s
  (`ADP_V4_GP_COUNT == 2`) — the 2 hardware DMA-source "generic pipe"
  layers real Apple display hardware exposes (main content + one overlay
  layer), not an arbitrary-N-layer software compositor.
- `adp_v4_gp_read()` — the pre-existing hardware-emulation path that models
  what the real display driver's normal per-frame activity already does —
  DMA-*reads* from `genpipe->state.data_start`, a guest-physical address
  programmed by the **guest kernel's own real display driver** via the
  `GP_LAYER_0_DATA_START` hardware register, then composites it with
  `pixman_image_composite(PIXMAN_OP_SRC, ...)` — a straight overwrite
  blit, not alpha blending — onto the single QEMU display surface, gp0
  then gp1, every tick.
- **`adp_v4_present_frame()` (this project's own addition) DMA-*writes*
  directly into that exact same `genpipe->state.data_start` region** — the
  identical guest-physical memory the real display driver's own
  hardware-register-programmed DMA source already points to. This is
  explicitly documented in the function's own pre-existing comment (line
  349-360): "the same guest memory the real iOS display driver programs
  via GP_LAYER_0_DATA_START/etc and that `adp_v4_gp_read()` above already
  reads from every draw ... it lands in the exact spot the real
  compositor's own current frame lives."

**Answer, confirmed directly from source, not inferred**: the genpipe
mechanism is **backboardd's own final, already-composited output — not an
earlier or per-surface stage.** There is exactly one display pipe modeling
the one physical scanout engine; its DMA source is programmed by the
guest's real display driver, which is fed by backboardd's
`IOMobileFramebufferOpen`-based final composited frame (per task 2's
findings above); `adp_v4_present_frame()` simply overwrites that same
memory region after the fact. This confirms the task's own stated
most-likely hypothesis with direct source evidence.

**Consequence**: the project's existing on-screen-triangle milestone works
by *overwriting* backboardd's already-fully-composited frame post-hoc — a
one-shot/adversarial mechanism, which is exactly why it needs the
already-documented continuous 1s-interval re-presenting hack just to stay
visible (the real display driver keeps re-DMA'ing backboardd's own fresh
frames into that same memory region every refresh, stomping the injected
one). This is architecturally a dead end for "cooperative," ongoing,
per-app content contribution — confirms genpipe is **not** the mechanism to
build on for the new strategic direction. The `CAContext`/hosting-chain
path identified in task 2 is a structurally earlier, and far more
promising, interception point than genpipe: it sits *before* CARenderServer's
compositing rather than after it.

### Concrete next steps this session's findings point to

1. **Cheapest possible next experiment, no new test-app engineering at
   all**: MapKit's own, real, already-live attempt to call
   `MTLCreateSystemDefaultDevice()` (section 1 above) is blocked purely by
   a sandbox `file-read-{metadata,data} /b` denial for
   `com.apple.MapKit`'s XPC service — not by any absence of the code path.
   Finding and patching that one sandbox check (same class of fix as the
   still-outstanding `com.apple.security.iokit-user-client-class`
   entitlement gate from the SIGKILL investigation — a Sandbox.kext MACF
   hook, almost certainly reachable with the same disassemble-then-patch
   technique already used repeatedly in this project) would let a **real,
   unmodified Apple framework's own code** obtain our bridge device and
   attempt to actually render map tiles through it. This validates (or
   falsifies) the entire "apps can reach Metal" premise using zero new
   test-app code, at the cost of one more sandbox-policy patch of a kind
   this project has already done multiple times. Immediately observable
   next questions once unblocked: does MapKit's device creation now
   succeed, and does its own private rendering backend go on to build a
   `CAMetalLayer`/Metal-backed `IOSurface`, or fall back to CPU rendering
   even with a working device?
2. **The real, purpose-built follow-on**: write a new, minimal test app
   that (a) creates its own `CAContext`, (b) renders Metal content (via
   this project's already-proven `/b` bridge + `metal2vulkan`/`reims-vgpu`
   pipeline) into an `IOSurface`-backed `CALayer` within that context, and
   (c) gets registered into another process's `hostingChain` — most
   realistically by packaging it the same way the already-working
   Today-widget extensions are (a real `.appex`/`NSExtension`), since
   that's a *proven*-live hosting path in this exact build, rather than
   guessing at a lower-level API. Success criterion: backboardd's
   existing, **completely unmodified** `CARenderServerRenderDisplay`/
   hosting-chain compositing picks the content up and it appears
   genuinely composited into the real on-screen interface (not overwritten
   post-hoc, per task 3's finding about genpipe) — the real end-to-end
   proof of this session's whole strategic premise.
3. Both (1) and (2) still ultimately run into the same `ipsw`/DSC-parser
   tooling gap flagged repeatedly across this whole document (the prior
   backboardd investigation, and both open questions in tasks 1 and 2
   above) for anything that requires seeing *inside* QuartzCore's or
   MapKit's own dyld_shared_cache-resident code. That gap is not this
   session's to close, but it is now the single most-referenced blocker
   across every open thread in this project — worth prioritizing a
   from-scratch DSC export-trie/symbol-table parser (this project already
   has adjacent pieces: `resolve.py`/`off2va.py`) purely on the strength of
   how many independent investigations it would unblock at once.

Environment left clean: QMP `info status` confirmed `running` throughout
(never paused — GDB/port 1234 wasn't used this session at all),
`/sigkill_test` → `Segmentation fault: 11` (gate patches intact),
`/compute_test` → `result = 42 (expect 42)` (guest undisturbed), `dmesg`
scanned for panics/asserts (none, only ordinary `memorystatus:` chatter).
One operational note for whoever runs commands over this serial console
next: a single very long (~1650-character) semicolon-chained shell command
sent in one line got corrupted mid-transmission and left the guest's bash
sitting at an open-quote continuation prompt (`>`) — recovered cleanly by
sending `Ctrl-C` twice over the same connection, no VM/session impact, but
worth keeping individual command lines short (a few hundred characters)
over this link, consistent with the file-transfer tooling's own
already-documented `CHUNK=100`-byte-per-line lesson.

## MapKit `/b` sandbox-deny investigation (2026-07-31, in progress)

Direct follow-up to "Concrete next steps... 1." at the end of the
App-level Metal reach investigation immediately above: find and patch the
exact kernel check responsible for the `Sandbox: com.apple.MapKit(NNN)
deny(1) file-read-{metadata,data} /b` denial, so MapKit's own real,
unmodified `MTLCreateSystemDefaultDevice()` call (already confirmed
reaching our `___MTLCreateSystemDefaultDevice_block_invoke` patch) can
actually `dlopen("/b")` successfully. **Not yet complete** — static
analysis and patch-byte preparation are done and high-confidence; live
verification is still in progress as of this writing, delayed by a
methodological discovery documented below. This section will be updated
again once live-verified and baked in; committing now per this session's
own instruction to land progress incrementally rather than hoard it.

**Recovered environment state first.** QEMU (same pid as the prior
session) was found with the debug port genuinely stuck in `paused
(debug)` — `info status` showed this consistently across a 15s polling
window, and a plain QMP `cont` did not clear it (matching the exact
"stuck paused, cont can't clear it" gotcha already documented in the
SIGKILL section's methodology notes). Root cause: the scratchpad directory
for this task (same session ID as a prior, interrupted attempt) contained
`mapkit_sandbox/verify_gate_mapkit.py`, a never-completed live-verification
script from that earlier attempt — it had gotten as far as identifying
candidate breakpoint addresses (see below, all independently re-derived
and confirmed by this session too) but apparently left a breakpoint
session dangling mid-flight when it was interrupted. Fixed the only way
this doc's own playbook allows: killed and relaunched QEMU (cheap, since
all patches — the 5 SIGKILL gates and the block_invoke DSC patch — are
disk-resident/file-baked, confirmed intact immediately after via
`/sigkill_test` → `Segmentation fault: 11`, not `Killed: 9`).

### Static analysis: found and disassembled every plausible candidate check

**Step 1: confirmed the master sandbox operation-name string table.**
`strings`-equivalent scan of `kernelcache.decompressed` around file offset
`0x559000` found Apple's real, alphabetically-*grouped* (not strictly
alphabetical — e.g. `device*`/`device-camera`/`device-microphone` are
grouped before `darwin-notification-post`, so operation index cannot be
assumed to equal simple alphabetical position) master operation-name
string blob, containing `file-read-data` (file offset `0x5590a1`) and
`file-read-metadata` (file offset `0x5590b0`) as literal, distinct,
null-terminated C strings — confirming these are real, first-class
Sandbox.kext operation names, not something this project's own guessing
invented. Searched for a raw 8-byte pointer table referencing either
string's VA anywhere in the file: **zero matches** — the kernel doesn't
have a static `name[op_index]` pointer array; op-index-to-name mapping (if
it exists at all in compiled code, as opposed to only in profile-compiler
tooling) isn't done via a simple indexed pointer table. This ruled out
"trace the string to its owning function via ADRP+ADD" (the exact
technique used for gate #2a/#2b/#3/#4's AMFI/Sandbox strings) as a
practical path here — unlike `"dyld signature cannot be verified"` etc,
which are printed as literal per-call-site format strings, `file-read-data`/
`file-read-metadata` appear to only exist as compile-time-only names for
whatever tool generates Sandbox.kext's operation-index table, not as
runtime-loaded strings referenced by `ADRP`+`ADD` anywhere in `__TEXT_EXEC`
(confirmed: 92 ADRP instructions land on that string's page, zero of them
feed a matching ADD/LDR within a 5-instruction lookahead window).

**Step 2: found every plausible check by disassembling the actual MACF
vnode-check hooks directly, using their own hardcoded op-index immediates
instead.** Every `hook_vnode_check_*` function passes a literal op index
(`movz w1, #N`) to either a shared wrapper, `_cred_sb_evaluate`
(`0xfffffff0092a0378`), or (for `open`/`access`, which need extra
pre/post logic) directly to the real shared bytecode evaluator,
`0xfffffff0092a9ef4`. Disassembled (own extension of `mini_disasm.py`,
reused unmodified) every function in the vnode-check family that could
plausibly back a `dlopen("/b")`'s metadata-probe + actual open+mmap, and
found the exact op-index each one uses:

| function | VA | evaluate call | op | plausible role |
|---|---|---|---|---|
| `hook_vnode_check_open` | `0xfffffff0092a242c` | direct → `0x92a9ef4` | `0x15` **and** `0x1f` (2nd only if flags & 0x402) | the real `open()`/`dlopen()` data-read check |
| `hook_vnode_check_access` | `0xfffffff0092a3524` | direct → `0x92a9ef4` | `0x15`, `0x1f`, `0x67` (each gated by a separate flag bit) | `access()`-based probe (less likely for dlopen, checked for completeness) |
| `hook_vnode_check_getattr` | `0xfffffff0092a3e64` | `cred_sb_evaluate` | `0x16` | `stat()`/`fstat()`-family metadata probe |
| `hook_vnode_check_stat` | `0xfffffff0092a1c0c` | `cred_sb_evaluate` | `0x16` | `stat()` syscall metadata probe |
| `hook_vnode_check_readlink` | `0xfffffff0092a23b0` | `cred_sb_evaluate` | `0x16` | symlink-resolution metadata probe |
| `hook_vnode_check_getattrlist` | `0xfffffff0092a2ccc` | `cred_sb_evaluate` | `0x16` | `getattrlist()` metadata probe |
| `hook_vnode_check_lookup_preflight` | `0xfffffff0092a0c08` | `cred_sb_evaluate` | `0x1a` | unrelated (file-search-ish), kept only as a completeness check |

Given the two actual dmesg lines are `file-read-metadata` and
`file-read-data` (exactly two, not more), and `open`'s own two checks are
`0x15`/`0x1f` while every metadata-ish hook above uniformly uses `0x16`,
the working hypothesis (consistent with, though not yet proven identical
to, the interrupted prior attempt's own labeling) is **op `0x15` =
file-read-data, op `0x16` = file-read-metadata** — the *shape* of the
evidence (exactly 2 ops, matching exactly 2 dmesg lines, `open` uniquely
supplying one of them) is strong even without the string-table index
independently confirming the exact numbers; live correlation (in
progress) is the real confirmation step, precisely because a static
numeric coincidence isn't proof.

**Step 3: explicitly ruled out patching the shared wrapper/evaluator —
too broad.** Before settling on a per-call-site patching strategy,
checked how many distinct call sites use `_cred_sb_evaluate`
(`0xfffffff0092a0378`): **103 separate `BL` callers**, spanning
`hook_kext_check_load` through dozens of unrelated `hook_sysv*`/`hook_iokit*`
functions — i.e. this is Sandbox.kext's *generic* single-op-evaluate
wrapper, used far beyond just the file-read family. Patching it (or the
even-more-shared inner evaluator at `0x92a9ef4`) would be a
`cs_process_global_enforcement`-style hammer disabling a huge swath of
unrelated sandbox checks system-wide — ruled out as inconsistent with
this project's own "minimal, per-check, disassemble-first" precedent.
**The patch must live at each specific hook's own call site**, matching
exactly how gate #2a/#2b (two independent checks inside the very same
`cred_label_update_execve`) were kept separate.

**Step 4: disassembled each candidate's own post-call code to find the
minimal correct per-site patch — and found the shapes genuinely differ**,
same as gates #1 vs #2b/#3/#4 differed:

- `hook_vnode_check_open`, op `0x15`: clean branch-based shape —
  `bl 0x92a9ef4` → `mov x21,x0` → **`cbnz w21, 0xfffffff0092a25c0`**
  (taken = early-return-with-denial; not-taken = falls into the op-`0x1f`
  write check, exactly the normal continue path). Minimal patch: **NOP the
  `cbnz`** at `0xfffffff0092a252c` (orig `b5040035` → `1f2003d5`) — same
  reasoning as gate #2b/#3/#4 (not-taking the branch already is the
  correct continuation).
- `hook_vnode_check_access`, op `0x15`: identical shape —
  `cbnz w22, 0xfffffff0092a3768` at `0xfffffff0092a3644` (orig `36090035`
  → NOP `1f2003d5`). Prepared for completeness; only needed if live
  evidence shows `access()` rather than `open()` is actually in play.
- `hook_vnode_check_getattr`, op `0x16`: **no early-return branch at
  all** — `bl 0x92a0378` → **`mov x20,x0`** (a local copy, unique to this
  call site) → later, a `tbz x0,#37,...` gates only whether to *also*
  populate some extended-attribute fields, but **every path** ultimately
  does `mov x0,x20` before `ret`, i.e. the real allow/deny result is a
  pure, unconditional passthrough of whatever `x20` holds. Minimal patch:
  since `mov x20,x0` is a call-site-local instruction (not shared),
  replace it with **`movz x20,#0`** at `0xfffffff0092a3f00` (orig
  `f40300aa` → `140080d2`) — lets `cred_sb_evaluate` (and its own internal
  logging/evaluation side effects) run exactly as before, just discards
  the result at this one call site, consistent in spirit with the
  gate-family precedent of "let the real check run, override what happens
  with its result."
- `hook_vnode_check_stat` / `hook_vnode_check_readlink` /
  `hook_vnode_check_getattrlist`, op `0x16`: even more trivial than
  `getattr` — **no capture register at all**, the return point is
  *directly* the function epilogue (`ldp x29,x30,...` reading `x0`
  unmodified as the return value). There's no free instruction to
  overwrite without corrupting the epilogue's frame-pointer/LR restore.
  Minimal patch here has to be shaped differently: since there's no room
  to insert anything, replace the **`bl 0x92a0378` call itself** (still a
  single, call-site-scoped 4-byte swap, same size in/out) with
  **`movz x0,#0`** — skips invoking the shared evaluator for this one
  call site only (so its side effects, e.g. any internal logging, are
  skipped for this specific site, but no other of the 103 callers is
  touched). Exact bytes: `stat` @ `0xfffffff0092a1c80` (orig `bef9ff97` →
  `000080d2`), `readlink` @ `0xfffffff0092a2418` (orig `d8f7ff97` →
  `000080d2`), `getattrlist` @ `0xfffffff0092a2d34` (orig `91f5ff97` →
  `000080d2`). All three verified byte-for-byte against the static
  `kernelcache.decompressed` file directly (not just against earlier live
  dumps) before finalizing.

All six candidate patches above are fully prepared (bytes computed and
independently round-tripped through `mini_disasm.py`'s own decoder to
confirm each encodes what it's meant to) but **not yet applied anywhere**
— live evidence is needed first to know which 1–2 of the six are actually
the ones MapKit's `dlopen("/b")` hits, per this task's own explicit
instruction not to patch blind.

### Live verification: in progress, one major methodological pitfall found and fixed along the way

Armed all ten candidates (the six above plus the three `access()` variants
and `lookup_preflight`, kept during the first pass for completeness) via a
hand-rolled RSP client (`gdb_rsp2.py`, reused unmodified) on a **freshly
relaunched** QEMU (to catch the documented ~1031s/~2894s early-boot window
from `t=0`, since the current long-uptime boot's dmesg buffer no longer
contained either historical hit — the ring buffer had long since wrapped
past them). Also added proper `Hg<tid>`-based thread-scoping (parsing the
`T05thread:NN;` field out of every stop reply before any `g` register
read) as a robustness improvement over the interrupted prior attempt's own
script, per this project's own documented SMP-misattribution risk.

**First full pass (40 minutes wall-clock, `WALL_CLOCK_DEADLINE_S=2400`):
zero denies on any candidate**, despite substantial, healthy allow-hit
activity confirming every breakpoint address is correct and genuinely
"hot" code (e.g. 76 hits on `open`'s op-`0x15` check, 142 on
`lookup_preflight`). Initially ambiguous — could have meant the hypothesis
was wrong, or that this boot's MapKit renderer just didn't fire.

**Root-caused via a direct comparison against the guest's own kernel-uptime
clock, not just wall-clock:** `dmesg`'s own bracketed timestamps (which
tick with genuine guest CPU execution, the same clock the historical
`[1031.098551]`/`[2894.476932]` marks used) had only reached **`[
200.512945]`** by the end of the 40-minute (2400s) wall-clock observation
window. **This is the real explanation, not a disproof of the hypothesis:
2400 seconds of host wall-clock time only advanced the guest's own clock
by ~200 seconds — roughly 12x dilation** — meaning the watch never
actually reached the historical ~1031s guest-uptime mark in the units
that matter, despite comfortably exceeding it in host wall-clock terms.
Confirmed this is specifically caused by keeping GDB software breakpoints
armed continuously (QEMU's gdbstub appears to impose a large, cumulative
overhead — whether from the sheer volume of stop/step/reinsert round
trips this hand-rolled Python RSP client requires, 19,024 of them over the
40-minute window, or from QEMU/TCG's own internal slower code-generation
mode whenever any software breakpoint is active, wasn't further
root-caused — the fix doesn't require knowing which): a clean, controlled
before/after check with **zero** breakpoints armed and **no GDB client
connected at all** showed guest uptime advancing by ~60–73 seconds across
a 73-second host wall-clock window — i.e. **~1:1, no measurable
dilation**, confirming breakpoints (not something else, like general host
load) are the specific cause.

**Methodology fix for future long passive waits on this platform,
worth remembering project-wide, not just for this task:** never leave GDB
software breakpoints armed for a long unattended wait when the trigger
timing is only loosely known. Instead: let the guest run completely free
(no debugger attached at all) for the bulk of the wait, tracking progress
via cheap, non-pausing guest-shell polling (`uptime`, not `dmesg` — a
first attempt at polling `dmesg` for the latest timestamp was itself
briefly misleading, since `dmesg` can go log-quiet for a couple of minutes
at a time, which looks identical to "guest time has stalled" if you're
only checking the newest log line's timestamp; the guest's own `uptime`
command output doesn't have that ambiguity), and only arm breakpoints
reactively for a short, targeted window once guest-uptime is close to the
expected mark.

**Status as of this commit**: re-armed with a trimmed 6-candidate set
(dropped `lookup_preflight` and the 3 `access()` variants — zero hits
across the entire first pass, lower prior probability, purely to reduce
per-hit overhead during the now-short active window) once free-running
guest uptime approached the historical ~1031s mark. Live capture is
in progress; this section will be updated with the actual result (which
candidate(s) fired, the live-verified patch, and — once baked into
`patch_kernelcache.py` and verified via a real restart — the permanent
fix) as soon as it resolves, per this task's own instruction to commit
real progress incrementally rather than wait for a single final commit.

### UPDATE, same investigation: three separate observation windows all
### came up empty; wall-clock-freeze theory checked and ruled out;
### active triggering attempted with partial results

After the section above, ran the re-timed methodology through to
completion: a targeted 20-minute window armed right as free-running guest
uptime crossed into the historical ~1031s mark (confirmed via `dmesg`
timestamps reaching **`[1205.6]`** by the end of the window — i.e. this
window did genuinely cover the exact guest-uptime range that historically
contained the first hit, not just in wall-clock terms this time), then a
third ~6.5-minute window that additionally tried an active trigger
(`kill -9` on `GeneralMapsWidget`'s pid mid-window). **All three windows
(40min free-from-t0, 20min targeted-at-1031s, ~6.5min targeted+triggered):
zero denies**, despite healthy, correctly-classified allow-hit activity on
every candidate in every window (hundreds of legitimate hits confirming
the breakpoints are correctly placed and firing on real traffic, not a
tooling failure).

**Investigated the large "unmatched breakpoint hit" counts (8331 in the
20-minute window, 2541 in the third) before concluding anything from the
zero-deny results** — worth being sure this wasn't our own op-classification
logic silently swallowing real hits. Extracted the actual PCs behind a
sample of these: 18 distinct addresses scattered across completely
unrelated kernel functions (`0xfffffff007a88e34`, `0xfffffff007b4e2c0`,
`0xfffffff0091cbdbc`, `0xfffffff008125a14` — one instruction past the
already-documented stale `_arm64_retention_wfi` breakpoint address — etc,
no repeated clustering around any of our own 6 candidate VAs). This is
consistent with, and further confirms, the already-suspected root cause of
the earlier dilation finding: QEMU's all-stop-mode gdbstub appears to
propagate a single thread's `s` (single-step) into incidental stepping/
stop-reporting for *other*, unrelated vCPUs too (a known class of
limitation for naive all-stop SMP handling, not something a plain RSP
client can fully work around without full `vCont`-based per-thread
control). **Conclusion: the unmatched hits are debugger-induced noise from
stepping over our own breakpoints on a busy 7-vCPU target, not
misclassified real hits** — they don't change the zero-deny finding's
interpretation.

**Checked the coordinator-raised "is the guest's wall clock actually
advancing" theory directly, since MapKit/WidgetKit-style refresh
scheduling plausibly depends on wall-clock (`NSDate`) budgets rather than
kernel monotonic uptime, which would make it insensitive to KERNEL time
window coverage even if that's correct — a real, distinct hypothesis from
the already-fixed monotonic-uptime dilation.** Result: **on a freshly
relaunched boot, disproven** — `date +%s` read twice (in-band over the
serial console, ~29s of host wall-clock apart) advanced from
`1785469345` to `1785469376`, i.e. ~31 guest-seconds for ~29 host-seconds,
essentially 1:1, no measurable freeze. The earlier appearance of a "stuck"
guest clock was almost certainly just another visible symptom of the
already-diagnosed GDB-breakpoint dilation (halting the VM halts *every*
clock domain uniformly, wall and monotonic alike) during the long
breakpoint-armed windows, not a separate, independent bug.

**Active-trigger attempts, mixed results.** `kill -9` on
`GeneralMapsWidget`'s pid did **not** cause it to respawn within the
observation window (absent from `ps` afterward) — Today-View widget
extensions in this build apparently aren't eagerly relaunched by a
supervisor the way a `LaunchDaemon` would be, consistent with them only
being instantiated on-demand when something (SpringBoard's Today-View
list) actually asks for them. Tried the stronger lever next — `kill -9` on
`SpringBoard` itself (pid 57, to force a full "respring" and therefore a
genuinely fresh Today-View/widget-list reload) — but this run's GDB
session got left in a stuck `paused (debug)` state (the watcher script was
killed with `-9` before its own `finally`/breakpoint-cleanup could run,
after a burst of activity around the kill made serial-console responses
intermittent enough that a clean stop point wasn't reached in time), **not
verified whether the SpringBoard kill itself would have produced a hit**
before the recovery restart was needed. Recovered via the standard
kill+relaunch playbook (cheap, all patches file-baked/disk-resident,
confirmed by the wall-clock check above already having been performed
successfully on the resulting fresh boot).

**Not yet resolved.** Three clean, correctly-timed/covered observation
windows with zero denies is a real, if still not fully conclusive, signal
against the original op-index hypothesis reproducing readily on every
boot — but static evidence (the exact 2-op shape matching `open`'s own
`0x15`/`0x1f` split and the metadata family's uniform `0x16`, see the
static-analysis subsection above) is still reasonably strong on its own
terms, and the SpringBoard-kill trigger was never actually completed
cleanly. **Next step, picking back up on the freshly-relaunched boot**:
retry the SpringBoard-kill trigger (this time with the breakpoint-removal/
cleanup dance given more headroom, e.g. a less aggressive process-kill
signal to the watcher itself if it needs to be stopped mid-run, or simply
letting its own bounded deadline elapse naturally instead of `kill -9`-ing
it) armed with the trimmed 6-candidate set, and if that also comes up
empty, treat the original two dmesg hits as provisionally
boot-instance-specific/non-reliably-reproducible and consider either (a)
a much longer natural free-run (covering multiple ~31-minute-spaced
cycles, now that the free-run methodology is cheap/dilation-free) or (b)
revisiting whether some *other*, not-yet-considered function (outside the
7 vnode-check hooks examined so far) is the real target, using a wider
net of candidate breakpoints for one more static-analysis pass before
the next live attempt.

### UPDATE: session wrap-up — live confirmation not achieved this session,
### two more real findings surfaced along the way, environment left clean

Picked back up exactly where the previous update left off, on a freshly
relaunched boot (3rd restart of this task). Found **why `GeneralMapsWidget`
never appeared at all on the 2nd restart's boot** — a plain, non-GDB
`ps`-polling wait (`wait_for_widget.py`, cheap, dilation-free) ran the full
15-minute safety window (guest uptime confirmed reaching ~1500s via its own
polling) and the widget simply **never launched that boot at all**
(`ps` showed `SpringBoard`, `WeatherWidget`, and the Maps support daemons
`destinationd`/`mapspushd`/`navd` all present and healthy, but no
`GeneralMapsWidget` process ever appeared). This conclusively explains that
boot's zero-deny result without needing to doubt the op-index hypothesis at
all: **if the widget process itself never instantiates, there is no
possible path to a MapKit snapshot request, full stop.** This is a new,
concrete finding in its own right: which Today-View widgets SpringBoard
actually instantiates is **non-deterministic boot-to-boot** in this build
(confirmed by contrast: on the very next restart, the same widget appeared
within ~130s).

**On that next (3rd) restart, with the widget confirmed present early,**
armed the trimmed 6-candidate set immediately and, separately, **the
device's screen turned out to be reachable and interactively usable** —
mid-session, real interactive use of the emulated device (tapping the Maps
app icon on the home screen) produced a **real, observed Maps app crash**.
This is a new, unverified-cause data point: unfortunately the specific GDB
session covering that moment was left in a stuck `paused (debug)` state
before the crash's own kernel-log evidence could be captured (a breakpoint
removal, `z0 open_op15_file_read_data`, didn't get a reply before a
`KeyboardInterrupt`-based script shutdown, leaving that one breakpoint's
trap byte in place; the immediate next `cont` re-triggered it instantly,
producing the same "stuck, plain `cont` can't clear it" failure mode
already documented earlier in this section) — recovered via the standard
kill+relaunch, which necessarily lost the in-memory `dmesg` evidence from
that specific moment (guest RAM only, not disk-persisted). **Not
re-investigated further this session** (see "left for a future session"
below) — but worth recording precisely, since it's the first real evidence
in this whole investigation that (a) the emulated device's screen is
genuinely interactive from outside the guest (via QMP `mouse_move`/
`mouse_button`, confirmed partially working — a scripted swipe-up on the
lock screen visibly changed the lock screen's own UI state, though it did
not reach a full unlock in this session's attempts, likely a timing/gesture
-recognition tuning issue rather than a fundamental blocker) and (b) Maps
crashing on open is itself possibly relevant to this whole investigation
(if Maps.app's own UI, not just the widget's snapshot renderer, also
attempts real Metal device creation and crashes similarly to
`agx_system_metal_test`'s still-unexplained pre-`main()` dyld-bootstrap
crash documented elsewhere in this doc) — flagged as a concrete, promising
lead for whoever continues this, not chased down further given this
session's time budget.

**A second, separate, and more serious finding surfaced during the
mandatory post-recovery stability check**: `/compute_test` — the single
most load-bearing sanity check used throughout this *entire* project's
history, always previously either "IOServiceOpen succeeded... result = 42"
or not run at all, **never previously seen to fail** — triggered a **real
guest kernel panic** ("Kernel data abort", `pid 422: compute_test`, full
register dump and backtrace captured from the guest serial console),
immediately followed by **the QEMU host process itself dying silently**
(no error in its own redirected stdout log, the process and its QMP unix
socket both simply gone). The panic backtrace's `lr` chain (e.g.
`0xfffffff009429138`/`0xfffffff0094290d0`/`0xfffffff009429078`) sits just
past `CODE_BASE` (`0xfffffff009427e10`, where `patch_kernelcache.py`
injects this project's own `InfernoVGPUHello` object code) — i.e. **the
panic happened inside this project's own injected driver code**, not
generic Apple kernel code. The QEMU-side log's last several hundred lines
before the death are a tight, rapidly-repeating loop of `inferno-vgpu`
FIFO-opcode-`0x0004` (compute-dispatch) register traffic — consistent with
`compute_test` issuing many back-to-back dispatches right before whatever
faulted. This is a concrete, evidenced reproduction of the previously only
vaguely-suspected gotcha already on record in this doc ("QEMU was observed
to die silently... during a long unattended background wait — cause
unconfirmed") — this session's version had a directly correlated guest-side
panic immediately preceding the death, which the earlier note didn't have,
narrowing the likely cause toward the custom `inferno-vgpu` device/driver's
FIFO dispatch path specifically rather than generic host idle handling.
**Important caveat: NOT reproducible on an immediate retry** — recovered via
the standard kill+relaunch, and a fresh `/compute_test` run immediately
after came back completely clean (`result = 42 (expect 42)`, exit 0, no
panic) — so this looks like a rare, state/timing-dependent race rather than
a consistently-broken path, but it's real, it's new, and it's now on
record with enough detail (exact panic register dump preserved in this
session's own transcript, not reproduced verbatim here for length) for a
future session to pick up if it recurs.

**Bottom line for the MapKit `/b` sandbox-deny task specifically**: **not
resolved this session.** The static analysis (six fully-prepared,
byte-verified candidate patches, the shared-evaluator over-broad-patch
rejection, the full disassembly of every plausible vnode-check hook) is
solid, evidence-based work product, ready to apply the moment a live
deny is actually captured. Live capture itself was not achieved despite:
5 separate observation windows (40min, 20min, ~6.5min, ~1.7min-then-stuck,
plus this session's widget-presence-polling and swipe attempts) across 4
separate QEMU boots, correctly covering the historically-relevant
guest-uptime window at least twice, an active `kill`-based widget-restart
trigger (didn't cause a respawn), an active SpringBoard-restart trigger
(inconclusive — GDB session got stuck before the result could be read),
and a real observed Maps-crash-on-tap (evidence lost to a forced restart
before it could be correlated with `dmesg`). Per this task's own explicit
allowance for a partial, evidence-based outcome: **this is where this
session stops.** Concrete, prioritized next steps for whoever picks this
back up:
1. **Cheapest, most likely to succeed**: retry the GUI-interaction path
   from this session — a scripted QMP swipe-up **did** visibly perturb the
   lock screen's UI (the "swipe up to open" prompt visibly faded/reset),
   just short of a full unlock; tuning the gesture (faster motion, a real
   multi-touch-style pressure/duration profile, or checking whether this
   build has *no* passcode at all vs. needing a code entered afterward via
   scripted taps on a keypad) is a bounded, concrete task, not a fresh
   investigation — and directly opens the door to also tapping Maps
   (already confirmed reachable and crash-producing this session) with
   breakpoints pre-armed, which would be a fully deterministic,
   on-demand trigger instead of waiting on any widget's own scheduling.
2. **Most reliable, higher cost**: write a small, dedicated CI-built test
   app using the real public `MKMapSnapshotter` API directly (this
   project already has the whole CI/transfer/exec pipeline proven, e.g.
   the `agx-bridge-dylib` job) — this would trigger the exact same
   `MTLCreateSystemDefaultDevice()` → `dlopen("/b")` path as MapKit's own
   XPC service, fully on-demand, with zero dependence on SpringBoard's
   own widget-loading nondeterminism.
3. **Chase the newly-observed Maps-crash-on-tap lead** (item above) — if
   Maps.app's own UI independently reaches (and crashes at/near) the same
   Metal-device-creation path `agx_system_metal_test` already got stuck at,
   that's a second, independent data point on that still-unsolved crash
   investigation, potentially more tractable than a synthetic test binary
   since it's a real, signed, fully-provisioned system app.
4. If a live deny is ever captured, applying the fix is now a short,
   mechanical step — re-read the "Static analysis" subsection above for
   the exact VAs/bytes, apply via the same live-GDB-patch-then-bake-into-
   `patch_kernelcache.py` pattern already used for all 5 SIGKILL gates.

**Environment left clean**: QEMU restarted fresh one final time after the
`compute_test` panic (4th restart this session), `/sigkill_test` →
`Segmentation fault: 11` (gate patches intact), `/compute_test` → clean
`result = 42` retry (no repeat panic), `dmesg` scanned for panics/asserts
(none on this final boot), QMP `info status` confirmed `running`, no GDB
session left attached or breakpoints left armed.

### UPDATE 2026-07-31 (new session): active GUI-tap trigger tried against
### Maps.app itself — clean negative result, narrows the search

Picked up on the same QEMU instance (pid unchanged, up since this session's
own boot, no restart needed — confirmed `/sigkill_test` →
`Segmentation fault: 11` and `/compute_test` → `result = 42` both clean
before touching anything). Found the previous session's own uncommitted,
unfinished work already sitting in `guest_tools/`: `qmp_raw.py` (raw QMP
client with `screendump`/`tap`/`swipe` helpers — `swipe()` is a real
multi-step held drag, not the earlier single-jump attempt that only got
"just short of a full unlock") and `tap_maps_watch.py` (arms all 10 sandbox
vnode-check candidates + the 6-address block_invoke chain +
`handle_user_abort`/`exception_triage` simultaneously, fires a QMP tap, then
watches a bounded window) — exactly the tool `NEXT_SESSION_PROMPT.md`
described an interrupted background agent as having been mid-build on.
Neither file had been run to a documented conclusion or committed; both are
committed now, alongside this update.

Screendump showed the guest already sitting on the home screen (not
locked), with a "Доступно обновление iOS" alert covering part of the
screen — dismissed via a direct tap on "Закрыть" (confirmed via a
follow-up screendump), then confirmed the Maps icon's actual on-screen
position (screendump-cropped, center ≈ (320, 440) in the 828×1792 frame)
matches `tap_maps_watch.py`'s own hardcoded default almost exactly — the
previous session had already calibrated this correctly against a real
screenshot.

Ran `tap_maps_watch.py 200 320 440 Maps` (200s wall-clock deadline, all 18
breakpoints armed first, tap fired 3s in). **Clean run, no tooling
issues**: 2090 rounds, all 18 breakpoints cleanly armed/removed, QMP `cont`
confirmed at the end (`RESUME` event observed). **Result: zero sandbox
denies, zero block_invoke-chain hits, zero `exception_triage` hits, and —
notably — zero crash** (unlike the previous session's observed-but-
uncorrelated Maps-crash-on-tap). 99 `handle_user_abort` hits, all ordinary/
benign dyld lazy-binding page faults (same signature already characterized
in the `agx_system_metal_test` investigation below), spread across what's
almost certainly Maps.app's own real startup work (fault addresses cluster
in a handful of distinct private-mapping ranges, consistent with the app
image + its own dylibs loading) — i.e. **Maps.app genuinely launched and
ran real code this time, didn't crash, and never once called
`MTLCreateSystemDefaultDevice()`.**

**A real, useful negative finding, not a null result**: tapping the Maps
app icon (launching the full app to its default view) is *not* the same
trigger as the widget's snapshot-refresh cycle. The two already-documented
historical dmesg hits were specifically from
`com.apple.MapKit.SnapshotService.xpc` — a distinct, on-demand XPC service
MapKit spins up specifically to render a map snapshot image (for the
Today-View widget), not something the main Maps.app process does merely by
launching to its default view. This may also explain why the earlier
session's Maps-crash-on-tap didn't recur here: plausibly tied to a
*specific* in-app action (e.g. panning/searching, which would need to
actually fetch/render live map tiles) rather than simple app launch — this
session's tap never went further than the default launch screen.

**Narrows the next step to the doc's own already-identified "most
reliable" option**: since neither passive widget-timing windows nor an
active full-app-launch tap reach the target code path, and MapKit's own
snapshot XPC service is specifically what's needed, the highest-confidence
remaining path is next-step 2 from the section above — a small, dedicated
CI-built test app that calls the real public `MKMapSnapshotter` API
directly, guaranteeing the exact trigger on demand instead of depending on
SpringBoard's widget-scheduling nondeterminism or guessing at in-app UI
gestures.

Environment left clean by the script's own `finally` block: all 18
breakpoints removed, QMP `cont` issued and confirmed (`RESUME` event seen),
no dangling paused state.

### UPDATE 2026-07-31 (new session): MKMapSnapshotter direct-trigger test
### built, deployed, and run — negative result, but a real and useful one:
### the binary crashes before `main()`, matching (and independently
### corroborating) the still-unsolved `agx_system_metal_test` pre-`main()`
### dyld-bootstrap crash, via a completely different, non-Metal-linking
### framework

Direct follow-up to this section's own "most reliable" next step:
`src/userspace_test/mapkit_snapshotter_test.m` (new file) constructs an
`MKMapSnapshotOptions` (plain struct literals throughout — deliberately
avoids `CLLocationCoordinate2DMake`/`MKCoordinateRegionMake`/`CGSizeMake` so
the link line stays minimal, `-framework Foundation -framework MapKit`
only, per this task's own instruction), builds an `MKMapSnapshotter`, and
calls `startWithQueue:completionHandler:` on a GCD **global concurrent**
queue (not the plain `startWithCompletionHandler:` main-queue variant —
this binary has no run loop pump, so blocking the calling thread on a
semaphore while the completion handler also needed that same blocked
thread's queue would deadlock; a global queue is serviced by GCD's own
thread pool independently of any run loop, sidestepping that entirely).
Bounded 120s `dispatch_semaphore_wait`. `MTrace()`-style tracing to
`/tmp/mapkit_test.log` at every stage, identical idiom to
`agx_system_metal_test.m`'s own helper, precisely because (per that
investigation) this exact class of test can be killed/crash with zero
stdout output.

**CI**: new step added to the `agx-bridge-dylib` job in
`.github/workflows/build.yml` (same `clang -target arm64e-apple-ios14.0
-isysroot "$SDK" -fobjc-arc -framework Foundation -framework MapKit ...`
shape as every other step in that job). Pushed (commit `0529fe4`), CI run
`30608243614` succeeded — **`-framework MapKit` links cleanly on this SDK,
no issues**, a new, previously-untested data point for this project.
Downloaded the `agx-bridge-dylib` artifact: `mapkit_snapshotter_test`,
68480 bytes.

**New gotcha found during deployment, worth correcting in this doc's own
playbook**: transferred to `/tmp/mapkit_test` first, per this task's own
literal instruction and per the existing "Playbook: running new test
logic" text ("`/tmp` is fine for a plain executable"). **That turned out to
be wrong for this binary, on this exact live boot** — running it (whether
via `nohup .../mapkit_test &` or plain foreground `/tmp/mapkit_test`)
produced a real, new, previously-undocumented denial:
```
System Policy: nohup(1387) deny(1) process-exec* /private/var/tmp/mapkit_test
System Policy: bash(1402) deny(1) process-exec* /private/var/tmp/mapkit_test
process-exec denied while updating label
Sandbox: hook..execve() killing <unsigned>[pid=1402, uid=0]: (err=1) failed to apply exec policy
```
This is a **genuinely different** Sandbox.kext check from all five
already-patched SIGKILL gates (those are codesigning/entitlement checks,
content- and mostly path-independent; this one is specifically
`process-exec*` gated on the `/private/var/tmp` path) — i.e. a `/tmp`-is-
not-executable-for-a-brand-new-process restriction, the same *class* of
restriction (though a different, execve-specific check) as the
already-documented `enable -f /tmp/....dylib` mmap-block for the
bash-builtin route. **Fix, same shape as that earlier one**: `cp` the
binary from `/tmp` to `/` guest-side (cheap, no re-transfer needed) before
running it — confirmed this works (`/mapkit_test`, direct foreground exec,
no policy-deny message at all afterward). Every previously-confirmed-
working test binary in this project's history (`/sigkill_test`,
`/compute_test`, `/draw_test`, `/agx_system_metal_test`, ...) was, on
inspection, actually always deployed to `/` already — the "`/tmp` is fine"
playbook text was an untested assumption, now empirically falsified for at
least this binary/boot. **Playbook corrected below.**

**The test binary itself crashes, reliably and reproducibly, via plain
`SIGSEGV` — not a sandbox/AMFI policy kill.** `/mapkit_test` (root path):
`Segmentation fault: 11` (exit 139), reproduced twice via direct,
un-instrumented `execve()`. Critically, **`/tmp/mapkit_test.log` — written
by `MTrace()` as the literal first statement inside `main()`'s
`@autoreleasepool` block, before touching MapKit at all — is never
created**, meaning execution never reaches the first line of `main()`.
This is **exactly** the same signature already fully characterized in the
"`agx_system_metal_test` crash investigation" section below: a pre-`main()`
crash, not a `main()`-body bug. `dmesg` confirmed no AMFI/Sandbox kill
message for either crashing run either (genuine `SIGSEGV`, not a disguised
policy kill).

**This is a materially new, useful data point for that OTHER,
still-unsolved investigation, found as a byproduct of this one.**
`mapkit_snapshotter_test` links `-framework MapKit` and does **not** link
`-framework Metal` at all, and never references `MTLCreateSystemDefaultDevice`
(directly or via `dlsym`) anywhere in its source — i.e. it has none of the
properties the leading hypothesis in that investigation was originally built
around (eager/lazy binding of that one specific Metal C symbol; that
hypothesis was already disproven by the dlsym experiment, see that
section's own dated updates). **A second, structurally unrelated binary,
linking a completely different large framework, crashes the identical
way**: pre-`main()`, zero dmesg policy message, `SIGSEGV`. This further
generalizes the mystery away from "something Metal/`MTLCreateSystemDefault
Device`-specific" toward "something about dyld's own bootstrap handling of
a sufficiently large/complex framework dependency closure on this specific
kernel/DSC build" — consistent with, and now independently corroborating,
that section's own "the crash is dyld's own bootstrap/loader machinery"
conclusion.

**Live capture, three GDB-armed windows, all using
`guest_tools/run_mapkit_test_watch.py` (new file, direct sibling of
`tap_maps_watch.py` — same 10 sandbox candidates + 6-address block_invoke
chain + `handle_user_abort`/`exception_triage`, same SMP-safety rules, same
`finally`/`qmp_cont()` discipline, trigger swapped from a QMP tap to
running the test binary over a second, separate serial connection — see
that file's own docstring) plus a leaner 2-breakpoint variant
(`guest_tools/run_mapkit_test_abort_only.py`, new file, `handle_user_abort`
+ `exception_triage` only, written specifically to reduce GDB-breakpoint-
induced dilation for a tighter trigger-to-fault correlation)**:

1. **240s window, full 18-breakpoint set, triggered via the (at-the-time-
   still-broken) `/tmp` path.** Zero sandbox denies, zero block_invoke
   hits, zero triage hits, 215 `handle_user_abort` hits — **all** traced to
   just 2 recurring `state` pointers (i.e. ordinary background lazy-binding
   activity from already-running processes, not our own trigger, since the
   `/tmp` exec was denied before the binary ever ran). Uninformative for
   the crash itself, but confirms the sandbox/chain breakpoints are correct
   and armed cleanly.
2. **100s window, full 18-breakpoint set, corrected `/` path.** Zero
   sandbox denies, zero chain hits, zero triage hits, 30 `handle_user_abort`
   hits, again all attributable to only 2 recurring `state` pointers.
   Post-hoc guest-side check (`/tmp/mapkit_test_stdout.log` freshly emptied
   by the trigger's own `rm -f` + redirect, `/tmp/mapkit_test.log` still
   absent) **confirmed the trigger command genuinely did execute and crash
   during this window** — the crash's own fault event simply wasn't caught
   live. Root-caused: this project's own already-documented GDB-breakpoint-
   induced dilation (see the section below, "three separate observation
   windows...") applies not just to guest monotonic-time advancement during
   a long passive wait, but — worse, and not previously characterized this
   precisely — to **QEMU's host-side I/O-loop responsiveness for the
   *other*, non-debug serial chardev** (port 4444) while the gdbstub
   connection (port 1234) is busy: a standalone, no-GDB-attached control
   test of the exact same trigger command completed in **0.17s** wall-clock
   (confirmed directly, see below); under 18 live breakpoints the same
   command's guest-side effects were still arriving well past this run's
   own 100s window.
3. **60s window, abort+triage only (2 breakpoints).** 31 hits, only 2
   unique `state` pointers — still no isolated, one-off hit attributable to
   a freshly-crashed process.
4. **200s window, abort+triage only.** 166 hits, **5** unique `state`
   pointers — four recurring (8-63 hits each, ordinary background
   activity), and **one single-occurrence hit**: `t=163.8s`,
   `state=0xffffffe19cc25090`, **`pc=0x18d40b24c`**,
   **`far=0x16d6d3f10`** (the faulting address), `esr=0x92000047` (EL0 data
   abort, `ESR_EC=0x24`, `DFSC=0x07` = translation fault at level 3),
   never repeated for the rest of the window (consistent with the thread
   dying on this exact fault, matching a freshly-exec'd, immediately-
   crashing process's expected one-shot signature). **`0x18d40b24c` falls
   squarely inside the exact `~0x184000000`–`~0x1eeffffff` address range
   family already identified, across three separate runs, as dyld's own
   privately-mapped code** in the "`agx_system_metal_test` crash
   investigation" section below (not the app's own binary mapping, not the
   fixed system-wide dyld_shared_cache range). Given a freshly-relaunched
   dyld gets a fresh ASLR slide on every single `execve()` (already
   established in that section, from `mach_loader.c`'s
   `dyld_aslr_page_offset` handling), an exact address match across
   different binaries/runs was never expected — landing in the *same
   family of ranges* is exactly the right signature to expect if this is
   genuinely the same underlying dyld-bootstrap fault class, and this
   result delivers precisely that.

**Standalone (no GDB) control test, for the dilation claim above**: same
exact trigger command (`rm -f ...; /mapkit_test > .../stdout.log 2>&1; echo
TRIGGER_EXIT_$?`) sent over a fresh, GDB-free serial connection completed
in 0.17s wall-clock (`Segmentation fault: 11` at +0.16s, `TRIGGER_EXIT_139`
at +0.17s) — directly demonstrating the crash itself is fast/deterministic,
and that the difficulty correlating it live is purely a GDB-overhead
artifact, not evidence the crash is somehow rare, timing-sensitive, or
different when instrumented.

**Bonus, unplanned finding — the passive/organic signal from the earlier
investigation is still real on a long-running boot.** `dmesg`, scanned
after this session's live-capture work, showed a **third** natural,
uninitiated MapKit sandbox-deny event (`com.apple.MapKit(1470) deny(1)
file-read-{metadata,data} /b`, at guest uptime `[11695.29...]`) —
completely independent of this session's own `/mapkit_test` triggers
(different pid, and it landed during the abort-only run 4 above, which
didn't have the 10 sandbox candidates armed at all). Confirms the
originally-observed passive signal (pids 363/364) is not a one-off
artifact of one specific boot — it recurs on a sufficiently long-running
boot (this one has been up since ~07:46, well over 3 hours by this point).
**Not chased further this session** — no live register data was captured
for it (wrong breakpoint set armed at the time), and re-arming to catch a
fourth occurrence live would mean returning to the exact passive-timing
approach this whole task was assigned specifically to route around; out of
this session's scope, noted here only as corroborating evidence that the
underlying phenomenon (and the still-open sandbox-deny hunt) remains real
and worth solving, independent of this session's own (negative) result.

**Bottom line for this task**: **MKMapSnapshotter, called directly via the
real public API, does NOT reach `MTLCreateSystemDefaultDevice()` either —
not because MapKit's own code doesn't call it (the historical dmesg
evidence already proves it does, from its real XPC service), but because
this test binary never reaches `main()` at all, crashing during dyld's own
process-bootstrap first.** This is a valid, evidence-backed negative
result, not a tooling failure: the crash is reproducible (2/2 direct runs),
zero dmesg policy-kill signature (genuine `SIGSEGV`), zero `/tmp/mapkit_
test.log` (proves pre-`main()`), and one clean, isolated fault-PC capture
landing in the exact address-range family already implicated by the
independent `agx_system_metal_test` investigation. **No live sandbox-deny
was captured this session** — the 6 precomputed candidate patches from the
static-analysis subsection above remain unapplied, correctly, per this
task's own explicit instruction not to patch blind.

**Concrete next steps for whoever picks this up:**
1. **This session's own finding reframes the open question**: the real
   blocker for a *direct, on-demand, CI-built test binary* reaching this
   code path is no longer "the sandbox denies `/b`" — it's the earlier,
   still-unsolved pre-`main()` dyld-bootstrap crash, now with **two**
   independent corroborating binaries (`agx_system_metal_test`, linking
   Metal; `mapkit_snapshotter_test`, linking MapKit) and PC evidence from
   both landing in the same dyld-private-mapping range family. Solving
   *that* crash (see that section's own "Concrete next steps," especially
   finding `exception_triage`'s real, possibly-inlined call site for an
   unambiguous single-hit capture) would likely unblock **every** future
   test binary that links a nontrivial framework, not just this one —
   probably the single highest-leverage remaining item across both
   investigations.
2. The passive/organic signal (bonus finding above) confirms the original
   target phenomenon is still alive and reproducible given enough boot
   uptime. If the pre-`main()` crash above is fixed first, re-running this
   exact same `mapkit_snapshotter_test`/`run_mapkit_test_watch.py` pair
   would then be the cheapest possible way to get the live sandbox-deny
   capture this whole investigation has been chasing — the test app, CI
   step, and watch tooling built this session are all already in place and
   need no further changes for that.
3. Playbook correction (applied below): transfer new test binaries to `/`
   directly, not `/tmp`, given this session's live-confirmed `process-exec*`
   denial for `/private/var/tmp`.

Environment left clean: QMP `info status` confirmed `running` throughout
(never left paused — every GDB script here used the same `finally`+
`qmp_cont()` discipline as `tap_maps_watch.py`), `/sigkill_test` →
`Segmentation fault: 11` (gate patches intact), `/compute_test` →
`IOServiceOpen succeeded... result = 42 (expect 42)` (guest undisturbed),
`dmesg` scanned for panics/asserts (none — only ordinary `memorystatus:`/
`Sandbox: nehelper`/HID chatter and the bonus MapKit finding above). No
kernel-side files or `patch_kernelcache.py` were touched this session — no
live sandbox-deny was ever captured, so per this task's own instructions,
no patch was applied.

## Widget-hosted Metal compositing design and prototype (2026-07-31)

Direct follow-up to "Concrete next steps... 2" at the end of the App-level
Metal reach investigation above, and item 4 of this project's standing
priority list: design and prototype a real app that gets Metal-rendered
content composited into the live interface by backboardd's existing,
**completely unmodified** compositing logic, via the private
`CAContext`/`hostingChain` mechanism that investigation confirmed is
already live, right now, in this exact build, for real Today-View widget
extensions.

**Hard constraint honored throughout**: this was a source-editing-and-CI-only
session — the guest serial console (4444), QMP socket, and GDB port (1234)
were never touched, per the coordination note that the live guest was in
concurrent, delicate use by the MapKit `/b` investigation the whole time
(confirmed still active: `git log`/`git pull` mid-session showed that
investigation had pushed new commits, up through
`49225ed`, while this session was working — no conflict, since this
section is purely additive and touches no guest state). Everything below
is design + source + a CI compile check only; **nothing in this section has
ever been run on the guest**, and that is explicitly the expected, correct
stopping point per this task's own instructions.

### Design pass

**Starting point, and the one deliberate deviation from the task's own
proposed shape.** The task's own suggested shape was: a process that (a)
creates its own `CAContext`, (b) renders Metal content into an
`IOSurface`-backed `CALayer` within that context, and (c) gets that context
registered into another process's `hostingChain`. Re-reading the prior
investigation's own findings closely enough to actually design against them
(rather than just citing the shape) surfaces an important simplification:
**(a) and (c) don't need to be built at all.** The prior investigation's own
evidence is that `backboardd` never calls `IOSurfaceLookup`/receives a raw
surface handle — the real Today-View widgets it already, provenly hosts get
there via UIKit/PlugInKit's own private extension-hosting machinery, which
*itself* is what creates the `CAContext` and registers it into the host's
`hostingChain`, as an already-existing, completely internal implementation
detail of "being a normal, working `NSExtension`". A brand-new freestanding
process manually poking `CAContext`/`CARenderServer` C functions would be
reinventing (and would first have to fully reverse-engineer, given this
project's own repeated "no `ipsw`/DSC-parser tooling" gap) a large, private,
undocumented protocol that UIKit/PlugInKit already implement correctly and
already exercise live in this exact build. **Given the task's own explicit
license to validate/adjust the proposed shape**, the actual design adopted
here is: replace an already-installed, already-hosted widget `.appex`'s
compiled executable in place with a new binary that is *still a normal,
functioning `NSExtension` principal class* (so all of UIKit/PlugInKit's real
hosting machinery keeps running, untouched, exactly as it already does for
the real widget being replaced) — and the only thing that actually changes
is *what draws into that principal class's view*. This reduces "get Metal
content hosted by backboardd" to "get Metal content into a `CALayer` that's
already part of an already-hosted view hierarchy" — a much smaller, much
more tractable problem, and a better fit for this project's own established
M.O. ("hand-patch/replace the existing thing" rather than "install
something new from scratch").

**Why replace-in-place beats a from-scratch `.appex` install — a stronger
argument than "sidesteps `installd`" alone.** The task prompt already flagged
the obvious reason (no `installd`/`MobileInstallation` provisioning flow has
ever been attempted or built by this project). Designing against this
project's actual, hard-won security-bypass history surfaces a second,
independent, arguably stronger reason: **replacing an already-provisioned
widget's binary in place inherits all 5 of this project's already-proven,
already-permanently-baked SIGKILL-gate patches for free.** Those gates
(`load_machfile`/`cs_process_global_enforcement`, AMFI's two
`cred_label_update_execve` checks, Sandbox.kext's "only launchd is allowed to
spawn untrusted binaries" and "outside of container && !i_can_has_debugger"
checks — see the SIGKILL section below for the full table) were discovered
and patched specifically to let an *unsigned* binary reach `execve()`
successfully — precisely the situation a hand-compiled replacement binary
(no real Apple signing key available) will be in here too. Two of those five
gates are directly, favorably relevant to this exact scenario: gate #3 is
specifically about disallowing untrusted binaries spawned by something other
than `launchd` (already patched to never fire); gate #4 is specifically about
being "outside of container" (already patched to never fire, and, as a
bonus, a widget's own real bundle path — inside its app's real, legitimate
`/private/var/containers/Bundle/Application/<UUID>/...` container — is
arguably *not* "outside of container" in the first place, unlike this
project's existing test binaries which mostly live loose at `/`). A
from-scratch `.appex` install would still need every one of these same five
gates to hold (nothing about installing fresh changes that), so this isn't
an argument against a from-scratch install being *possible* — it's an
argument that replace-in-place has **zero new security-bypass discovery
risk**, reusing exactly the same, already-verified-permanent patch set this
project already depends on for every other unsigned-binary scenario.

**Design choice: `CGImage`-backed `CALayer.contents`, not
`IOSurface`-backed, for this first prototype — a second deliberate deviation
from the task's proposed shape, with reasoning.** Three converging reasons:
1. This project's entire Metal render pipeline is already fundamentally
   CPU-round-trip-based end to end — a synchronous `IOConnectCallStructMethod`
   into the host's Vulkan renderer, then a `getBytes`-style CPU copy back
   into an `NSMutableData` (see `inferno_render_encoder.m`'s `InfernoTexture`/
   `InfernoSendDrawDispatch`). There is no existing GPU-resident buffer this
   project could hand to `IOSurface` zero-copy even if it wanted to — the
   pixels are already plain CPU memory by the time this project's own code
   ever sees them. `IOSurface` would add real implementation risk (pixel
   format/lock/`bytesPerRow` semantics this project has **never once**
   exercised anywhere in its whole test suite — confirmed by the prior
   investigation's own grep) for zero actual benefit at this stage.
2. `CGImage`-backed `CALayer.contents` is 100% public, extremely
   well-trodden CoreGraphics/QuartzCore API — essentially the standard way
   any app puts a bitmap into a layer — with no private-API risk at all.
3. Nothing in the actual success criterion ("Metal-rendered content
   composited into the live interface by backboardd's existing, unmodified
   compositing logic") requires `IOSurface` specifically. Any `CALayer`
   content type that CoreAnimation's own hosting-chain protocol already
   knows how to serialize across the `CAContext` process boundary (which,
   per the prior investigation, is a private mechanism this project has no
   visibility into the internals of anyway) satisfies the criterion
   identically from backboardd's point of view — `IOSurface` was the *prior*
   investigation's own speculative "most realistic-sounding" guess at the
   payload shape, explicitly flagged there as "not gospel, validate/adjust
   as you learn," not a hard requirement.

`IOSurface` remains a reasonable *later* upgrade path if a genuinely
zero-copy, GPU-resident pipeline is ever built (would need `reims-vgpu`'s
output to land directly in an `IOSurface`-backed buffer instead of a CPU
`getBytes`-style copy — a real, separate project, not attempted here).

**A third open design question, surfaced by actually thinking through the
mechanics rather than stopping at the high-level shape**: which widget
*type* is actually running here? The prior investigation's `hostingChain`
evidence is real and convergent, but it implicitly assumes the observed
widgets are the **legacy `NCWidgetProviding`** ("Today Extension") kind —
a real, live-hosted `UIViewController` whose `CALayer` genuinely is
continuously composited via `hostingChain`, matching everything the prior
investigation found. iOS 14 also shipped **WidgetKit**, a fundamentally
different architecture: a `TimelineProvider` returns declarative
(SwiftUI-described) view snapshots, which the *host* renders into bitmaps
itself on some refresh cadence — no live-hosted `CALayer` view, and quite
possibly no `hostingChain` involvement at all for actual pixel delivery
(WidgetKit's own IPC surface wasn't specifically identified in either prior
investigation). If the target widget is actually WidgetKit-based, this
whole "replace the principal class, keep the view live-hosted" approach
would not work as designed and would need real rethinking, not just
adjustment. The one-`.appex`-per-widget process shape actually observed
(`WeatherWidget`, `StocksWidget`, `GeneralMapsWidget`, `PhotosReliveWidget`,
`ScreenTimeWidgetExtension` — each its own separate process) is a real,
if indirect, point in favor of the legacy `NCWidgetProviding` model (which
is exactly one extension per widget; WidgetKit typically hosts *all* of one
app's widgets from a single per-app extension process) — a reasonable
working hypothesis, not a confirmed fact. **Resolving this for certain needs
exactly one live, read-only guest operation** (dumping the target `.appex`'s
`Info.plist` and checking `NSExtensionPointIdentifier` — see "Concrete next
steps" below for the exact command), which this session's hard constraint
correctly forbids doing itself.

**Target selection: deliberately NOT `GeneralMapsWidget`.** `GeneralMapsWidget`
is the widget the concurrent MapKit `/b` sandbox-deny investigation actively
depends on (it's the trigger for `com.apple.MapKit`'s snapshot-render XPC
service) — killing/replacing its binary would directly interfere with that
session's still-in-progress live work. This design instead recommends
**`StocksWidget`** as the primary candidate for whoever does the first live
attempt (uninvolved in any other current investigation, and its normal
function — live stock quotes — has no working backend in this offline QEMU
guest anyway, so replacing its binary loses nothing of value), with
`WeatherWidget`/`PhotosReliveWidget`/`ScreenTimeWidgetExtension` as
uninvolved fallbacks if `StocksWidget` turns out to have some other
complication.

### What was built this session

**`src/userspace_test/inferno_widget_host.m`** — a new, self-contained
Objective-C source file implementing `InfernoWidgetHost`, a `UIViewController`
subclass meant to serve as a replacement principal class for a widget
`.appex`. It deliberately does NOT declare formal `<NCWidgetProviding>`
conformance (that header may not exist in whatever SDK the CI runner's Xcode
ships, since it's been deprecated since iOS 14 — PlugInKit's own dispatch is
`respondsToSelector:`-based, not a static protocol check, so this project's
own established "hand-declare the ABI you need instead of depending on a
maybe-missing header" pattern, already used for `bash_present_builtin.m`'s
hand-declared `struct builtin`, applies directly here too). Structure:
- `-viewDidLoad`: one-time device/pipeline setup (`dlopen("/b")` → `Q()` →
  device → texture → two `MTLLibrary`s → `MTLRenderPipelineState` → command
  queue — same shape as the already-proven `agx_metal_api_draw_test.m`,
  reusing its exact AIR shader text verbatim), then an initial render, then
  starts a repeating 1-second `NSTimer`.
- Each timer tick re-renders (fresh vertex buffer, encoder, draw, commit,
  `waitUntilCompleted`, `getBytes`) with the triangle's horizontal position
  animated by a `sin(phase)` term, specifically so a genuinely live/ongoing
  render is visually distinguishable from a static single frame if/when this
  is ever actually screenshotted — directly contrasting with the existing
  on-screen-triangle milestone's post-hoc genpipe-overwrite mechanism (task
  3 of the prior investigation found that mechanism is architecturally a
  dead end for exactly this kind of cooperative, ongoing content; this
  file's whole point is to demonstrate the opposite, cooperative shape).
- Each frame's readback pixels are wrapped in a `CGImageRef`
  (`CGDataProviderCreateWithCFData` + `CGImageCreate`) and assigned directly
  to `self.view.layer.contents` — the one and only point of contact with
  CoreAnimation in the whole file, deliberately never touching `CAContext`/
  `CARenderServer`/`hostingChain` APIs at all, per the design above.
- Hand-implements `widgetPerformUpdateWithCompletionHandler:` and
  `widgetMarginInsetsForProposedMarginInsets:` (the two `NCWidgetProviding`
  selectors most likely to actually be invoked by the host) by selector name
  only, matching real ABI shapes, no header dependency.
- A `WTrace()` diagnostic helper (same rationale/shape as
  `inferno_agx_bridge.m`'s `QTrace`) appends one line per step to
  `/tmp/widget_host_trace.log` via raw POSIX I/O — needed because this
  process's stdout isn't obviously reachable the way a plain `execve()`'d
  test binary's already is, and because if anything about the
  extension-hosting handshake goes wrong, this is likely the only way to
  learn how far execution actually got.
- No `main()` — real Xcode-built App Extension targets have none either;
  the real entry point is Foundation's own exported `NSExtensionMain()`,
  which at runtime reads the hosting bundle's own `Info.plist` to find
  `NSExtensionPrincipalClass` and instantiate it. This file relies on the
  same mechanism rather than reimplementing any part of the PlugInKit
  handshake by hand.

**`.github/workflows/build.yml`** — added a new `widget-host-prototype` job
(same `continue-on-error: true`/artifact-upload conventions as every other
job in this file) compiling `inferno_widget_host.m` against the `iphoneos`
SDK, linking `Foundation`/`UIKit`/`QuartzCore`/`Metal`/`CoreGraphics`, with
`-Wl,-e,_NSExtensionMain` as the entry-point override (the actual linker
flag real Xcode App Extension targets use) — then dumping the resulting
Mach-O's header/load-commands/linked-libraries/symbol-table for inspection,
matching this project's own established "always show the raw evidence, not
just pass/fail" style.

### CI result

**Compiled and linked cleanly, first try, run `30607988494`
(`widget-host-prototype` job).** This is a genuinely useful, concrete signal
given open question #3 above was "does `-Wl,-e,_NSExtensionMain` even
work at all against this exact toolchain" — it does:

- Only one warning, harmless and already understood: an implicit
  `CGImageAlphaInfo`→`CGBitmapInfo` enum conversion at the `CGImageCreate`
  call site (both enums share the same underlying values by design in real
  CoreGraphics — this is the completely standard idiom, the warning is
  purely a strictness note, not a correctness issue). Zero errors, zero
  "Undefined symbols", zero `ld:` failures.
- `otool -hv`: `MH_MAGIC_64 ARM64 E filetype=EXECUTE ncmds=25
  sizeofcmds=2992 flags=NOUNDEFS DYLDLINK TWOLEVEL PIE` — **structurally the
  same shape as every other unsigned test binary already proven to run on
  this guest** (compare to `/sigkill_test`'s already-documented
  `flags=0x200085(NOUNDEFS|DYLDLINK|TWOLEVEL|PIE)` in the SIGKILL section
  below) — `NOUNDEFS` in particular confirms the linker fully resolved every
  symbol reference, including the entry point.
- `otool -l`: a real `LC_MAIN` load command, `entryoff 21092`, immediately
  followed by ordinary `LC_ENCRYPTION_INFO_64`/`LC_LOAD_DYLIB` commands for
  `Foundation`/`UIKit`/`QuartzCore`/`Metal` (and `CoreGraphics`/`CoreFoundation`/
  `libobjc.A.dylib`/`libSystem.B.dylib` further down, not reproduced here) —
  no `LC_UNIXTHREAD` fallback was needed, meaning the linker treated
  `_NSExtensionMain` as a completely normal, valid `LC_MAIN` entry target.
- `nm -m`'s full symbol table (checked directly, not just the `grep`
  convenience check the CI step itself runs) shows
  `(undefined) external _NSExtensionMain (from Foundation)` **twice** (once
  per Objective-C `.m`→object-file compilation unit reference, both
  resolving to the same Foundation import) — i.e. the linker genuinely
  treated it as an ordinary lazily-bound dylib-stub symbol, exactly the
  mechanism real Xcode-built App Extension targets are understood to use.
  Also confirms every `InfernoWidgetHost` method (`viewDidLoad`,
  `inferno_setUpDevice`, `inferno_renderAndPresent`, `inferno_presentPixels:...`,
  `inferno_timerTick:`, `widgetPerformUpdateWithCompletionHandler:`,
  `widgetMarginInsetsForProposedMarginInsets:`, `widgetAllowsEditingForCompactMode`,
  `.cxx_destruct`) and `_OBJC_CLASS_$_InfernoWidgetHost` are present in the
  compiled `__TEXT`/`__DATA,__objc_data` sections, i.e. the ObjC runtime will
  genuinely be able to find and instantiate this class by name at runtime.

**What this does and does not prove.** This confirms the binary is
well-formed or at least well-formed *enough that this project's already
much-more-experienced toolchain (`otool`/`nm`, the same tools used
throughout the SIGKILL investigation) sees nothing wrong with it, and that
the `-e _NSExtensionMain` linking approach this design depends on is real
and reproducible on this exact toolchain, not a dead end. It does **not**
prove the binary will actually get past `execve()` inside a real widget
process's launch context (open question #2 above — untested, needs live
verification), and it does **not** prove PlugInKit will actually treat this
class as a valid, hostable extension once it does run (open question #1
above — depends on facts about the target widget's `Info.plist` this
session could not read). Both remain real, honestly-unresolved unknowns —
this CI result narrows the risk surface by one (linking mechanics) out of
three, not all three.

### Concrete next steps for whoever picks this up (live-testing required —
### out of scope for this session per its own hard constraint)

1. **Cheapest, most information-dense first move, entirely read-only**:
   once the guest is free (not concurrently in use by another investigation),
   dump the target widget's `Info.plist` to learn (a) the real
   `NSExtensionPointIdentifier` — decides whether this whole approach is
   even viable (`com.apple.widget-extension` = yes, legacy `NCWidgetProviding`,
   matches this design; `com.apple.widgetkit-extension` = no, WidgetKit's
   snapshot-rendering architecture would need a fundamentally different
   approach, see the design discussion above) — and (b) confirms the exact
   bundle path/UUID to target. Recommended target: **`StocksWidget`**, not
   `GeneralMapsWidget` (actively used by the concurrent MapKit investigation
   — avoid touching it). Something like (exact tooling TBD by whoever runs
   it — `plutil` may or may not be present on this guest, `cat`/`od` binary-
   plist parsing is the fallback, same spirit as this project's own existing
   `dd`/`od` Mach-O header dumps elsewhere in this doc):
   ```
   find /private/var/containers/Bundle/Application -iname "StocksWidget*"
   plutil -convert xml1 -o - "<found path>/Info.plist"   # or cat + manual bplist parse if plutil is absent
   ```
2. **If (and only if) step 1 confirms `com.apple.widget-extension`**: edit
   this file's `Info.plist` copy (extracted alongside the binary — not yet
   done this session, since it requires the live read from step 1 first) to
   point `NSExtensionPrincipalClass` at `InfernoWidgetHost` (this project's
   own fixed, chosen class name — deliberately NOT trying to discover and
   match Apple's original principal class name, since changing one plist
   string is far simpler and lower-risk than guessing a private name
   correctly). Leave `CFBundleIdentifier`, `NSExtensionPointIdentifier`, and
   everything else in the plist untouched.
3. Transfer `inferno_widget_host` (from the `widget-host-prototype` CI
   artifact) to the guest, replacing the target widget's real
   `CFBundleExecutable` binary in place (same path, same file name) — same
   `guest_tools/transfer_binary3.py` chunked-transfer mechanism already used
   for every other guest-side deployment in this project. Do **not** touch
   the bundle's other files (entitlements/provisioning/other Info.plist
   keys) beyond the one plist edit in step 2.
4. Trigger a re-launch of the widget (e.g. respring / re-open Today View —
   see the MapKit investigation's own notes on `SpringBoard`-restart timing
   gotchas for what to expect/avoid) and check, in order of cost: (a)
   `/tmp/widget_host_trace.log` on the guest for how far `WTrace()` calls got
   (proves whether the process even launched and reached `viewDidLoad` —
   the cheapest possible signal, no GDB needed); (b) `dmesg` for any new
   SIGKILL-gate-shaped denial this project's existing 5 patches don't cover
   (open question #2 above); (c) if both of those look clean, a QMP
   screendump of the Today View to check whether the animated red triangle
   is actually visible and moving — the real, final proof of this whole
   session's design.
5. If step 4 reveals a genuinely new, currently-unpatched security gate
   (plausible, per open question #2), the exact same disassemble-first,
   verify-then-patch methodology already used for all 5 known SIGKILL gates
   (see the SIGKILL section below) applies directly — this would not be a
   new investigation from scratch, just one more application of an already-
   proven technique.



This is the direct answer to the open question this section used to end
with ("not yet live-verified specifically"). Once the 5 SIGKILL gates were
baked permanently into the kernelcache (see the dated update at the end of
the SIGKILL section below) and verified via a real reboot, the CI-built
`agx_system_metal_test` binary (run `30569211270`, `agx-bridge-dylib`
artifact, `out8/agx_system_metal_test`, 68992 bytes) was transferred to the
guest (`/agx_system_metal_test`, chunked transfer, ~885s, size verified
68992==68992) and run via plain direct `execve()` — no bash-builtin, no
GDB attached for the run itself.

**Result: it crashes.** `Segmentation fault: 11` (exit 139), reproduced
twice. Critically, this is **not** one of the known SIGKILL gates — `dmesg`
shows no AMFI/Sandbox kill message at all for either run (unlike the
gate-by-gate `/sigkill_test` history above), confirming this is a genuine
new userspace crash, not a security-policy kill.

**Standard crash reporting is non-functional in this VM environment** for
any process crash (not specific to this test): both `.ips` reports in
`/var/mobile/Library/Logs/CrashReporter/agx_system_metal_test-*.ips` show
`Backtrace not available`, `Binary images description not available`, and
`Error Formulating Crash Report: Failed to create CSSymbolicatorRef -
corpse still valid`, with every register (including `pc`/`lr`) dumped as
literal `0x0`. Given ReportCrash apparently can't symbolicate/read the
corpse of a genuinely-unsigned process at all (plausibly because our own
SIGKILL-gate bypasses skip trust setup that ReportCrash's own corpse-access
path still expects), **this all-zero register dump should NOT be trusted
as the real PC** — it looks like a degraded placeholder, not a real crash
snapshot. Don't use it as evidence of anything; treat it as "ReportCrash is
broken here", full stop.

**Real diagnosis instead came from live kernel-GDB breakpoints** (own
minimal RSP client scripts, following `gdb_rsp2.py`'s pattern, arm
breakpoint → trigger `/agx_system_metal_test` from a separate serial
connection → observe hits, using the documented remove/single-step/
reinsert dance on every hit to avoid the infinite-retrap gotcha):

- Breakpointed the ENTIRE `MTLCreateSystemDefaultDevice` call chain at
  once: the real outer exported function's own entry
  (`_MTLCreateSystemDefaultDevice` at `0x1970505d0`, address from this
  project's own memory notes, originally found via `ipsw dyld symaddr
  --image Metal`), the inner `dispatch_once` block's entry
  (`___MTLCreateSystemDefaultDevice_block_invoke` at `0x1970506e4`), our
  own injected patch body's start (`0x1970506fc`), the two stub call
  targets our patch invokes (`dlopen` stub `0x1970a5cc0`, `dlsym` stub
  `0x1970a5cd0`), and the block's shared epilogue (`0x197050750`).
  **None of these ever fired**, across two separate triggered runs. This
  conclusively rules out our own `block_invoke` patch as the cause — the
  process crashes before Metal.framework's device-creation code, patched
  or not, is ever entered at all.
- Also confirmed via the caller side: `agx_system_metal_test.m`'s own
  `MTrace()` helper writes a line to `/tmp/m_trace.log` as the literal
  first statement inside `main()`'s `@autoreleasepool` block, before
  calling `MTLCreateSystemDefaultDevice()`. **This file is never created**
  (confirmed `/tmp` itself is writable — a plain `echo test > /tmp/probe.txt`
  from the same root shell succeeds fine) — meaning execution never even
  reaches the first line of `main()`, consistent with the breakpoint
  findings above.
- Tested and ruled out a specific alternate hypothesis: that gate #1's
  overly-broad `load_machfile` patch (documented above as "ignores ANY
  `parse_machfile` failure reaching that return point, not just the
  specific `got_code_signatures` gate") might be letting a *genuinely,
  differently* malformed load through for this specific binary, producing
  an incompletely-initialized process. Breakpointed gate #1's own site
  (`0xfffffff007eef788`) during a triggered run: `x0` (parse_machfile's
  real return value) was `0x4` (`LOAD_FAILURE`) — but this is the *exact
  same* value/code path already documented for `/sigkill_test` and,
  necessarily, for every other unsigned binary this patch already handles
  correctly (`compute_test`/`draw_test`/etc. all lack `LC_CODE_SIGNATURE`
  too, so they hit the identical `got_code_signatures` gate and the
  identical forced-success patch). Since the value and code path are
  identical to binaries that DO work, this is not a differentiator — ruled
  out.

**Leading hypothesis (not yet proven at the exact instruction level):**
`agx_system_metal_test.m` is the **first** binary in this project's whole
test history to reference the real exported C symbol
`_MTLCreateSystemDefaultDevice` directly, at compile/link time (`clang
... -framework Metal`, plain function call). Every other test that has
ever been run via direct `execve()`
(`compute_test`/`draw_test`/`agx_functional_test`/`metal_api_test`) instead
obtains the device via a **runtime** `dlopen("/b")` + `dlsym(handle, "Q")`
+ call — confirmed by grepping their sources (e.g.
`src/userspace_test/agx_metal_api_draw_test.m` line 66 area) — which never
requires dyld to bind any Metal.framework C symbol at link time at all;
they only touch Metal.framework's Objective-C classes
(`MTLTextureDescriptor`, `MTLRenderPipelineDescriptor`, etc.), which
resolve via the ObjC runtime's own class-list mechanism, not standard
symbol/PLT-style binding. The likely fault site is therefore inside
**dyld's own process-launch-time symbol binding/resolution** for this one
specific external symbol — not anywhere in Metal.framework's own compiled
code (which the breakpoint sweep above never even reaches), and not
related to our patch.

**Next steps for whoever picks this up:**
1. Find dyld's own lazy/eager-binding entry point's VA (would need `ipsw`
   — not installed on this Linux host this session — or a from-scratch
   DSC export-trie/symbol-table parser) and breakpoint there to catch the
   actual faulting instruction directly, the same two-phase LR-capture
   technique used throughout the SIGKILL investigation would apply
   directly.
2. Faster empirical unblock, no new tooling needed: rewrite
   `agx_system_metal_test.m` to obtain the function pointer via
   `dlsym(RTLD_DEFAULT, "MTLCreateSystemDefaultDevice")` and call through
   that, instead of calling the symbol directly — if this alone fixes the
   crash, it confirms the eager-linktime-binding theory precisely and
   gives a usable pattern for future "prove the real system API surface
   works" tests without waiting on dyld internals to be fully understood.
   (Caveat: this would slightly weaken the "genuinely unmodified calling
   pattern" framing the test was designed around, since a real app would
   just call the C symbol directly — worth keeping both versions once this
   is unblocked, so the direct-symbol-call version stays available as a
   regression check once/if the real root cause is fixed.)
3. Whichever fix lands, re-run via the exact same steps documented here
   (transfer, plain direct exec, check for `Segmentation fault` vs `ALL
   CHECKS PASSED`) — do not assume clean based on a partial pipeline
   change alone, this test's whole point is being the strictest, most
   realistic check in the suite.

QEMU/guest state was left clean after this investigation: all GDB
breakpoints removed, VM confirmed `running` (not paused) via QMP, `dmesg`
scanned for panics (none), and `/compute_test` re-run as a sanity check
(still passes, `result = 42 (expect 42)`) to confirm the GDB session
itself didn't corrupt anything.

### UPDATE 2026-07-30 (later session): fast empirical test (next step #2 above)
### tried and DISPROVEN -- eager-binding-of-the-C-symbol hypothesis is dead

Picked up exactly at "next steps for whoever picks this up" step 2: rewrote
`agx_system_metal_test.m` to resolve the device-creation entry point via
`dlsym(RTLD_DEFAULT, "MTLCreateSystemDefaultDevice")` and call through the
resulting function pointer, instead of calling the real exported C symbol
`_MTLCreateSystemDefaultDevice` directly at compile/link time. Rest of the
test (texture, library x2, function x2, pipeline state, queue, command
buffer, render encoder, draw, commit, readback) kept byte-identical. Per the
task's own explicit instruction, the original direct-symbol-call version was
preserved unchanged (not deleted/overwritten) as
`src/userspace_test/agx_system_metal_test_direct.m`, and both now build in
CI side by side in the `agx-bridge-dylib` job (`.github/workflows/build.yml`,
same `clang -target arm64e-apple-ios14.0 ... -framework Foundation
-framework Metal ...` flags as before, no flag changes needed since dropping
the direct call means the linker never needs to bind that one C symbol at
all -- `-framework Metal` stays linked regardless, since it's still needed
for the ObjC classes/protocols, `#include <dlfcn.h>` added for the new
`dlsym()` call).

Pushed (commit `cc9ad7b8df25febda84c500a803b1b7605c10d8f`), CI run
`30576781147` (`agx-bridge-dylib` job) succeeded, both new/changed compile
steps green. Downloaded the `agx-bridge-dylib` artifact:
`agx_system_metal_test` (the new dlsym variant, 68944 bytes) and
`agx_system_metal_test_direct` (the preserved original, 68992 bytes --
matches the previously-documented direct-call binary's size exactly, good
sanity check that nothing changed in that variant). Transferred the dlsym
variant to the guest (`/agx_system_metal_test`, chunked transfer, 875.7s,
size verified 68944==68944) and ran it via plain direct `execve()`, zero GDB
attached, guest's root `/` already writable from the still-running QEMU
process (no remount needed).

**Result: it crashes identically.** `Segmentation fault: 11` (exit 139) --
the exact same signal, same exit code, as the original direct-call version.
`/tmp/m_trace.log` (written as the literal first statement inside `main()`,
before even the `dlsym()` call) is **still never created**, meaning
execution never reaches the first line of `main()` **even with the direct
C-symbol reference removed entirely**. `dmesg` (`grep -iE
'agx_system|amfi|sandbox.*execve|killing pid'`) shows zero matches for this
run, confirming (same as the original investigation) this is a genuine
SIGSEGV, not a disguised SIGKILL-gate-style policy kill.

**This conclusively disproves the eager-binding-of-that-one-C-symbol
hypothesis.** The dlsym-based binary contains no direct link-time reference
to `_MTLCreateSystemDefaultDevice` anywhere in its compiled object code --
there is nothing left for dyld to eagerly (or lazily) bind for that symbol
at process-launch time -- yet the crash is byte-for-byte the same failure
signature, still happening before `main()` is ever entered. Since `main()`
is never reached in *either* variant, the crash cannot be caused by
anything `main()`'s body does: not the device-acquisition strategy (direct
call vs. `dlsym(RTLD_DEFAULT, ...)`), not the `MTrace()` file-logging helper
(never called), not the render pipeline logic -- none of that code ever
executes. Whatever is faulting is happening earlier than any source-level
difference between the two variants of this file.

**Follow-up diff against the known-passing `agx_metal_api_draw_test.m`**
(per the task's own suggested comparison): the two files are structurally
near-identical -- same object graph end to end (`MTLTextureDescriptor`,
`MTLRenderPipelineDescriptor`, `MTLRenderPassDescriptor`, same protocols,
same two AIR shaders, same `-framework Foundation -framework Metal` compile
flags). The only remaining source-level differences are (a)
`agx_system_metal_test.m`'s extra `fcntl.h`/`unistd.h`/`string.h` includes
and `MTrace()` helper -- irrelevant, since that code is never reached in the
crashing binary -- and (b) the passing test always calls `dlopen("/b",
RTLD_NOW)` near the top of `main()` while both variants of the failing test
do not -- also irrelevant on its face, since this too is `main()`-body code
that never executes before the crash. Neither identified difference can
explain a **pre-main()** crash. This means the actual differentiator is
something at the compiled-Mach-O/dyld-processing level not yet identified
by source inspection alone (e.g. total binary layout, load-command
count/ordering/size, or some codegen difference not visible in the ObjC
source) -- genuinely a different, deeper question than "which symbol does
this call directly," and per this task's own instructions this is the
right point to stop rather than keep guessing blindly.

**Environment left clean, same as before**: this session never touched GDB
at all (debug port 1234 unused throughout -- the whole test was a
rebuild+retransfer+direct-exec cycle). `dmesg` scanned for panics/asserts
(none), QMP `info status` confirmed `running` (not paused, expected since
no GDB session ever attached), and `/compute_test` re-run as a sanity check
(still passes, `IOServiceOpen succeeded`, `result = 42 (expect 42)`, exit
0) to confirm nothing in the guest was disturbed by this investigation.

**Next steps for whoever picks this up next** (step 2 above is now closed
out; step 1 remains the live option, plus a new one this session's finding
suggests):
1. (Unchanged from before) Find dyld's own lazy/eager-binding entry point's
   VA and breakpoint there directly -- though this session's finding makes
   this less likely to be fruitful on its own, since there's no longer a
   specific symbol-binding call to catch in the dlsym variant, and it
   crashes the same way regardless.
2. **New, better-targeted idea given this session's finding**: breakpoint
   the binary's own real Mach-O entry point (`LC_MAIN` `entryoff`, `0x4000`
   for these binaries per the load-command dump already in the SIGKILL
   section above, i.e. VA = wherever `__TEXT` gets mapped + `0x4000`) to
   determine whether the crash happens *before* dyld ever hands control to
   this image's own code at all, or *after* (e.g. inside this binary's own
   C runtime startup / ObjC `+load`/static-initializer machinery, before
   reaching hand-written `main()`). This would cleanly split the search
   space into "purely a dyld-internal fault, unrelated to this binary's own
   code" vs. "something in how this specific translation unit's startup
   code is structured/ordered." Not attempted this session per the task's
   explicit instruction to stop and report once the fast-empirical
   hypothesis was disproven, rather than open a new live-debugging
   investigation unbounded.
3. A structural (not just source-text) diff of the compiled Mach-O headers/
   load commands between `agx_system_metal_test` (either variant) and
   `agx_metal_api_draw_test` -- e.g. via a small from-scratch Mach-O header
   parser (this project already has the pieces: `resolve.py`/
   `patch_kernelcache.py`'s segment-walk logic, `off2va.py` from the SIGKILL
   investigation) -- would be a cheap, no-GDB-needed way to narrow down
   *what* about this binary's compiled structure differs, before spending a
   GDB session on step 2.

### UPDATE 2026-07-30 (later session): Mach-O diff done (Step 2, conclusive
### no-differentiator), and the crash is now localized to dyld's own
### userspace bootstrap code, not this binary's own code or link deps

Picked up both remaining next steps from the previous update. Both binaries
plus the known-passing comparison binary were already available locally
(no CI wait needed) from the prior session's downloads: `agx_ci_dl/
agx_system_metal_test` (68944B, dlsym variant), `agx_system_metal_test_direct`
(68992B), `agx_metal_api_draw_test` (68816B), plus `agx_metal_api_compute_test`/
`agx_functional_test`/`agx_introspect` as extra known-good baselines.

**Step 2 (Mach-O structural diff): done, conclusive, no differentiator
found.** Wrote a minimal hand-rolled `mach_header_64`/load-command walker
(no `otool` on this Linux host -- same spirit as this project's existing
`resolve.py`/`parse_obj.py`) and dumped every load command for all five
binaries. Result: **completely structurally uniform** across crashing and
passing binaries alike --
- Identical `LC_LOAD_DYLIB` list, exact same 5 entries in the exact same
  order and versions in both `agx_system_metal_test` and
  `agx_metal_api_draw_test`: `Foundation` (cur=0x9c70100), `Metal`
  (cur=0x1571300), `libobjc.A.dylib` (cur=0xe40000), `libSystem.B.dylib`
  (cur=0x5417802), `CoreFoundation` (cur=0x9c70100). No `LC_LOAD_WEAK_DYLIB`,
  no `LC_REEXPORT_DYLIB`, no `LC_RPATH`, no `LC_LINKER_OPTION` in either.
- Identical segment layout shape: `__PAGEZERO`/`__TEXT`/`__DATA_CONST`/
  `__DATA`/`__LINKEDIT`, same `vmaddr`/`maxprot`/`initprot` for every
  segment, same section *names* in the same order (only sizes/offsets
  differ, tracking the small code-size delta from the extra
  `fcntl.h`/`unistd.h`/`MTrace()` helper -- already known to be
  never-reached, dead-before-crash code).
- Identical `LC_MAIN entryoff=0x4000` in both. Neither has
  `LC_CODE_SIGNATURE` (already established). `ncmds=22 sizeofcmds=2096` in
  both (`agx_metal_api_compute_test`/`agx_functional_test`/`agx_introspect`
  have slightly smaller `ncmds`/`sizeofcmds` purely because they don't link
  `CoreFoundation`, consistent with their simpler compute-only source).
- Compile commands in `.github/workflows/build.yml` are **byte-identical
  text** for all of them: `clang -target arm64e-apple-ios14.0 -isysroot
  "$SDK" -fobjc-arc -framework Foundation -framework Metal [-framework
  IOKit] -o ...`. No `-weak_framework` anywhere in the whole file.

**This also directly kills the task's own "-weak_framework Metal" theory,
without even needing to build the experimental variant it suggested**:
`agx_metal_api_compute_test` -- one of the four binaries already proven to
run to completion via plain `execve()` with fully correct results (see the
gates-#2-#5 update above, `IOServiceOpen succeeded... result = 42`) --
**also hard-links `-framework Metal`** in exactly the same way as the
crashing binary. A hard hard-link to `Metal.framework` cannot be the
differentiator when a binary with the identical hard link demonstrably
works. No weak-framework experiment was run since the counter-example
already disproves the premise.

**New source-level finding that reframes Step 1 entirely.** Fetched
`osfmk/kern/mach_loader.c` from `apple-oss-distributions/xnu` tag
`xnu-7195.50.7.100.1` (this project's own already-established
closest-available match for this exact kernel build -- same tag the SIGKILL
investigation above used throughout) via `gh api search/code` +
`raw.githubusercontent.com` fetches (this project's own local copy of this
same file, saved to scratch by an earlier session, was reused first and
cross-checked). `load_main()` -- the LC_MAIN load-command handler, which is
what every binary in this project's test suite has (none use
`LC_UNIXTHREAD`) -- contains this, verbatim:

```c
if (result->using_lcmain || result->entry_point != MACH_VM_MIN_ADDRESS) {
        /* Already processed LC_MAIN or LC_UNIXTHREAD */
        return LOAD_FAILURE;
}

/* kernel does *not* use entryoff from LC_MAIN.  Dyld uses it. */
result->needs_dynlinker = TRUE;
result->using_lcmain = TRUE;
```

**The kernel never computes or sets the initial user-thread PC to the main
executable's own entry point at all**, for any LC_MAIN binary. It sets
`needs_dynlinker=TRUE`, and elsewhere (`load_dylinker()`, called from
`parse_machfile()`'s own recursive-invocation path for `LC_LOAD_DYLINKER`
-- the same inlined recursion this project's SIGKILL investigation flagged
as "made isolating the one check from its neighbors nontrivial by hand")
the kernel loads `/usr/lib/dyld` itself as a second, nested Mach-O and it is
**dyld's own entry point** that ends up as the thread's initial PC. Dyld
itself, entirely in userspace, resolves the real app's mapped location and
jumps to *its* LC_MAIN entry only as the very last step of its own
bootstrap.

This means the task's literally-proposed Step 1 ("breakpoint the app's own
LC_MAIN entryoff VA") **cannot discriminate working vs. crashing binaries
even in principle**: the kernel-committed initial PC is dyld's entry either
way, identically, for every dynamically-linked binary in this whole
project. A breakpoint there would fire the same for `agx_metal_api_draw_test`
and `agx_system_metal_test` alike -- it only proves dyld gets control at
all, which was never in question. Also confirmed from the same source read:
`load_machfile()` computes the app's own slide (`aslr_page_offset`) and
dyld's own, fully independent slide (`dyld_aslr_page_offset`) via
**genuine, freshly-randomized `random()` calls on every single `execve()`**
(`mach_loader.c` ~line 531) -- ruling out any fixed/precomputed breakpoint
address for "the app's own mapped entry" a priori; it would need to be
captured live, per-run, same as everything else in this investigation.

**Live kernel-GDB fault capture (the real substitute for literal Step 1).**
Since the kernel-committed PC can't discriminate, and dyld's own userspace
code is exactly where the mach_loader.c finding points, the practical
question became "where is the CPU when the fault actually happens" --
answered directly by breakpointing the ARM64 EL0-abort path instead of
guessing addresses. `osfmk/arm64/sleh.c` (same xnu tag, fetched the same
way) shows `sleh_synchronous(arm_context_t *context, uint32_t esr,
vm_offset_t far)` dispatches per `ESR_EC(esr)` class; breakpointing it
directly is far too hot (fires on literally every syscall, `ESR_EC_SVC_64`
included) to be practical with a hand-rolled RSP client. Its EL0-abort-only
callee is a **much** better target: `_handle_user_abort`
(`0xfffffff007b574f0` -- found via `kernel-symbols.txt`, confirmed unique),
called only for genuine EL0 data/instruction aborts, signature `static void
handle_user_abort(arm_saved_state_t *state, uint32_t esr, vm_offset_t
fault_addr, fault_status_t fault_code, vm_prot_t fault_type, vm_offset_t
recover, expected_fault_handler_t expected_fault_handler)` -- at entry,
AAPCS64 puts `state`/`esr`/`fault_addr`/`fault_code`/`fault_type` directly
in `x0`-`x4`, no memory read needed for those. The one thing worth reading
out of memory is the *real* faulting PC, inside `*state`: `struct
arm_saved_state { arm_state_hdr_t ash /* 8B: flavor,count */; union {
ss_32; ss_64; } uss; }`, and for arm64 `struct arm_saved_state64 { uint64_t
x[29]; fp; lr; sp; pc; uint32_t cpsr,reserved; uint64_t far; uint32_t
esr,exception; }` (fetched from `osfmk/mach/arm/thread_status.h`, same
tag) -- giving, relative to the `state` pointer in `x0`: `pc @ +0x108`,
`cpsr @ +0x110`, `far @ +0x118`, `esr @ +0x120`. Every single hit across
every run this session cross-checked `state->far == x2` (the fault_addr
argument register) and `state->esr == x1` (the esr argument register)
**exactly**, on every hit with no exceptions -- strong live confirmation
the offset math and the live-register interpretation are both correct.

Armed this breakpoint, triggered `/agx_system_metal_test` via a separate
serial connection (same two-phase pattern as the SIGKILL investigation),
and captured live fault data across three separate sessions/execs (two
inadvertently required a full QEMU restart in between -- see the
methodology-gotcha paragraph below). **Consistent finding across all
three**: fault PCs immediately following the trigger cluster tightly in a
`~0x184000000`-`~0x1eeffffff`-ish range that is (a) **not** the app
binary's own private mapping (independently visible in the same sessions:
an unrelated long-lived background process's lazy-binding activity
consistently lands around `~0x100000000-0x102000000`, self-consistently
tracked across many hits from a single stable `state` pointer), and (b)
**different in each of the three separate runs** (`~0x18489508c` in run 1;
a single hit at `~0x1a7dd9684` in run 2 before it was cut short; a whole
cluster spanning `~0x1b12d1490`-`~0x1eea4827c` in run 3). This
run-to-run variation is *itself* a positive confirmation of the
`mach_loader.c` finding above: it is exactly what independently-re-randomized
per-exec ASLR for dyld's own private mapping (`dyld_aslr_page_offset`,
separate from the shared cache's own fixed system-wide base) predicts. The
converging picture: **the fault happens inside dyld's own privately-mapped
code, not inside `agx_system_metal_test`'s own compiled TEXT, and not (based
on the address ranges) inside the fixed, system-wide dyld_shared_cache
mapping either** -- consistent with, and reinforcing, "the crash is dyld's
own bootstrap/loader machinery, before it ever hands control to this
image's own code."

**Honest caveat -- this is strong circumstantial evidence, not a single
unambiguous smoking-gun hit.** A second breakpoint on `_exception_triage`
(`0xfffffff007a2c850`, xnu's Mach-exception-escalation entry point --
should fire only when a fault is genuinely unresolvable, not for the many
benign/resolved lazy-binding page-ins that also route through
`handle_user_abort`) was armed alongside `handle_user_abort` specifically
to pinpoint the one truly-fatal hit per trigger. **It never fired, in any
of the three runs**, despite `agx_system_metal_test` visibly crashing each
time (independently reconfirmed via plain `execve()`,
`Segmentation fault: 11`, matching every prior session). Given this
project's own SIGKILL investigation already hit the exact same class of
surprise once (`_cs_process_global_enforcement`'s trivial `return 1;` body
got constant-folded into its ~4 call sites at compile time, "presumably
built with LTO/whole-module optimization," making a breakpoint on the
*symbol* never fire even though the logic still ran), the most likely
explanation is that `exception_triage` is similarly inlined into its
caller(s) in this specific compiled kernel, not that the crash bypasses
Mach-exception delivery entirely. Not confirmed further this session --
would need the same disassemble-the-caller-and-find-the-real-call-site
technique used for the SIGKILL gates. Because of this, the ~86 `abort` hits
captured per run span up to 27 distinct concurrent kernel-thread contexts
(this guest boots several widget-host processes --
`WeatherWidget`/`GeneralMapsWidget`/`com.apple.mobilenotes.WidgetExte` --
confirmed via `dmesg`'s `memorystatus:` chatter, not a symptom of anything
wrong), and no single hit could be individually, unambiguously proven to be
*the* fatal one via a clean `exception_triage` correlation the way earlier
SIGKILL gates were pinned down. The PC-range convergence across three
independent runs is the strongest evidence in hand, not a single definitive
capture.

**Methodology gotcha hit twice this session, root-caused and fixed --
worth recording precisely for whoever debugs multi-vCPU SMP targets here
next.** QEMU's system-mode gdbstub for this 7-vCPU (`-smp 7`) target runs
in all-stop mode: when *any* vCPU hits a breakpoint, the whole VM halts,
but a `g` (read registers) call without explicit `Hg`/`Hc` thread-scoping
can return **a different, unrelated vCPU's live PC** on any given stop --
not necessarily the one that actually hit a known breakpoint. The first
version of this session's capture script treated *every* stop uniformly
(`z0,pc,4` / `s` / `Z0,pc,4` at whatever PC was read back), which is safe
for a *recognized* breakpoint hit but actively harmful for an
unrecognized one: `Z0` unconditionally **plants a brand-new real
breakpoint** at that essentially-arbitrary observed PC. Across a busy
multi-core idle system this snowballs fast (dozens of spurious breakpoints
within seconds), and ultimately wedged the VM into the exact
`paused (debug)`-that-`cont`-can't-clear state this doc's SIGKILL section
already warned about -- **twice** this session, each requiring the
documented kill+relaunch recovery (both were clean: no panics, `dmesg`
otherwise normal, and critically **both the file-baked SIGKILL-gate patches
and the disk-resident `block_invoke` DSC patch survived intact** --
confirmed via `/sigkill_test` showing `Segmentation fault: 11`, not
`Killed: 9`, immediately after each restart, no live-patch re-application
needed). **The fix**: only ever touch (`z0`/`s`/`Z0`) breakpoint addresses
you yourself explicitly set; for any stop at an unrecognized PC, just call
`c` again and leave it alone entirely -- don't try to "clean up" or
"step past" a PC you don't own, since on an SMP all-stop target that PC may
not even belong to a real breakpoint at all. Also worth pre-clearing
proactively in any future session on this VM: `_arm64_retention_wfi`
(`0xfffffff008125a10`), the CPU idle-loop entry, which had a stale
breakpoint armed from some earlier, unrelated session and is hot enough
(every idle core re-enters it constantly) to single-handedly starve a naive
capture loop of its entire time budget if not cleared first.

**No fix produced this session.** Unlike the dlsym-vs-direct-call
experiment (a fast, cheap, fully-testable hypothesis) or a hypothetical
`-weak_framework` swap (already disproven above without needing to build
it), "dyld's own bootstrap logic faults while loading this specific image"
doesn't have an equally cheap userspace-only knob to flip -- a real fix
would need either finding and patching the exact faulting dyld instruction
(would need dyld symbols for whichever private slide it lands at each run
-- `ipsw` still not installed on this Linux host, and this project's local
`dyld_shared_cache_arm64e.a2s` is `ipsw`'s own undocumented binary
address-to-symbol cache format, not a plain symbol table, so not usable
without writing a real parser for that format specifically), or a
structural change to how this binary is linked/loaded that avoids whatever
dyld is choking on (not yet identified, since Step 2 found no structural
Mach-O differentiator at all).

**Concrete next steps for whoever picks this up:**
1. Redo the `handle_user_abort` capture with proper GDB thread-scoping
   (parse the `T05thread:NN;` field out of every stop reply, send an
   explicit `Hg<NN>`/`Hc<NN>` before `g`/`s`) so every read is guaranteed to
   belong to the vCPU that actually hit the breakpoint -- would make the
   per-run PC data fully trustworthy per-hit instead of only
   trustworthy-in-aggregate/by-convergence, and would let the "don't touch
   unrecognized PCs" safety fix above be relaxed back to something closer
   to the original per-hit remove/step/reinsert dance without the
   SMP-misattribution risk that caused the two stuck-VM restarts.
2. Find `exception_triage`'s real call site(s) in the compiled kernel (it's
   likely inlined into one or more of its callers, same class of surprise
   as `_cs_process_global_enforcement` in the SIGKILL investigation) --
   disassemble `handle_user_abort`'s own compiled tail (already
   breakpointable, hits confirmed) looking for whatever it actually branches
   to on the "fault could not be resolved" path, then breakpoint *that*
   directly instead of the (possibly-inlined-away) standalone symbol. This
   would finally let a single hit be proven unambiguously fatal, tightening
   "strong circumstantial convergence" into a definitive single capture.
3. Once a specific dyld-side faulting PC is captured with full confidence,
   read its own private mapping's file (would need to identify which file
   backs that region -- almost certainly `/usr/lib/dyld` given the
   `mach_loader.c` finding, or dyld-in-cache if this build embeds it) at
   `pc - runtime_slide` to see what instruction/data reference is actually
   faulting, which would finally explain *why* (e.g. a bad export-trie
   lookup, a missing shared-cache mapping, a bug specific to this
   project's custom kernel/DSC patches interacting badly with one specific
   binary's dependency-resolution order).

Environment left clean: `dmesg` scanned after the final restart (only
ordinary `memorystatus:`/`Sandbox: nehelper deny`/HID-reporter chatter, zero
panics/asserts), QMP `info status` confirmed `running` (not paused),
`/sigkill_test` confirmed still `Segmentation fault: 11` (patches intact),
`/compute_test` re-run as the standard sanity check (`IOServiceOpen
succeeded, connection=0x140b` / `result = 42 (expect 42)`, exit 0). GDB
breakpoints were fully removed at the end of the final successful capture
run before the `finally`-block QMP `cont`.

### UPDATE 2026-07-31 (new session): read dyld's own real source for the
### first time — a concrete, source-grounded hypothesis (launch-closure
### building, scaling with dependency-graph size), NOT yet live-verified

**Hard constraint honored throughout**: this was a source-reading-only
session. Per explicit instruction, the live guest was **never touched** —
serial console (4444), QMP socket, and GDB port (1234) were all left
completely alone the whole session, because a different, concurrent session
had exclusive claim on the live guest for an unrelated live-debugging task.
Everything below comes from reading this repo's own existing source/history
and real Apple open-source code fetched from GitHub. No kernel-side or
userspace-side project files were changed.

**Picking the right dyld source tag.** This project's xnu source (`osfmk/
kern/mach_loader.c`, `osfmk/arm64/sleh.c`, already used throughout the two
update sections above) comes from `apple-oss-distributions/xnu` tag
`xnu-7195.50.7.100.1`. `apple-oss-distributions/dyld` has never been fetched
by this project before. Rather than guess from version-number proximity,
checked the actual **tag timestamps** via `gh api`: `xnu-7195.50.7.100.1`'s
tag object was created at `2020-11-19T01:08:12Z`. Enumerating `dyld`'s ~70
tags and checking timestamps for the ones in the right numeric range found
**`dyld-832.7.1`, tagged at `2020-11-19T01:06:42Z`** — 90 seconds before the
xnu tag, both clearly minted by the same automated "Apple OSS Distributions"
bot account as part of the same source-drop event. This is about as strong a
same-build confirmation as is available without an actual build-number
manifest, and far more rigorous than picking the numerically-closest tag by
guesswork. **All source below is `apple-oss-distributions/dyld` tag
`dyld-832.7.1` (commit `e93f005b86145786b6ef986f2814dce5489acdbe`)** — full
shallow clone kept locally this session for reference at
`scratchpad/dyld_full/` (not committed — recreate via `git clone --depth 1
--branch dyld-832.7.1 https://github.com/apple-oss-distributions/dyld.git`
if a future session needs it again).

**The bootstrap chain, confirmed from source, start to finish.**
`src/dyldInitialization.cpp`'s `dyldbootstrap::start()` — the very first C++
code the kernel's initial PC actually runs (per the previous update's own
`mach_loader.c` finding) — does `rebaseDyld(dyldsMachHeader)` (dyld fixing up
its **own** internal pointers via its chained-fixups, identical for every
process, not app-specific) and then falls straight into `dyld::_main(...)`
(`src/dyld2.cpp`, ~line 6340). `_main()` does a long sequence of environment/
platform setup, then (line 6674 area) determines `sClosureMode` — for iOS
this resolves to `ClosureMode::On` via `getPlatformDefaultClosureMode()`
(dyld3/closures are mandatory on embedded platforms, not optional). It then
(line 6708-6752) tries, **in order**: (1) `sSharedCacheLoadInfo.loadAddress->
findClosure(sExecPath)` — a closure already baked into the dyld_shared_cache
itself, keyed by the executable's own path; (2) `findCachedLaunchClosure()` —
an on-disk closure cache file for this specific binary from a previous launch;
(3) **`buildLaunchClosure()`** (line 6147) — build one from scratch, in-process,
right now. For every one of this project's ad-hoc, `scp`'d, never-through-
`installd`, unsigned test binaries (`/agx_system_metal_test`,
`/mapkit_snapshotter_test`, ...), **(1) and (2) are certain to miss** — the
shared cache's own closure table only knows about binaries that existed when
the cache was built (system binaries), and there is no on-disk closure cache
entry for a binary that has never been launched via the real app-install
pipeline. **This confirms the task's own starting theory directly from
source**: these binaries are guaranteed to fall through to the "build a
closure on the fly, in-process, at launch time" path, every single time.

**`buildLaunchClosure()` → `ClosureBuilder::makeLaunchClosure()`
(`dyld3/ClosureBuilder.cpp:3081`) — read in full.** Its very first lines
(3086-3093) declare the closure-builder's working storage as **plain,
fixed-size C arrays living on `makeLaunchClosure`'s own stack frame**:
```cpp
BuilderLoadedImage  loadImagesStorage[512];
Image::LinkedImage  dependenciesStorage[512*8];   // = 4096
InterposingTuple    tuplesStorage[64];
Closure::PatchEntry cachePatchStorage[64];
_loadedImages.setInitialStorage(loadImagesStorage, 512);
_dependencies.setInitialStorage(dependenciesStorage, 512*8);
```
`_loadedImages` and `_dependencies` are member fields, declared
(`dyld3/ClosureBuilder.h:333-334`) as:
```cpp
OverflowSafeArray<BuilderLoadedImage,2048>  _loadedImages;
OverflowSafeArray<Image::LinkedImage,65536> _dependencies;   // all dylibs in cache need ~20,000 edges
```
`OverflowSafeArray<T,MAXCOUNT>` (`dyld3/Array.h:108-182`, fetched and read in
full) starts using exactly the stack storage handed to it via
`setInitialStorage()`. If a `push_back()` would exceed current capacity,
`verifySpace()`→`growTo()` fires: because a non-default `MAXCOUNT` is
specified for both of these fields, `growTo()` takes the "jump straight to
`max(MAXCOUNT, n)` and `vm_allocate()` a brand-new heap buffer, `memcpy` the
old contents over, then (if there *was* a previous `vm_allocate`'d buffer —
i.e. this isn't the very first growth) `vm_deallocate()` it" branch — guarded
by `assert(oldBufferSize == 0); // only re-alloc once`, an assert that
compiles to nothing in a release/NDEBUG build (Apple's own comment
literally documents the "only re-alloc once" assumption baked into this
data structure).

**The actual recursive walk: `ClosureBuilder::recursiveLoadDependents()`
(`dyld3/ClosureBuilder.cpp:786`), called directly from `makeLaunchClosure`
at line 3180.** Read in full. For the image passed in, it walks that
image's own `LC_LOAD_DYLIB`-equivalent list via `forEachDependentDylib`,
`push_back()`-ing one `Image::LinkedImage` per dependency edge into the
**single, shared, whole-process-wide** `_dependencies` array (line 807/810),
then (line 876) does:
```cpp
forImageChain.image.dependents = _dependencies.subArray(startDepIndex, depIndex);
```
— `subArray()` (`dyld3/Array.h:74-75`) does **not** copy; it returns a new
`Array<T>` object that is a raw pointer **view** into `_dependencies`'s
*current* backing buffer. The very next thing the function does (line
879-891) is iterate that view and, for each entry, **recursively call itself
again** — which will `push_back()` more entries onto that same shared
`_dependencies` array from deeper in the call stack, **while the outer
frame's `for` loop is still actively iterating its own already-taken view of
it**. Critically — checked directly, not assumed — **there is no shortcut
for images already resident in the dyld_shared_cache here**: grepping the
whole file for every assignment to `.dependents` found exactly one site for
the real on-device (`BUILDING_DYLD`) path, this one at line 876; the only
other assignment (`entry.dependents = image->dependentsArray();`, line 3405)
is inside `makeOtherDylibsImageArray()`, compiled only under `#if
BUILDING_CACHE_BUILDER` — the **offline** cache-building tool, never part of
the on-device dyld binary at all. So on a real device, **every single node
in the entire transitive dependency graph — including every ordinary
cache-resident system dylib — gets its own fresh `recursiveLoadDependents()`
call and its own fresh walk of its real load-command list**, every time an
on-the-fly launch closure is built. (Direct, telling contrast: the
ObjC-selector/class optimizer, `optimizeObjC()`, a few hundred lines later
in the same file, explicitly *does* skip cache-resident images — its own
comment says so verbatim: `// Skip shared cache images as even if they need
a new closure, the objc runtime can still use the optimized shared cache
tables.` This proves Apple's own engineers were well aware of, and
deliberately avoided, the "re-walk the whole cache-resident graph" cost in
at least one sibling subsystem — but `recursiveLoadDependents`'s own
node/edge bookkeeping has no equivalent skip.)

**Why this is a plausible, size-scaling crash mechanism — and why it fits
the specific empirical fault signature already captured better than a plain
stack-overflow theory would.** Two related, honestly-ranked candidates:

1. **(Weaker, but real and uncapped) Native C++ recursion depth.**
   `recursiveLoadDependents` has no depth cap, no iterative/worklist
   rewrite, and each frame captures an Objective-C block literal (the
   `forEachDependentDylib` callback) plus a `LoadedImageChain` — real stack
   cost per node in the graph. Rough estimate: real framework dependency
   graphs tend to be wide, not deep (rarely more than a dozen or so hops for
   even "heavy" frameworks), so pure depth alone probably wouldn't blow a
   normal multi-MB thread stack — this is the less independently-convincing
   of the two candidates, flagged mainly because it's real, present in the
   exact same function, and cheap to rule in/out live (see recipe below).
   Worth an explicit, cheap cross-check: whether this project's own kernel
   patches changed anything about the default initial-thread stack size for
   a freshly-`execve()`'d process on this specific build (checkable directly
   from `bsd/kern/kern_exec.c` in the same already-fetched xnu tag) —
   not yet done this session.
2. **(Stronger candidate) A stale/dangling view into `_dependencies` (or
   `_loadedImages`) read after a second or later reallocation actually
   `vm_deallocate`s the buffer it points into.** The *first* ever growth
   (stack `dependenciesStorage[4096]` → heap) does **not** immediately
   crash anything by itself — the abandoned stack array is a local of
   `makeLaunchClosure`'s own frame, which stays alive/mapped for the whole
   recursive walk, so a stale view into it just silently reads frozen-old
   data (a correctness bug, not a fault). The genuinely fault-capable case
   is a **second** growth (needing `_dependencies` to exceed its
   already-large `65536` floor, or `_loadedImages` to exceed `2048`) —
   from that point on, `growTo()`'s `if (oldBuffer != 0) vm_deallocate(...)`
   branch actually unmaps memory that an **outer, still-executing**
   recursion frame may still hold a live pointer/subArray view into (exactly
   the pattern at lines 876/879/888). The next read through that stale view
   lands on genuinely unmapped memory → a clean EL0 data-abort translation
   fault. This maps precisely onto what's already been captured live, twice,
   independently: `esr=0x92000047` (`ESR_EC=0x24`/`DFSC=0x07`, translation
   fault at level 3 — "this address was never mapped," not a permission
   fault) in both the original `agx_system_metal_test` sweep and the
   `mapkit_snapshotter_test` sweep documented above. More tellingly: the one
   fully-captured hit from the MapKit session had **`pc=0x18d40b24c`**
   (inside dyld's own code, as already established) but **`far=0x16d6d3f10`**
   — a *wildly different, unrelated* address region, not a value anywhere
   near what `pc`'s own mapping or a plausible nearby stack pointer would be.
   A classic stack-guard-page overflow's fault address is essentially always
   immediately adjacent to the current `sp` (within one page); a stale
   pointer into memory that was `vm_deallocate()`'d can legitimately point
   *anywhere* in the address space. The one real data point in hand fits
   "wild/stale pointer" qualitatively better than "stack exhaustion" — worth
   stating honestly as **reasoning from one data point, not proof**: `sp`
   itself was never captured alongside `pc`/`far` in either prior session
   (only `pc`/`cpsr`/`far`/`esr` at their known `arm_saved_state64` offsets
   were read), which the recipe below fixes.
   Honest caveat on the numbers: reaching >65536 total dependency edges (or
   >2048 distinct images) for **one process's** launch closure seems like a
   lot even for MapKit — Apple's own comment says the *entire* shared
   cache needs "only" ~20,000 edges. It's plausible the real threshold that
   matters is lower than 65536/2048, via some *other* un-capped-MAXCOUNT
   (i.e. plain-doubling, not one-time-jump) `OverflowSafeArray` elsewhere in
   the same call graph that re-allocates (and thus `vm_deallocate`s) far more
   readily — a doubling array hits its second-and-later, `vm_deallocate`-
   triggering growth at a much lower absolute count than a `MAXCOUNT`-floored
   one does. This session surveyed every `STACK_ALLOC_(OVERFLOW_SAFE_)ARRAY`
   call site in `ClosureBuilder.cpp` (full list kept in this session's
   scratch notes) and `_dependencies`/`_loadedImages` were the clearest,
   best-fitting match found for "scales with total transitive graph size,
   on the real launch path, with no cache-resident shortcut" — but this is
   not represented as an exhaustive proof no smaller-threshold sibling
   array exists.

**One specific alternative theory explicitly checked and NOT supported by
source: dyld tripping over the missing `LC_CODE_SIGNATURE`/entitlements
itself.** The task's own prompt raised this as a plausible angle, so it was
checked directly rather than assumed away: `ClosureBuilder::buildImage()`
(`dyld3/ClosureBuilder.cpp:981-998`) handles a signature-less Mach-O
completely gracefully —
```cpp
if ( macho->hasCodeSignature(codeSigFileOffset, codeSigSize) ) {
    writer.setCodeSignatureLocation(codeSigFileOffset, codeSigSize);
    ...
}
```
a plain, well-formed conditional, not an unchecked assumption of presence.
This doesn't rule out every code-signature-adjacent check in the whole
`dyld3`/`dyld2` source (not exhaustively audited this session), but the one
most directly on the launch-closure-build path for the main executable
itself is clean. This makes the dependency-graph-size angle above the
better-supported lead of the two the task suggested chasing.

**Confidence ranking, stated honestly.** Moderate, not proven. What's solid:
(a) these binaries are certain, from source, to hit the on-the-fly
closure-build path every launch; (b) `recursiveLoadDependents`'s walk
genuinely has no cache-resident shortcut and genuinely scales with total
transitive graph size, confirmed by contrast with a sibling subsystem that
does have such a shortcut; (c) the specific `OverflowSafeArray` growth/
`vm_deallocate` mechanism is a real, reproducible-in-principle hazard
pattern in this exact real Apple source, not a speculative pattern invented
for this write-up; (d) the one existing live fault capture's `pc`/`far`
relationship is qualitatively consistent with "stale pointer into unmapped
memory" and not obviously consistent with "stack guard page." What's
**not** yet shown: that the actual edge/image counts for Metal.framework's
or MapKit.framework's real transitive closures on this exact build cross
any of the specific thresholds identified, or that this mechanism (as
opposed to the recursion-depth candidate, or something else in dyld
entirely not surveyed this session) is what's actually firing. This is
genuinely a hypothesis to test, not a diagnosis.

**Concrete, ready-to-execute live-verification recipe for whoever has guest
access next** (ordered cheapest/most-discriminating first, all built on
tooling this project already has working — no `ipsw`, no new symbol
resolution required for step 1):

1. **Re-run the existing `handle_user_abort` capture (`run_mapkit_test_
   abort_only.py` or the `agx_system_metal_test` equivalent from the update
   above — both already built, already proven to catch this crash), but
   additionally read and record `sp` (general-purpose register read via the
   RSP `g` command, not just the `arm_saved_state64` struct offsets already
   used for `pc`/`cpsr`/`far`/`esr`) on the one genuinely-isolated hit.**
   Compare `far` to `sp`: within ~1 page (`0x4000` on this build) is
   consistent with classic stack-guard-page exhaustion (candidate 1 above);
   wildly different (as the existing single `pc`/`far` pair already
   suggests) is consistent with a stale-pointer/`vm_deallocate` read
   (candidate 2). This alone, with zero new addresses or symbols, is the
   single highest-value/lowest-cost next step and directly discriminates
   between this update's two candidates.
2. **Test the `OverflowSafeArray`-growth theory directly**: breakpoint the
   `vm_deallocate` Mach trap (a well-known fixed syscall, reachable the same
   way this project already located the `dlopen`/`dlsym` stub addresses for
   the Metal-patch work — no dyld symbol table needed) during a triggered
   `/agx_system_metal_test` or `/mapkit_snapshotter_test` run. If it fires
   at all before the crash, capture its `lr` (the same two-phase LR-capture
   technique used throughout the SIGKILL investigation) and check whether it
   points back into the already-established `~0x184000000`-`~0x1eeffffff`
   dyld-private-mapping range. A `vm_deallocate` call originating from
   inside dyld's own mapping shortly before the fault would be strong,
   near-conclusive support for candidate 2.
3. **Localize the exact faulting instruction without needing dyld's own
   (very likely stripped, per this project's own already-documented
   `ipsw`-gap reasoning) symbol table**: `/usr/lib/dyld` is a real,
   standalone on-disk Mach-O (not purely dyld_shared_cache-resident — same
   `dd`/`od` + `grep -a` technique already proven on `backboardd` in the
   App-level investigation section above applies directly, no `ipsw`
   needed). Determine dyld's own runtime load bias for one specific
   triggered run either by (a) scanning backward from the captured `pc` in
   page-sized (`0x4000`) steps via live GDB memory reads until the Mach-O
   magic `0xfeedfacf` is found (reusing the same "assert the magic byte up
   front" idiom `resolve.py` already established for the kernelcache), or
   (b) breakpointing `load_dylinker()` in `mach_loader.c` (already fetched/
   quoted in the update above, `dyld_aslr_page_offset` around line 531 of
   that file) to read the computed slide directly from kernel state at
   image-load time. Compute `fileOffset = pc - dyldLoadVA`, `dd`/`od` dump
   `/usr/lib/dyld` at that offset, disassemble with this project's own
   `mini_disasm.py`. Cross-reference against the concrete constants this
   session's source read predicts should appear nearby if candidate 2 is
   right: an immediate load of `0x1000` (4096) or `0x10000` (65536) (the
   `_dependencies` capacity constants), `0x200`(512)/`0x800`(2048) (the
   `_loadedImages` capacity constants), or a `bl`/`blr` to a `vm_allocate`/
   `vm_deallocate`/`memcpy` stub.
4. **If (1)-(3) point away from candidate 2, test candidate 1 directly**:
   breakpoint `recursiveLoadDependents`'s own entry (locatable the same way
   as step 3, once dyld's file offset math is established) and count
   entries-without-matching-returns (live recursion depth) during a
   triggered run, using the same "don't touch/step an unrecognized stop"
   SMP-safety discipline this doc's methodology notes already mandate for
   this exact multi-vCPU target. Correlate peak depth against the fault.
5. **Cheap, independent, complementary check regardless of which candidate
   wins**: read this project's own patched kernelcache's actual configured
   default stack size for a freshly-`execve()`'d process's initial thread
   (`bsd/kern/kern_exec.c`, same xnu tag already used throughout this doc)
   and sanity-check it against a real device's known default — a two-minute
   source cross-check, cheap enough to do regardless of which theory the
   live data ends up supporting, and directly load-bearing for candidate 1
   specifically if this project's own kernel patches (5 SIGKILL gates,
   `block_invoke` patch) turn out to have touched anything relevant (not
   expected, per what those patches actually do, but not yet independently
   confirmed either).

Full local copy of the fetched dyld source (`dyld-832.7.1`) used for this
update — `src/dyldInitialization.cpp`, `src/dyld2.cpp`,
`dyld3/ClosureBuilder.cpp`/`.h`, `dyld3/Array.h`, `dyld3/Closure.h`,
`dyld3/AllImages.cpp`, `dyld3/Loading.cpp`, `dyld3/SharedCacheRuntime.cpp`,
plus the full shallow clone — lived only in this session's scratchpad, not
committed to this repo (same convention as this project's other
scratch-only tooling, e.g. `mini_disasm.py`'s intermediate dumps);
regenerate via the `git clone` command above if needed again.

No guest state was touched this session (confirmed by construction, not by
after-the-fact check, since no guest connection was ever opened): QMP, GDB,
and the serial console were never used. This entire update is source
research only.

## Widget-hosted Metal compositing: live test attempt (2026-07-31, new session)

Direct continuation of "Widget-hosted Metal compositing design and prototype"
above, picking up its own "Concrete next steps for whoever picks this up
(live-testing required)" list. Guest was confirmed exclusively available
(QEMU pid `2858187`, `info status` → `running`, `/sigkill_test` →
`Segmentation fault: 11`, `/compute_test` → `result = 42`) — no other
session using it concurrently, unlike every prior session in this thread.
Downloaded the `widget-host-prototype` CI artifact from run `30607988494`
fresh (`inferno_widget_host`, 71952 bytes, matches the run's own recorded
job — no rebuild needed, artifact was still live).

### Step 1 result (read-only): decisive, build-wide negative for the
### prototype's core assumption — every widget in this build is WidgetKit,
### none are legacy `NCWidgetProviding`

Per the prior session's own step 1 ("resolve legacy `NCWidgetProviding` vs
WidgetKit identity — decides whether this whole approach is even viable"),
dumped `StocksWidget.appex/Info.plist` (976 bytes, `bplist00` magic —
`plutil` is **not present on this guest** (`which`/`plutil` both "command
not found"), so used this project's own established technique: `od -An -v
-tx1` over the full file via the serial console, reassembled and parsed
locally with Python's `plistlib`, which reads binary plists natively). One
real gotcha hit doing this: a first parsing attempt of the reassembled hex
silently corrupted the byte stream by including the *echoed command
line itself* as data — the naive "any two-hex-char token" filter matched
the literal substring `dd` inside the echoed `dd if=... | od ...` command
line (`d` and `d` are both valid hex digits), prepending a spurious extra
`0xdd` byte and shifting every subsequent offset by one (visible as a
garbled magic number, `0xedfacfdd` instead of `0xfeedfacf`, a right-rotated
version of the real bytes). Fixed by simply excluding the first (echoed
command) line before token-scanning — worth recording as a reusable
gotcha for this project's whole "reassemble hex dumped over the serial
console" technique, since it would silently corrupt any future dump whose
issuing command line happens to contain a two-hex-digit substring.

**Full parsed `StocksWidget.appex/Info.plist`:**
```
NSExtension = {'NSExtensionPointIdentifier': 'com.apple.widgetkit-extension'}
CFBundleExecutable = StocksWidget
CFBundleIdentifier = com.apple.stocks.widget
CFBundlePackageType = XPC!
... (version/platform/build metadata, unremarkable)
```
**No `NSExtensionPrincipalClass` key at all** — confirming this isn't just
"the wrong value," it's a structurally different plist shape than the
prototype's design assumed. Legacy `NCWidgetProviding` extensions declare
`NSExtensionPointIdentifier = com.apple.widget-extension` **and** a
`NSExtensionPrincipalClass` string naming the hosted `UIViewController`
subclass (exactly the key the prototype's step 2 was designed to edit).
WidgetKit extensions apparently need neither.

**This is not specific to `StocksWidget`.** Repeated the same check (this
time via a faster `grep -a -o 'com\.apple\.widget[a-z-]*extension'` directly
on the guest, no need to reassemble the full plist) against every other
widget `.appex` actually running in this exact boot (`ps auxww | grep -i
widget`): `WeatherWidget`, `PhotosReliveWidget` (note: lives at
`/Applications/MobileSlideShow.app/...`, **not** under
`/private/var/containers/Bundle/Application/<UUID>/`, unlike every other
widget here — a real, system-app-tier bundle location, not a
sandboxed-container one), `ScreenTimeWidgetExtension` (also
`/Applications/...`), `GeneralMapsWidget`, plus two more not previously
enumerated (`RemindersWidgetExtension`, `CalendarWidgetExtension`, found via
a fresh `find ... -iname "*.appex"` sweep). **All seven, without a single
exception, report `com.apple.widgetkit-extension`.** This build's
Springboard-hosted Today View / widget gallery is 100% WidgetKit — Apple
shipped every first-party widget already rewritten for the new framework
from iOS 14.0 itself (matches real-world history: WidgetKit launched with
iOS 14, and Apple's own bundled widgets adopted it immediately, unlike
third-party apps which needed a new Xcode target type). **There is no
legacy-widget fallback anywhere in this build** — the prior session's own
fallback list (`WeatherWidget`/`PhotosReliveWidget`/`ScreenTimeWidgetExtension`)
is exhausted; none of them are viable either, for the identical reason.

### Step 1b (not in the original plan, but directly informative): inspected
### the real `StocksWidget` binary's own Mach-O structure before deciding
### whether to still attempt the swap

Given the plist result, checked what a *real* WidgetKit extension binary
actually looks like structurally, to judge whether the existing prototype
(built `-Wl,-e,_NSExtensionMain`, no `main()`, plain ObjC/UIKit) has any
realistic chance of being accepted even if forced into place. Same `dd
bs=1 count=4096 | od -An -v -tx1` + local Python `struct`-based Mach-O
header/load-command parse used throughout this project (not otool/ipsw).

**The real `StocksWidget` binary has a genuine `LC_MAIN` with
`entryoff=0x4a658`** — i.e. a real, normal, compiled `main()` entry point,
**not** the `-e _NSExtensionMain`-style dyld-stub-symbol entry the
prototype uses. Its `LC_LOAD_DYLIB` list: `TeaUI.framework`,
`NewsFoundation.framework` (shared code with News.app, mildly interesting
but a tangent), `Stocks/StocksCore.framework`, **`SwiftUI.framework`**,
`TeaCharts.framework`, `TeaFoundation.framework`, **`WidgetKit.framework`**,
`Foundation.framework`, `libobjc.A.dylib`, plus more past the 4096-byte
capture window. The presence of a real compiled `main()` (almost certainly
Swift's autogenerated top-level entry from a `@main`-attributed
`WidgetBundle` conformer, consistent with linking `WidgetKit`+`SwiftUI`
directly) rather than `NSExtensionMain()` **independently confirms, from
the binary side, what the Info.plist already showed from the metadata
side**: WidgetKit extensions in this build do not go through the classic
`NSExtensionMain()` → `NSExtensionPrincipalClass` → `UIViewController`
bootstrap at all. They have their own, different, Swift/WidgetKit-native
bootstrap and registration path with the host, one this project has no
visibility into internals-wise (same DSC-parser gap as everywhere else in
this document — `WidgetKit.framework` itself is DSC-resident).

**Assessment**: the existing `inferno_widget_host` prototype is built for
an extension shape (`NSExtensionMain`-entry, ObjC `UIViewController`
principal class, live-hosted `CALayer`) that **does not exist anywhere in
this build**. This is a real, load-bearing architecture mismatch, not a
detail to patch around — the prototype would need a fundamentally
different entry point (a real `main()`, most realistically obtained the
same way this project's other plain-executable test binaries already are,
i.e. compiled with a normal `int main()` instead of the `-e
_NSExtensionMain` linker override) and a different registration story with
whatever WidgetKit's own host process expects, which is unknown without
DSC introspection of `WidgetKit.framework` itself.

### Live test performed anyway, expectations set honestly up front

Per this task's own instruction ("a well-documented failure with a clear
next step is a completely acceptable outcome — don't force a false-positive
conclusion") and this project's standing M.O. of preferring a real,
observed result over a purely theoretical one, the swap was attempted live
on `StocksWidget` regardless of the architecture mismatch just established
— the artifact was already downloaded, the transfer/respring mechanics are
cheap to exercise either way, and *how exactly* a mismatched extension
shape fails (silent non-launch? launch-then-immediate-death? a new,
unpatched SIGKILL-style gate? the same pre-`main()` dyld crash signature
documented elsewhere in this file?) is itself real, useful information for
whoever designs the next iteration.

### A 6th, previously-undiscovered security gate, found and live-patched:
### app-container writes are blocked for non-`_installd` processes

Attempting even the very first mechanical step — backing up the original
`StocksWidget` binary before overwriting it — hit a brand-new wall,
independent of everything above: `cp
.../StocksWidget.appex/StocksWidget .../StocksWidget.appex/StocksWidget.orig`
failed with `Operation not permitted`, and so did a plain `touch` of a new
file anywhere under
`/private/var/containers/Bundle/Application/<UUID>/...` — even though the
calling shell is root (`uid=0`) and the target directory's own POSIX
permissions (`_installd:_installd`, `rwxr-xr-x`) don't obviously forbid it.
`dmesg` showed the real cause, a message shape not seen before in this
project's SIGKILL/Sandbox investigations:
```
System Policy: cp(2805) deny(1) file-write-create /private/var/containers/Bundle/Application/.../StocksWidget.appex/StocksWidget.orig
System Policy: touch(2811) deny(1) file-write-create /private/var/containers/Bundle/Application/.../StocksWidget.appex/probe.txt
```
(the `System Policy:` tag, as opposed to `Sandbox: <proc> deny(1) ...`, was
already seen twice before in this document — the `/private/var/tmp`
`process-exec*` denial and the `iokit-open`/`com.apple.security.iokit-user-client-class`
entitlement denial — confirming it's a real, distinct message class, not a
typo or one-off.) This is, at root, a *genuine and correct* iOS protection
(installed-app bundle content is normally `_installd`/`MobileInstallation`-only
to write, even for root, on real hardware too) — this project just hadn't
needed to write into an app container before.

**Found and live-patched using the exact same disassemble-first technique
as every other gate in this document — no live guest interaction needed for
the analysis itself**, since the target function lives in the same
`kernelcache.decompressed` file this project already keeps locally
(`/home/makr/Documents/Inferno/InfernoData/kernelcache.decompressed`) and
`patch_kernelcache.py` already exports a reusable `va2off()` helper — this
whole investigation was done by direct offline Python analysis (own small
correctly-verified-by-hand ARM64 decoder, hand-checked bit-by-bit against
the ARMv8 reference encoding for the two instructions that mattered, since
this session had no access to the scratch `mini_disasm.py` from a prior,
different session), reading `guest_tools/gdb_rsp2.py`'s `RSP` client class
for the actual live-patch step.

The concurrent MapKit `/b` investigation (see that section) had already
statically identified `hook_vnode_check_open`
(`0xfffffff0092a242c`) as handling **two** MACF operation indices in one
function: op `0x15` (hypothesized `file-read-data`) unconditionally, and op
`0x1f` conditionally, only "if flags & 0x402" — but had never disassembled
that second, conditional branch (its own task was about reads, not
writes). Disassembling it this session (offline, against the static
`kernelcache.decompressed`) shows the exact shape:
```
0xfffffff0092a24ac: tbz  x20, #0, 0xfffffff0092a2530   ; skip op-0x15 block if bit0 clear
...op-0x15 block (evaluate(op=0x15), capture into x21, no early branch)...
0xfffffff0092a2530: movz w8, #0x402                     ; flags-need-write-check test
... (AND/TST + B.cond, gates whether the block below runs)
0xfffffff0092a2590: movz w8, #0x1f
0xfffffff0092a25ac: movz w1, #0x1f
0xfffffff0092a25b0: bl   0xfffffff0092a9ef4              ; SAME shared evaluator as op 0x15
0xfffffff0092a25b4: mov  x21, x0                          ; <-- capture op-0x1f's result (PATCHED)
0xfffffff0092a25b8: b    0xfffffff0092a25c0                ; (skips the flags-didn't-need-it path)
0xfffffff0092a25bc: movz w21, #0x0                        ; flags-didn't-need-it path: force-allow
0xfffffff0092a25c0: ...
0xfffffff0092a25d0: mov  x0, x21                           ; unconditional passthrough -> return value
0xfffffff0092a25e8: ret
```
Same shape as the already-documented `hook_vnode_check_getattr` patch
(op `0x16`): no early-return branch gates this specific op, the result is a
pure passthrough of whatever the capture register holds at the very end.
**Minimal patch, matching that same established style exactly**: at VA
`0xfffffff0092a25b4`, replace `mov x21, x0` (`f5 03 00 aa` LE) with `movz
w21, #0` (`15 00 80 52` LE) — reusing the *exact same instruction encoding*
already present 8 bytes later in this very function for the
"flags-didn't-need-this-check" path, so it's not even a novel instruction
sequence, just relocating one already-proven-valid encoding to a second
site. Independently hand-verified both the original and replacement
encodings bit-by-bit against the ARMv8 reference (logical-shifted-register
and MOVZ formats) before writing, given no live disassembler was available
this session.

**Live-patched via GDB (`gdb_rsp2.py`'s `RSP` client, one-off script), fully
verified, in-memory only (does NOT survive a QEMU restart — same caveat as
the original 5 SIGKILL gates before they were baked permanently):**
asserted current bytes matched the expected original (`f50300aa`) before
writing (per this project's own "never patch blind" rule) — matched — wrote
`15008052`, read back to confirm. QMP `cont` issued immediately after
(wrapped in the reused script's own `finally`), confirmed VM `running`
again via `info status` before touching the guest further.

**Empirically confirmed fixed, with an important nuance**: `dd
if=/StocksWidget.orig of=<the real StocksWidget path> conv=notrunc` (an
in-place, no-new-vnode overwrite) now succeeds (`EXIT=0`, correct byte
count) — as does plain `dd` **without** `conv=notrunc` (which truncates the
target to the new content's length, useful since the real replacement
binary is a different size than the original). **`cp`/`touch` targeting a
genuinely new filename in the same directory still fails** — confirming
`file-write-create` (new vnode) is a **separate, still-unpatched** check
from `file-write-data` (write into/truncate an existing vnode), almost
certainly XNU's real, architecturally-distinct `vnode_check_create` hook
rather than another branch inside `hook_vnode_check_open` — not
investigated further since it isn't needed for this task (the backup was
instead made by copying the original *out* to `/StocksWidget.orig`, a
path outside the container restriction entirely, which was already
unrestricted and worked on the first try). One more confirmed-benign
wrinkle: `dmesg` keeps logging `System Policy: dd(...) deny(1)
file-write-data ...` for every write **even after the patch**, despite the
write actually succeeding — consistent with (and further evidence for) the
same conclusion already drawn for the `hook_vnode_check_getattr` patch:
the shared bytecode evaluator (`0xfffffff0092a9ef4`) does its own internal
deny-logging as a side effect of evaluating the profile, independent of
what the calling hook function goes on to do with the result — the log
line is *not* proof of an actual continuing enforcement, only proof the
profile bytecode itself still says "would have denied this."

This gate is currently **live-patched in memory only** — not yet added to
`patch_kernelcache.py`'s permanent `SIGKILL_GATE_PATCHES` table (would
require a full QEMU kill+relaunch to verify against a fresh boot, which
this session deferred in favor of spending the time budget on the actual
widget-hosting live test this gate exists to unblock). **Recommended
next step for a future session**: add `(0xfffffff0092a25b4,
bytes.fromhex("f50300aa"), bytes.fromhex("15008052"), "gate #6: Sandbox
hook_vnode_check_open op 0x1f (file-write-data), force-allow capture
register")` to that table and re-verify after a real restart, the same way
the original 5 were made permanent.

## Widget-hosted Metal compositing: `main()`-shape fix + live test, and the
## container-signature-cascade gate (2026-07-31, new session)

Direct follow-up to both open items the prior live-test session left behind:
(1) the confirmed architecture mismatch (`inferno_widget_host.m`'s
`-Wl,-e,_NSExtensionMain` shape vs. the real `StocksWidget` binary's genuine
`LC_MAIN`), and (2) the "6th, previously-undiscovered security gate"
(`hook_vnode_check_open` op `0x1f`, file-write-data) that session live-patched
in memory to unblock the binary swap itself. Two explicit sub-tasks, in
priority order: fix the entry-point shape (primary), then investigate the
*separate* container-signature-cascade kill the prior session observed live
via the user's own manual interactive testing (secondary). Both are covered
below.

### Part 1: `inferno_widget_host_main.m` — a real `main()`-shaped variant

**New file, `src/userspace_test/inferno_widget_host_main.m`, added alongside
the original `inferno_widget_host.m` (kept unmodified, same precedent as
`agx_system_metal_test_direct.m` sitting next to `agx_system_metal_test.m`).**
Deliberately as simple/dependency-light as possible so any failure is
attributable to the file's own shape, not incidental complexity:
- A real, plain `int main(void)` — no `-Wl,-e,_NSExtensionMain` override, no
  `UIViewController`/`UIKit`/`QuartzCore`/`CoreGraphics` at all (there is no
  PlugInKit-hosted view to draw into without the `NSExtensionMain` handshake
  this file specifically does not attempt, so that machinery would add risk
  for zero payoff).
- Links only `Foundation` + `Metal`, mirroring this project's own
  already-proven plain executables (`compute_test`/`draw_test`, i.e.
  `agx_metal_api_compute_test.m`/`agx_metal_api_draw_test.m`).
- Reuses the exact same `/b`-bridge render pipeline (`dlopen("/b")` → `Q()` →
  device → texture → two `MTLLibrary`s → pipeline → per-frame
  buffer/encoder/draw/commit/`getBytes`, same two AIR shaders byte-for-byte)
  as `inferno_widget_host.m`, purely as an ongoing self-check that the
  process is alive and doing real work for as long as it survives — not
  because this variant expects any compositing to actually happen (there is
  no hosted `CALayer` to reach in this design).
- An infinite plain `sleep()`-paced loop (not `CFRunLoopRun()`/
  `dispatch_main()`, which would pull in run-loop bootstrap machinery this
  file has no need to depend on), so it can't itself be the reason the
  process fails to stay alive.
- `WTrace()` diagnostic helper, same shape as the original's `WTrace`, but
  writing to a **different** path (`/tmp/widget_host_main_trace.log`, not
  `.../widget_host_trace.log`) specifically so a live test of this variant
  can never be confused with a stale log from an `inferno_widget_host.m` run.
  Every line is pid-prefixed. The very first statement in `main()` writes a
  trace line before anything else runs — mirroring
  `agx_system_metal_test.m`'s `MTrace()`-as-first-statement pattern, so even
  a total setup failure still proves whether execution reached user code at
  all.

**`.github/workflows/build.yml`**: added a second compile step to the
existing `widget-host-prototype` job (the original `-e _NSExtensionMain` step
kept as-is), building `inferno_widget_host_main.m` as a plain executable
(`clang ... -framework Foundation -framework Metal -o
out9/inferno_widget_host_main ...`, no `-e` override), then dumping
`otool -hv`/`-l`/`-L` and `nm -m` for inspection, same "always show the raw
evidence" convention as every other job in this file.

**CI result (run `30630089751`): compiled and linked cleanly, decisive
evidence the entry-point mismatch is genuinely fixed.**
- `otool -hv`: `MH_MAGIC_64 ARM64 E USR00 EXECUTE 22 2176 NOUNDEFS DYLDLINK
  TWOLEVEL PIE` — real `EXECUTE` filetype, `NOUNDEFS` confirms every symbol
  reference (including the entry point) fully resolved.
- `otool -l`: a genuine `LC_MAIN`, `entryoff 16384 stacksize 0` — **not** an
  `-e`-override entry, immediately followed by ordinary `LC_ENCRYPTION_INFO_64`
  then `LC_LOAD_DYLIB` commands for exactly `Foundation`, `Metal`,
  `libobjc.A.dylib`, `libSystem.B.dylib`, `CoreFoundation` — **no**
  `WidgetKit`/`SwiftUI`/`UIKit`, matching the design intent precisely.
- `nm -m`: `_main` present as `(__TEXT,__text) external _main` — a real,
  defined symbol, not an undefined stub reference. Grepping the symbol table
  for `extensionmain` (case-insensitive) finds **nothing at all** — confirmed
  by inspecting the full table directly, not just trusting the grep (the CI
  script's own convenience grep for `\bmain\b|extensionmain` also came back
  empty, which turned out to be a regex-boundary false negative — `\b` does
  not match between `_` and `m` since both are word characters in POSIX ERE
  — not a real absence; the full `nm` output settles it unambiguously).
  `SetUpDevice`/`RenderOneFrame`/`WTrace`/`gDevice` etc. all present as
  expected `__TEXT`/`__DATA,__bss` symbols.

This fully answers open question #3 from the design session and the
mismatch identified in the prior live-test session: the binary's Mach-O
shape now structurally matches the real `StocksWidget` executable's own
(real `LC_MAIN`, no extension-stub entry), and is otherwise link-clean.

### Part 1 live test: swap, respring, unlock, navigate — a real, decisive,
### negative-but-highly-informative result

**Transfer gotcha, worth recording as a reusable lesson**: the first attempt
to transfer `inferno_widget_host_main` (69304 bytes) to the guest via
`transfer_binary3.py` was corrupted mid-flight — a **second, concurrent**
serial-console connection (opened by the orchestrating session to check
status while the transfer was still in progress) collided with it, leaving
the guest shell stuck at an open continuation prompt (recovered cleanly via
the already-documented `Ctrl-C` twice) and the transferred file **silently
truncated** at exactly 69000 of 69304 bytes (confirmed via `wc -c`, not just
assumed) with its trailing `chmod 755` never having run. This is the exact
same underlying hazard this doc's "never open a second serial connection
while a transfer is in flight" rule already warns about, now with a second,
independent concrete reproduction (the first being the ~1650-character
long-command corruption case). Fix was mechanical: `rm -f` the truncated
file, retransfer cleanly with no concurrent connection this time — the retry
landed at exactly 69304 bytes, executable, verified byte-count-exact before
proceeding. **Lesson for future sessions**: a byte-count check
(`wc -c < remote_path`) against the known local size should be treated as
mandatory before trusting *any* serial-console transfer that had *any*
external interaction during its window, not just an optional nicety.

**The swap itself**: `dd if=/inferno_widget_host_main of=<StocksWidget.appex
path>/StocksWidget` (no `conv=notrunc`, correctly truncating the container
copy from the previous session's 71952-byte `NSExtensionMain`-shaped
prototype down to this variant's 69304 bytes) — `135+1 records
in/out`, confirmed via a follow-up `wc -c` reading back exactly `69304`.
Gate #6 (file-write-data, still live-patched in memory only from the prior
session — QEMU has not restarted since) worked exactly as documented: the
write succeeded (`DD_RC=0`) despite `dmesg` still logging a `System Policy:
dd(...) deny(1) file-write-data ...` line for it (the already-understood
"shared bytecode evaluator logs its own would-have-denied verdict
independent of what the calling hook does with it" behavior).

**Triggering a real launch attempt required three real steps, not just a
respring** (a respring alone reloads SpringBoard's own process but does
**not** by itself cause Today-View widgets to instantiate — confirmed
directly: immediately after `kill -9 <SpringBoard pid>` and the fresh
SpringBoard respawning, `ps auxww | grep -i stocks` showed nothing, and the
screen was confirmed via QMP `screendump` to still be sitting on the lock
screen):
1. **Unlock**: `qmp_raw.py`'s `swipe()`, a real held multi-step drag from
   near the bottom of the screen to near the top (`(414,1780)→(414,100)`,
   40 steps, `0.04s` per step, `0.3s` settle). A first, shorter attempt
   (`(414,1750)→(414,300)`, 25 steps) visibly began the transition (the
   "Смахните вверх, чтобы открыть" prompt text visibly faded) but did not
   complete it — confirming this doc's existing "known finicky, not
   impossible" note about this gesture. The longer/slower second attempt
   unlocked cleanly to the home screen on the first try.
2. **Reach Today View**: a stray "iOS update available" alert
   (`Доступно обновление iOS...`) appeared after the first rightward swipe
   attempt (from a genuine, unrelated system nag dialog, not anything this
   session triggered) and had to be dismissed (tap on `Закрыть`) before a
   second rightward swipe actually landed on the Today View page (swipe
   right across the leftmost home-screen page — the paging model in this
   build is: numbered app pages, then Today View one swipe further left,
   confirmed by the page-dot indicator disappearing and being replaced by
   Today View's actual widget-stack content once reached).
3. **Screendump-confirm, don't assume**: every step above was verified with
   a QMP `screendump`, not inferred from gesture completion alone — this
   directly avoided two real near-misses (the too-short swipe leaving the
   lock screen still up; the update-alert silently blocking the swipe from
   reaching Today View at all) that would have produced a false "widget
   never launches" conclusion for the wrong reason.

**Result, once Today View was genuinely confirmed reached**:
`StocksWidget` **still never launches** — the exact same failure mode
already documented for the previous (`NSExtensionMain`-shaped) swap,
confirming the entry-point-shape fix, while real and necessary, is **not
sufficient** on its own. Three independent, converging pieces of evidence:
1. `ps auxww | grep -i widget` around and after the Today View visit shows
   `WeatherWidget`, `GeneralMapsWidget`, and `PhotosReliveWidget` **all**
   freshly relaunched (new pids, timestamps matching the respring window) —
   proving the general "Today View becoming active triggers on-demand widget
   instantiation" mechanism is genuinely working and was genuinely exercised
   — but `StocksWidget` is conspicuously, consistently absent from every
   check.
2. `/tmp/widget_host_main_trace.log` **never gets created** — `cat` returns
   "No such file or directory". Since `WTrace()`'s very first call is the
   literal first statement in `main()`, this proves execution never reached
   even the first line of this project's own code — the process is being
   killed at/before `execve()` completion, structurally identical to the
   original 5 (now 6) SIGKILL-gate kills, not a userspace crash.
3. `dmesg`, timed exactly against the Today View window (correlated via the
   simultaneous `memorystatus: set assertion priority(3) target
   WeatherWidget:4243` / `GeneralMapsWidget:4229` / `PhotosReliveWidget:4334`
   lines — RunningBoard assertions for those widgets' own legitimate,
   successful timeline-refresh cycles), shows repeated bursts of the exact
   same message the prior session already found and flagged as a new,
   undocumented-until-then gate:
   ```
   Sandbox: hook..execve() killing <unsigned>[pid=4330, uid=501]: attempting to use a container without a code signing identity.
   Sandbox: hook..execve() killing <unsigned>[pid=4331, uid=501]: attempting to use a container without a code signing identity.
   Sandbox: hook..execve() killing <unsigned>[pid=4333, uid=501]: attempting to use a container without a code signing identity.
   ```
   (and a second burst, pids 4338/4339/4340, ~17s later) — i.e. **the same
   container-signature-cascade gate that killed sibling XPC helpers when the
   prior session's `NSExtensionMain`-shaped binary was in place also kills
   this session's correctly-`main()`-shaped replacement**, before its own
   code ever runs. This is a genuinely useful negative result: it cleanly
   separates the two problems Part 1 and Part 2 were scoped around — the
   entry-point mismatch is fixed and no longer the blocker; the *actual*
   current blocker is the bundle-wide signature-validity cascade, which is
   completely indifferent to what shape the replacement binary itself takes.

**A concrete, unplanned bonus finding**: tapping the Today View's placeholder
tile for the (non-functional) Stocks widget — a plain gray box reading "Нет
доступного контента" ("No content available"), the same generic WidgetKit
placeholder chrome separately observed for Photos earlier this session per
the orchestrating session's own account — **expands into a fully-formed,
structurally correct Stocks widget-stack detail view**: a stock-chart icon,
a large chart pane with a proper axis/gridline/legend layout, and two
watchlist-style list sections — all populated with `--` placeholder values
instead of real data (unsurprising, since this offline QEMU guest has no
real stocks backend for even the *genuine* Stocks app to reach). Since our
replacement process never runs at all (confirmed above), **this chrome is
being generated entirely host-side by WidgetKit itself**, not by anything
our binary produced. This lines up exactly with the `getPlaceholders`
operation the concurrent DSC-parser session found in
`WidgetKit.ExtensionSessionOperation`'s case list (see that session's own
section below) — concrete, live, visual confirmation that real WidgetKit
hosts fall back to their own generic placeholder rendering when the actual
extension can't be reached, rather than showing an error or blank tile.
This also answers, empirically, one of Part 1's own original questions
("does WidgetKit's host process itself report/log anything more
informative once the immediate mismatch is gone?") — the answer is: it
doesn't need to log anything extra to be informative; its own placeholder
behavior *is* the informative signal, and it's a clean, non-crashing,
non-alarming fallback, not any kind of error state.

**Net assessment for Part 1**: the task's own stated success bar ("a
well-documented 'it still doesn't fully work, but here's exactly how it
fails now, and here's what that tells us' is a completely valid, valuable
outcome") is met with a genuinely decisive result, not a shrug. The specific
mismatch Part 1 was scoped to fix (`NSExtensionMain`-stub entry vs. real
`main()`) **is fixed and CI/structurally verified**; the live test now
isolates the *next* real blocker precisely (the container-signature cascade,
Part 2's target) instead of leaving both problems conflated. Even if Part 2
is later resolved, the concurrent DSC-parser session's WidgetKit findings
(see its section below) mean a further, separate piece of real engineering
— an `NSXPCListener` implementing `ExtensionToHostXPCInterface` and
answering at minimum `getDescriptors`/`getTimeline` over a
`com.apple.chronod`-reachable channel, plus whatever `RunningBoard`
assertion dance `ExtensionSessionFactory.makeSession` expects — would still
be needed before this project's replacement binary could ever show *live,
Metal-rendered* content in that Stocks tile instead of the generic
placeholder. That is real, substantial, correctly out-of-scope-for-this-
session work; the concrete addresses to start from
(`WidgetBundle.main()` @ `0x1c100957c`, `ExtensionSessionFactory.makeSession`
@ `0x1c101573c`, both already located via `dsc_parse.py`) are recorded in
that session's own "Concrete next steps" list, not repeated here.

### Part 2: the container-signature-cascade gate — investigated, not yet
### live-patched; concrete groundwork laid for both candidate fixes

Per the task's own explicit choice between (a) ad-hoc-resigning the
replacement binary (+ patching the bundle's `_CodeSignature/CodeResources`)
and (b) a kernel patch via this project's established disassemble-first
methodology — **both were investigated this session; neither was completed
live**, given the time already spent recovering from the transfer-corruption
gotcha above and completing Part 1's live test properly (per the
orchestrating session's own repeated, correct emphasis on reaching a real
observed outcome for Part 1 first). What follows is real, verified
groundwork for whichever direction a future session picks up.

**Direction (a): `ldid` is now a genuinely available, working tool on this
Linux host — a decisive, positive answer to a previously-open question.**
Not present via `apt`/`snap` directly (`apt-cache search ldid` and
`snap find ldid` both come up empty), but buildable from source with zero
`sudo`/root access needed at any step:
- `git clone https://github.com/ProcursusTeam/ldid.git` (the actively
  maintained fork; plain `make` needs only `libcrypto`/`libplist-2.0` via
  `pkg-config`).
- `libssl-dev` (for `libcrypto`) was **already installed** on this host.
  `libplist-2.0` was only present as a runtime lib
  (`libplist-2.0-4`), not the `-dev` headers/`.pc` file — fixed with
  `apt-get download libplist-dev` (downloads the `.deb` without installing
  or needing root — Ubuntu's `apt-get download` doesn't require privilege
  escalation) then `dpkg -x <deb> <local dir>` to extract just the headers
  locally, with a hand-created `.so` symlink pointing at the
  already-installed runtime `.so.4` (the extracted `-dev` package's own
  symlink target was itself missing since only the runtime package, not the
  `-dev` package, was ever actually installed system-wide) so the linker
  could resolve `-lplist-2.0` against it.
- Result: `ldid` builds cleanly (`g++ -std=c++11 ...`), runs, and was
  **verified functionally correct** against a real arm64e Mach-O (this
  session's own CI-built `inferno_widget_host_main`): `ldid -S
  test_sign_target` embeds a real `CodeDirectory` (`v=20400`, `hashes=17+2`,
  `Hash type=sha256`) with a real `CandidateCDHash`; `ldid -Icom.apple.stocks.widget
  -S` correctly sets a custom identifier matching the real bundle ID. This
  concretely resolves the "is `ldid` obtainable" question this task raised —
  it is, and it works.
- **Not completed this session**: actually pulling the real
  `_CodeSignature/CodeResources` off the guest (would need the same
  chunked-transfer-in-reverse technique this project already uses for
  binary-plist reads, `od`-dumped over the serial console and reassembled
  locally — the file's exact size wasn't checked this session, so its cost
  is unknown but plausibly cheap, being just a hash manifest, not a full
  binary), understanding/patching its per-file SHA1/SHA256 hash entry for
  `StocksWidget` to match the ad-hoc-re-signed replacement, and writing the
  modified plist back (gate #6 covers this, being a file-write-data
  operation against an existing file). **This is the recommended next step
  for whoever picks up direction (a)** — the tooling gap that made this
  direction previously "not obviously feasible" is now fully closed; only
  the mechanical CodeResources-editing work remains, and it's a clean fix
  (no kernel modification) if it works.

**Direction (b): the responsible kernel string/function is now precisely
located, though the exact guarding conditional branch was not fully pinned
down.** Same disassemble-first methodology as every other gate in this doc,
done entirely offline against the local `kernelcache.decompressed`, no live
guest interaction needed for the analysis itself:
- The literal message string `"attempting to use a container without a code
  signing identity."` occurs **exactly once** in the kernelcache — file
  offset `0x559ef6`, VA `0xfffffff00755def6` (inside `__TEXT __cstring`,
  confirmed via the same `va2off`/segment-table walk `patch_kernelcache.py`
  already uses). Immediately followed in memory by
  `"failed to upcall to containe[rmanagerd]"` (truncated by the read
  window), a plausible sibling error string for a related failure mode, not
  otherwise investigated this session.
- Wrote a small, targeted ADRP+ADD scanner (not a general disassembler) over
  the `__TEXT_EXEC __text` section (0x1def9c0 bytes, ~7.85M instructions,
  fully scanned in under 3 seconds in pure Python via bulk
  `struct.unpack_from`) looking for an `ADRP xN, page` immediately followed
  by an `ADD xN, xN, #imm12` whose combined result equals the target VA
  exactly (not just "targets the right 4KB page", which alone produced 92
  same-page false-positive candidates — most of that page holds several
  *other* nearby error strings referenced from the same function region).
  Found exactly one genuine, immediately-adjacent pair: **`ADRP x8, page` at
  VA `0xfffffff0092b1350` (encoding `90ff1568`) + `ADD x8, x8, #0xef6` at VA
  `0xfffffff0092b1354` (encoding `913bd908`)**, followed by an unconditional
  `B` to a shared epilogue at `0xfffffff0092b1368` that `STR`s the computed
  message pointer before falling into whatever common log/kill logic all of
  this function's many error paths share.
- **This message-construction site sits inside the exact same large function
  region as the already-patched, already-permanent gate #3
  (`0xfffffff0092b0f00`) and gate #4 (`0xfffffff0092b126c`)** — i.e. very
  likely still `hook_cred_label_update_execve` in `Sandbox.kext`, the same
  function this project has already twice successfully patched individual
  branches inside of. This is a strong, concrete, structurally-consistent
  lead: the container-signature-cascade kill is not some unrelated new
  subsystem, it is (almost certainly) one more internal error path inside a
  function this project already has two proven, permanent surgical patches
  in.
- **What was NOT completed**: pinning down the *specific* conditional branch
  that funnels execution into this exact message block (as opposed to any
  of the several sibling blocks visible in the same disassembly window, each
  an identical 16-byte `MOVZ w0,#0 / ADRP / ADD / B`-shaped case for a
  *different* message — this function evidently compiles a long chain of
  distinct failure conditions, not a single simple bounds-checked jump
  table, since the individual case blocks are reached via scattered
  `CBZ`/`CBNZ`/`TBZ`/`B.cond` instructions earlier in the function rather
  than one computed indirect branch). Isolating the *exact* guarding branch
  with confidence — as opposed to guessing and risking an over-broad patch
  like gate #1's own documented caveat — would benefit from the same
  two-phase live-breakpoint technique used throughout this project (arm a
  breakpoint at `0xfffffff0092b134c`, the block's own entry, alongside the
  handful of sibling case-block entries at the same 16-byte stride
  immediately before it, then correlate which one actually fires against a
  real triggered kill) rather than more static guessing — a live,
  bounded-scope task well suited to a future session with fresh time budget,
  not attempted here given how much of this session's budget had already
  gone into Part 1's live test and its transfer-corruption recovery.

**Reasoning for not completing either direction live this session**: both
are now substantially de-risked (tooling proven for (a); exact
string/function region pinned for (b)) but both still need real additional
work (CodeResources format handling for (a); live breakpoint correlation for
(b)) that would have meaningfully extended an already-long session already
carrying real risk (the transfer corruption, the multi-step
unlock-and-navigate sequence) — consistent with this project's own standing
preference for a well-documented, honestly-scoped stopping point over a
rushed, unverified patch attempt on the live guest.

### Cleanup and final state

`StocksWidget` was restored to the real original binary via `dd
if=/StocksWidget.orig of=<real path>` (**no** `conv=notrunc`, correctly
truncating back down from this session's 69304-byte replacement to the
original's exact `451200` bytes) — verified via a follow-up `wc -c` reading
back exactly `451200`. Final sanity checks, all passing: QMP `info status` →
`running`; `/sigkill_test` → `Segmentation fault: 11` (gates #1-#4 patches
intact); `/compute_test` → `IOServiceOpen succeeded... result = 42 (expect
42)`. Gate #6 remains live-patched in memory only (unchanged from the prior
session — see that section's own note; a `SIGKILL_GATE_PATCHES` table entry
for it was added to `patch_kernelcache.py` this session as prepared-but-
dormant groundwork, deliberately not executed/baked — see that commit's own
message for the reasoning: avoiding a kernelcache rebuild against an
unrelated, uncommitted local `resolve.py`/`parse_obj.py` diff already
sitting in this working tree from an unknown earlier session, rather than
risk the live guest on an unvetted rebuild mid-task).

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

**UPDATE 2026-07-30 (later session): this workaround is likely OBSOLETE
now — read the "gates #2-#5" update further down first.** All five
exec-time kill gates (this AMFI one plus four more found in the same later
session, two more in AMFI and two in a completely separate Sandbox.kext
hook) are now live-patched, and plain direct `execve()` of unsigned test
binaries has been verified working end-to-end with real functionality
(`IOServiceOpen`, GPU compute/draw dispatch, all correct). **New test
logic going forward should probably just be a normal standalone
executable again, not a bash loadable builtin** — try direct exec first;
only fall back to the builtin trick below if something about that
specific new test still gets killed (would indicate a still-undiscovered
sixth gate). The bash-builtin section below is kept for reference/history
and because it may still matter for anything that specifically needs to
run *inside* `/bin/bash`'s own already-established process (e.g. its
particular entitlements or credentials), but it is no longer required
just to dodge a kill.

**THE WORKAROUND (historical — see update immediately above before using
this for new work):** the kill only affects **new process exec()**, not
**`dlopen()` of an unsigned dylib from an already-running, already-trusted
process**.
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

### UPDATE 2026-07-30 (later session): gates #2-#5 found, all patched —
### unsigned execve() now works completely, end-to-end, verified with real test binaries

Picked up exactly where the previous session left off. **Re-verified state
first** per the coordination note: `ps aux | grep qemu-system` showed the
*same* QEMU pid the previous session had been using (no restart happened
in between), and running `/sigkill_test` confirmed gate #1's patch was
still live (got the AMFI diagnostic message, not a silent kill) — so no
re-patching of gate #1 was needed before starting.

**Gate #2a: found and patched.** Used file-offset→VA arithmetic (same
segment-walk approach as `resolve.py`'s `va2off`, just inverted) to locate
the three literal `"AMFI: hook..execve() killing pid %u: ..."` reason
strings in `__TEXT,__cstring`, then wrote a small ADRP+ADD/LDR scanner
(`find_adrp_refs.py`, not committed — lived in scratch) over the whole
`__TEXT_EXEC __text` section (`0xfffffff0079ec000`, size `0x1def9c0`) to
find which function actually references each string's address. All three
led to the same function: **`_cred_label_update_execve`**
(`0xfffffff0082f2a30`–`0xfffffff0082f2f84`, 0x554 bytes) — this is
**AMFI's own implementation of the `cred_label_update_execve` MACF policy
hook** (matches xnu's `mpo_cred_label_update_execve_t` signature: `(ucred*
old, ucred* new, proc*, vnode*, off_t, vnode* script, label*, label*,
label*, u_int *csflags, void*, size_t, int *disjointp)`). Extended
`mini_disasm.py` with ADRP/ADR/BLR/BR decoding and hand-disassembled the
whole function (dumped bytes saved to
`/tmp/.../scratchpad/cred_label_update_execve.bin` — scratch-only, not
committed, would need re-dumping in a future session). Also had to
correctly decode one `CCMP` (conditional-compare) instruction
(`0xfa401800`) that a naive read would have misread as an unconditional
compare — decoded it bit-field-by-bit-field in Python to get the real
short-circuit `(w23 != 0 && x0 == 0)` semantics right before trusting any
conclusion about the surrounding control flow.

Found the exact gate: `_cred_label_update_execve` loads `*csflags` into
`w8` (via a stack-spilled pointer, `x23 = [x29+0x18]`, one of the hook's
own arguments) and does:
```
0xfffffff0082f2b18: tbnz w8, #25, 0xfffffff0082f2b34   ; bit set -> skip kill, continue
0xfffffff0082f2b1c: mov x0, x20                         ; (kill path) get pid
0xfffffff0082f2b20: bl   0xfffffff007e72df4
0xfffffff0082f2b24: str  x0, [x31, #0x0]
0xfffffff0082f2b28: adrp x0, 0xfffffff0073ab000
0xfffffff0082f2b2c: add  x0, x0, #0x8f                  ; "...dyld signature cannot be verified..."
0xfffffff0082f2b30: b    0xfffffff0082f2d14             ; shared kill+log call
```
**Live-verified before touching anything**: breakpointed both
`0xfffffff0082f2b28` (kill path) and `0xfffffff0082f2b34` (skip-kill
path), ran ~45s of idle boot activity as a clean baseline (many hits at
the skip-kill address, bit25 always 1, zero hits at the kill address),
then triggered `/sigkill_test` and got a hit at the kill address with
`w8=0x300` → **bit25=0**, confirming causality precisely. Patched
`tbnz w8,#25,...` (bytes `e8 00 c8 37`) into an unconditional
`b 0xfffffff0082f2b34` (bytes `07 00 00 14`, same target, same technique
as gate #1's `cbz`→`b`). Verified: `/sigkill_test` no longer produces the
"dyld signature cannot be verified" message.

**Gate #2b (a second, independent check inside the SAME function): found
and patched.** Re-running `/sigkill_test` after gate #2a's patch produced
a *different* kill message: `"AMFI: hook..execve() killing pid N: no code
signature"` — exactly the "there could be a third gate" scenario this
task's instructions warned about. Traced it to the same function, a bit
further down: right after gate #2a's skip-kill target, the function calls
`bl 0xfffffff0082f2f84` (the very next symbol,
`StaticPlatformPolicy<...>::loadEntitlementsFromVnode`, called with
`(&dict_out, vnode, offset, &errmsg_out)`), then:
```
0xfffffff0082f2b8c: tbz w0, #0, 0xfffffff0082f2cfc   ; bit CLEAR -> kill (generic "%s" message, errmsg_out)
```
The kill path here uses the OUT-PARAM string from `loadEntitlementsFromVnode`
itself (`"no code signature"` — an accurate description, since this
project's CI-built test binaries genuinely have no `LC_CODE_SIGNATURE` at
all) fed through a generic `"AMFI: hook..execve() killing pid %u: %s\n"`
format, rather than one of the three hardcoded strings gate #2a used.
Live-verified the same way (breakpoint + idle baseline + triggered hit):
saw `w0=1` (bit0 set) for an ambient/pre-existing process and `w0=0` for
the freshly-triggered one. **Patch technique differs slightly from gate
#2a here**: since the branch's own *fallthrough* (not-taken) address is
already the success continuation, the minimal correct patch is a plain
**NOP** (`d503201f`) over the `tbz` (bytes `80 0b 00 36` → `1f 20 03 d5`),
not a retargeted branch — "always don't take this branch" and "NOP the
branch" are the same outcome here, and NOP is simpler/more obviously
correct than computing a same-target branch. Verified: the "no code
signature" message also stopped appearing.

**Gate #3 (a THIRD, entirely separate kext): found and patched.**
Re-running `/sigkill_test` again after gate #2b's patch produced **yet
another** kill, this time completely silent again (matching the *original*
pre-gate-#1 symptom exactly) — `dmesg` revealed why: `"Sandbox:
hook..execve() killing <unsigned>[pid=N, uid=0]: only launchd is allowed
to spawn untrusted binaries"`. Critically, this message never reaches the
interactive serial console the way AMFI's messages do (it only shows up in
`dmesg`), which is why `/sigkill_test`'s own output looked identical to
the original mystery — **worth remembering for any future gate: always
check `dmesg` after a silent kill, not just the triggering shell's own
output**, since not every kext's log line makes it to the tty.

This is **Sandbox.kext's own, separate implementation** of the exact same
`cred_label_update_execve` MACF hook AMFI implements — a different C
function, in a completely different kext's address range:
**`_hook_cred_label_update_execve`** (`0xfffffff0092b0e54`–
`0xfffffff0092b1454`, 0x600 bytes; note the AMFI hook is named
`_cred_label_update_execve` with no `_hook_` prefix — easy to conflate the
two by name alone, don't). Found the same way (string → ADRP/ADD scan →
enclosing symbol via `kernel-symbols.txt`). The gate:
```
0xfffffff0092b0ef0: mov x0, x22            ; x22 = vnode
0xfffffff0092b0ef4: bl   0xfffffff007e739b4
0xfffffff0092b0ef8: mov  x20, x0
0xfffffff0092b0efc: bl   0xfffffff007e7428c
0xfffffff0092b0f00: cbz  w0, 0xfffffff0092b0fdc   ; w0==0 -> kill ("only launchd is allowed...")
```
Live-verified: breakpointed `0xfffffff0092b0f00`, triggered
`/sigkill_test`, got `w0=0` on the very next hit (round 16, ~2s after the
trigger — this address had never been breakpointed before so there was no
leftover-breakpoint noise to wade through, unlike gates #2a/#2b). Patched
with a NOP (same reasoning as #2b): bytes `e0 06 00 34` → `1f 20 03 d5`
at `0xfffffff0092b0f00`.

**Gate #4 (a fourth check, same Sandbox.kext function): found and
patched.** Re-testing after gate #3's patch produced **another** silent
kill; `dmesg` showed `"Sandbox: hook..execve() killing <unsigned>[pid=N,
uid=0]: outside of container && !i_can_has_debugger"` — a distinctive,
almost debug-assertion-style message, clearly a deliberate sandbox
**containment** policy (as opposed to AMFI's pure code-signing checks):
roughly "an unsigned binary can only run if it's inside its declared
sandbox container, or the caller holds a debugger entitlement". Found in
the same function, further down (this function is a long chain of
sandbox-profile-string checks, each following the same
`adrp+add(profile-name-string)+bl 0x8125d80(intern?)+bl
0x7b53960(sandbox_check_profile-style call)+cbz` pattern — the target
message's specific gate):
```
0xfffffff0092b1268: ldr  w8, [x31, #0x3c]              ; flag set once, early in the function
0xfffffff0092b126c: cbz  w8, 0xfffffff0092b132c         ; w8==0 -> kill ("outside of container...")
```
Live-verified the same way: breakpointed `0xfffffff0092b126c`, triggered,
got `w8=0` on the very next hit (round 76, ~9s after trigger — some
leftover noise from earlier sessions' breakpoints, but far less than
gates #2a/#2b). Patched with a NOP: bytes `08 06 00 34` → `1f 20 03 d5` at
`0xfffffff0092b126c`.

**After all five patches (gate #1 + #2a + #2b + #3 + #4), `/sigkill_test`
no longer gets killed by the security stack at all** — no more `Killed:
9`, and no more AMFI/Sandbox log lines in `dmesg` for any subsequent
trigger. Its new failure mode is `Segmentation fault: 11` (exit 139) — a
**completely different, unrelated class of failure** (a real userspace
crash, signal 11, not a policy-driven signal 9). This is expected to be a
bug/limitation specific to that one minimal test binary (its source isn't
in this repo — likely built ad-hoc in an earlier session and only the
compiled binary was ever transferred to the guest) rather than a sign of
a sixth gate, confirmed by the next paragraph.

**End-to-end verification with REAL functional test binaries** (already
present on the guest from earlier sessions, at `/compute_test`,
`/draw_test`, `/metal_api_test`, `/agx_functional_test` — all built the
same unsigned way as `/sigkill_test`, i.e. equally subject to every gate
above): **all four ran to completion with exit code 0 and fully correct
results**, e.g. `/compute_test`: `IOServiceOpen succeeded... result = 42
(expect 42)`; `/draw_test`: full render pipeline, `ALL CHECKS PASSED`;
`/metal_api_test`: full compute pipeline, `ALL CHECKS PASSED`;
`/agx_functional_test`: `0 failure(s)`. This is a major milestone beyond
just "fixing the SIGKILL bug" — **it's the first time this project's real
Metal test suite has run via genuine, direct `execve()`** rather than the
`dlopen`-from-bash-builtin workaround, and notably `/compute_test`'s
`IOServiceOpen` succeeded *without* hitting the separate
`com.apple.security.iokit-user-client-class` entitlement wall documented
above — strong evidence that wall is specific to `/bin/bash`'s own signed,
sandboxed profile (as the existing writeup already suspected: "raw
unsigned standalone test binaries... apparently run with no sandbox
profile attached at all"), not something every unsigned process hits.

**System stability re-checked after all five patches**: `uptime` (1:00,
climbing normally), `ps aux` (175 procs), `dmesg` scanned for
panic/assert (nothing), QMP `info status` (`running`, not paused) — all
normal.

**Methodology note for future sessions — a real gdbstub gotcha hit and
fixed this session**: a naive loop of repeated GDB RSP `c` (continue)
calls **will infinite-loop forever** the instant it actually hits one of
its own software breakpoints, because a software breakpoint is a patched
trap instruction left in memory — `continue` alone just re-executes and
re-traps on the exact same instruction forever; the PC never advances.
(First symptom: a breakpoint that should fire once appeared to fire 600+
times in a row with byte-for-bit identical register state — that's the
tell.) The fix, standard for any minimal RSP client: on a hit, **remove
the breakpoint (`z0,addr,4`), single-step once (`s`), then reinsert it
(`Z0,addr,4`)** before continuing. Also reconfirmed the earlier session's
note that QEMU's gdbstub breakpoint list **persists across TCP
reconnects** — a fresh RSP connection to the same long-lived QEMU process
can and does still have dozens of stale breakpoints from every earlier
debugging session in this project's history armed and firing; budget
extra rounds (hundreds, not tens) and don't assume a stop at an
unexpected PC means anything went wrong, it's very likely just old noise.

**Caveat, same as gate #1 (RESOLVED — see the dated update immediately
below the table)**: all five patches (gate #1's `load_machfile` fix plus
this session's four) were **live-memory-only** and would be lost on the
next QEMU restart, exactly like gate #1 — they were **not** baked into
`kernelcache.vgpu2.patched` via `patch_kernelcache.py` (that script was
previously special-purpose, only handling the `InfernoVGPUHello`
constructor-redirect injection, not a general byte-patch mechanism). A
future session wanting permanence should extend `patch_kernelcache.py`
with a small table of `(file_offset, original_bytes, new_bytes)` entries
and apply all five (plus gate #1's) as a matter of course when
regenerating `kernelcache.vgpu2.patched`. All five patch addresses/bytes,
for that table:
| # | VA | orig bytes | new bytes | meaning |
|---|----|-----------|-----------|---------|
| 1 | `0xfffffff007eef788` | `a0 00 00 34` | `05 00 00 14` | `load_machfile`: `cbz`→`b`, ignore `parse_machfile` failure |
| 2a | `0xfffffff0082f2b18` | `e8 00 c8 37` | `07 00 00 14` | AMFI hook: `tbnz`→`b`, ignore csflags bit25 (dyld sig check) |
| 2b | `0xfffffff0082f2b8c` | `80 0b 00 36` | `1f 20 03 d5` | AMFI hook: NOP `tbz`, ignore loadEntitlementsFromVnode failure |
| 3 | `0xfffffff0092b0f00` | `e0 06 00 34` | `1f 20 03 d5` | Sandbox hook: NOP `cbz`, ignore "only launchd..." check |
| 4 | `0xfffffff0092b126c` | `08 06 00 34` | `1f 20 03 d5` | Sandbox hook: NOP `cbz`, ignore "outside of container..." check |

(Scratch-only scripts from this session, not committed since they're
throwaway/reusable-pattern rather than reusable-as-is: `off2va.py`
(file-offset→VA, inverse of `resolve.py`'s `va2off`), `find_adrp_refs.py`
(ADRP+ADD/LDR reference scanner given a target VA), `verify_gate2.py` /
`verify_gate2b.py` / `verify_gate3.py` / `verify_gate4.py` /
`verify_gate5.py` (breakpoint+trigger+correlate verification harnesses,
each a small variation on the same pattern — worth consolidating into one
parameterized script if this pattern gets used again), `patch_gate2.py`
/ `patch_gate3.py` / `patch_gate4.py` / `patch_gate5.py` (the actual
live-memory patch appliers) — all under
`/tmp/.../scratchpad/` per session convention, gone once the scratchpad is
cleaned up; the extended `mini_disasm.py` (with ADRP/ADR/BLR/BR support
added this session) is the one piece worth recreating first in any future
session that needs to read more of this kernel's compiled code.)

### UPDATE 2026-07-30 (later session): all 5 patches now PERMANENTLY baked
### into the kernelcache file, verified with a real QEMU restart

Extended `patch_kernelcache.py` with a small, clearly-separated
`SIGKILL_GATE_PATCHES` table (the exact 5-row table above) and a loop that
applies each as a straight file-offset byte replacement, reusing the
script's own existing `va2off()` helper (same one already used for the
`InfernoVGPUHello` ctor-redirect injection). Each patch asserts the
original bytes at its computed file offset exactly match the table before
writing — this fired correctly (all 5 passed) confirming the file offsets
and the underlying kernelcache build genuinely match what the live-memory
investigation was patching.

Before running: backed up the previous known-good
`kernelcache.vgpu2.patched` to `kernelcache.vgpu2.patched.backup_pre_
sigkill_bake` (the script itself has no built-in backup/versioning, so
this was a manual `cp` first — worth remembering for next time too, the
script always overwrites `KC_OUT` unconditionally).

Ran `python3 patch_kernelcache.py`: all 5 gate-patch assertions passed,
personality-hijack injection still applied normally afterward, wrote a new
`kernelcache.vgpu2.patched` (same file size, `54613232` bytes, different
md5 from the backup as expected).

**Verified with a REAL QEMU kill+relaunch** (not just trusting the code —
live-memory patches from earlier sessions "worked" too until the process
died, so only a genuine restart proves anything): killed the running QEMU
process, relaunched via the standard `launch_shell.sh` playbook
(`launch_shell_stdoutJJ.log`). Boot came up clean — no panics in
`dmesg`, the `InfernoVGPUHello` present-dispatch retry thread visibly
active in QEMU's own stdout log (proves the personality-hijack injection
also survived correctly, not just the new gate patches), guest shell
reachable via `guest_tools/shell_cmd.py`, `/sbin/mount -uw /` succeeded.

Then, **with zero GDB attached at any point**:
- `/sigkill_test` → `Segmentation fault: 11` (exit 139), **not**
  `Killed: 9` — the exact same benign unrelated-bug failure mode already
  documented above as the *expected* post-all-5-patches result. `dmesg`
  confirmed no AMFI/Sandbox kill message either.
- `/compute_test` → `IOServiceOpen succeeded, connection=0x130b` /
  `ComputeDispatch returned 4 bytes` / `result = 42 (expect 42)`, exit 0.
- `/draw_test` → full render pipeline, every `CHECK` line `OK`, `center
  pixel RGBA = 255,0,0,255` / `corner pixel RGBA = 0,0,0,0`, `ALL CHECKS
  PASSED`, exit 0.

This conclusively proves the patches are genuinely file-baked, not an
artifact of leftover GDB state or a QEMU process that merely looked fresh
— a real kill+relaunch cycle was completed first. The `patch_
kernelcache.py` changes (new `SIGKILL_GATE_PATCHES` table + application
loop, ~55 lines) are the only code change; the byte values themselves are
unchanged from the table already documented above, so no new addresses to
track.

Also independently re-verified as part of this same session, before the
restart: the `___MTLCreateSystemDefaultDevice_block_invoke` DSC patch
bytes at guest file offset `0x170506fc` decode to the expected `sub
sp,#0x20` / dlopen-glue instruction sequence from `patch_block_invoke.py`'s
`build_patch()`, confirming that patch (disk-resident, unrelated to this
kernelcache work) also remained intact across the restart, as expected
since it's written directly to the guest's persistent root disk image, not
memory-only.

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

## Playbook: running new test logic

**UPDATE 2026-07-30: try plain direct exec first now** (see the "gates
#2-#5" update in the SIGKILL section — all known exec-time kill gates are
patched and 4 real test binaries now run cleanly via normal `execve()`).
Only fall back to the bash-builtin pattern below if a specific new test
still gets killed.

1. Write a new standalone `main()`-based test binary (any of
   `src/userspace_test/*.m`'s non-builtin examples), or, for the
   historical bash-builtin route: a `.m` file implementing bash's `struct
   builtin` ABI (see `src/userspace_test/bash_present_builtin.m`).
2. Add/reuse a CI step compiling it (plain executable, or `-dynamiclib`
   for the builtin route — see the `agx-bridge-dylib` job in
   `.github/workflows/build.yml` for the builtin pattern).
3. **UPDATE 2026-07-31: transfer straight to `/`, not `/tmp`** — the
   "`/tmp` is fine for a plain executable" claim this step used to make
   here was an untested assumption, now live-falsified (see the MKMapSnapshotter
   dated update above): running a freshly-transferred plain executable from
   `/private/var/tmp` can hit a real, distinct Sandbox.kext
   `process-exec*` denial (`System Policy: <proc> deny(1) process-exec*
   /private/var/tmp/<name> ... failed to apply exec policy`) — a different
   check from all 5 already-patched SIGKILL gates. Every binary in this
   project's history that's actually been confirmed working via direct
   `execve()` was, on inspection, always deployed to `/` anyway. If already
   transferred to `/tmp`, no re-transfer needed — just `cp
   /tmp/whatever_test /whatever_test; chmod 755 /whatever_test` guest-side
   first (same fix shape as the already-documented `/tmp`-mmap-block fix
   for the bash-builtin route below). The builtin route already required
   `/`, not `/tmp`, for a different reason (mmap-for-exec sandbox block) —
   this update means *both* routes now need `/`, not just the builtin one.
4. Run it directly (`/whatever_test`), or, for the builtin route: `enable
   -f /path/to/whatever.dylib my_builtin_name` then run `my_builtin_name`.

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
- `qmp_raw.py` — native QMP client (not just HMP passthrough like
  `qmp_client.py`) with `screendump`/`tap`/`swipe`/`status` helpers, for
  driving the emulated touchscreen (`hw/arm/apple-silicon/mt-spi.c` via
  `input-send-event`, abs range `0..0x7FFF` over the display's real
  828×1792 pixel dimensions). `swipe()` does a real multi-step held drag
  (not a single jump) since the touch device reconstructs gestures from a
  stream of intermediate positions. Importable (`from qmp_raw import QMP`)
  or standalone (`qmp_raw.py screendump|tap|swipe|status ...`).
- `tap_maps_watch.py <deadline_s> <x> <y> <label>` — arms the 10 sandbox
  vnode-check candidates from the MapKit `/b` investigation, the 6-address
  `MTLCreateSystemDefaultDevice`/block_invoke chain, and
  `handle_user_abort`/`exception_triage`, all at once, fires a QMP tap at
  `(x, y)` 3s in, then watches every hit until `deadline_s` elapses,
  writing a JSON summary to `tap_watch_summary_<label>.json`
  (gitignored). Reusable template for "arm breakpoints, trigger an action,
  watch a bounded window" — swap in different candidate sets/trigger
  actions for other investigations.
- `run_mapkit_test_watch.py <deadline_s> [label]` — direct sibling of
  `tap_maps_watch.py`, same full candidate set, trigger swapped from a QMP
  tap to running `/mapkit_test` over a second, separate serial connection
  (127.0.0.1:4444) instead of the GDB debug port (127.0.0.1:1234) — the
  two-phase "arm on one connection, trigger on another" pattern used
  throughout this project. Written for the MKMapSnapshotter direct-trigger
  test, see that section's dated update.
- `run_mapkit_test_abort_only.py <deadline_s>` — leaner variant of the
  above, arms only `handle_user_abort`+`exception_triage` (drops the other
  16 addresses) specifically to reduce GDB-breakpoint-induced dilation for
  a tighter trigger-to-fault-PC correlation — this is what actually caught
  the single, isolated `mapkit_snapshotter_test` crash PC documented in
  that same section.

## From-scratch `dyld_shared_cache` parser (`dsc_parse.py`) — the "no ipsw"
## gap is finally closed (2026-07-31, new session)

This doc has referenced "no `ipsw`/DSC-parser tooling on this Linux host"
as a blocking gap in at least a dozen separate places across totally
different investigations (dyld's own lazy-binding internals, QuartzCore's
private Metal-vs-software backend selection, MapKit's private snapshot
backend, WidgetKit's registration protocol, the pre-`main()` dyld crash).
**That gap is now closed.** `dsc_parse.py` (repo root) is a hand-decoded
Python parser, same `struct.unpack_from` approach as `parse_obj.py`/
`resolve.py` — no `ipsw`, no `macholib`, no external dependency at all.

**Format reference used**: `apple-oss-distributions/dyld` tag
`dyld-832.7.1` — the same tag this project's own dyld-crash-investigation
section (above) already pinned as timestamp-matched to this project's xnu
build (`xnu-7195.50.7.100.1`, both tag objects minted 90 seconds apart by
the same automated Apple OSS bot). Specifically:
- `dyld3/shared-cache/dyld_cache_format.h` — `dyld_cache_header`,
  `dyld_cache_mapping_info`, `dyld_cache_image_info`,
  `dyld_cache_image_text_info`, `dyld_cache_local_symbols_info`/`_entry`.
- `dyld3/MachOLoaded.cpp` (`getExportsTrie`) — how a per-image
  `LC_DYLD_INFO[_ONLY]`/`LC_DYLD_EXPORTS_TRIE` load command's `export_off`/
  `dataoff` combines with that image's own `__LINKEDIT` segment to locate
  its export trie's real bytes.
- `dyld3/shared-cache/Trie.hpp` (`processExportNode`) — the export trie's
  on-disk node format (ULEB128 terminal size/flags/address, then a
  child-count byte and `(cstring edge, ULEB128 child-offset)` pairs) —
  identical to the export trie format used in any standalone Mach-O's
  `LC_DYLD_INFO`, no DSC-specific differences.

A full local shallow clone of that tag was already sitting in this
session's own scratchpad (`scratchpad/dyld_full/`) from the prior
dyld-crash-investigation session sharing this same scratchpad — reused
directly rather than re-fetched.

**One real implementation wrinkle worth recording**: `MachOLoaded::
getExportsTrie`'s live-pointer arithmetic (`this + (linkeditVMAddr -
textVMAddr) + offsetInLinkEdit`) implicitly assumes the whole cache is one
flat mapping where a VM-address delta always equals the same file-offset
delta. **That assumption does NOT hold for this cache** — `dsc_parse.py`
checks it explicitly (`DSC.flat`) and it comes back `False`: this cache's 4
mappings (`__TEXT`/`__DATA*`/`__LINKEDIT`, r-x/rw-/r--) are laid out
back-to-back with zero gaps in the **file**, but have real, non-contiguous
gaps between them in **VM address space** (e.g. a 32MB gap between the end
of the TEXT mapping and the start of the DATA mapping). `dsc_parse.py`
therefore does real per-mapping `vmaddr → fileOffset` translation
(`DSC.vm_to_file`, a linear scan of the 4 mappings) rather than the
single-formula shortcut — the general, always-correct form, not a
DSC-specific hack.

### Validation against this project's own independently-known ground truth

Two separate, previously-recorded VAs (both from this project's own prior
live-GDB work, not from this session) were used as blind validation
targets:

```
$ python3 dsc_parse.py sym2addr _MTLCreateSystemDefaultDevice Metal.framework/Metal
_MTLCreateSystemDefaultDevice = 0x1970505d0  flags=0x0  image=/System/Library/Frameworks/Metal.framework/Metal

$ python3 dsc_parse.py addr2sym 0x1970506e4
0x1970506e4 = ___MTLCreateSystemDefaultDevice_block_invoke + 0x0   (sym@0x1970506e4)  image=/System/Library/Frameworks/Metal.framework/Metal
```

Both are **exact matches** to the addresses already on record earlier in
this doc (the system-wide-patch section's `0x1970505d0`, and the
`agx_system_metal_test` crash-investigation section's `0x1970506e4`
block_invoke breakpoint address). The second one is notable beyond being a
correct match: `___...block_invoke` is a local (non-exported) symbol —
it's not in Metal's export trie at all, and only got resolved because
`dsc_parse.py` also parses the cache-wide `dyld_cache_local_symbols_info`
nlist table (per-image `nlistStartIndex`/`nlistCount` keyed by the image's
file offset) and folds it into `addr2sym`'s nearest-symbol search — i.e.
both major code paths (export-trie symbol lookup, and cache-wide local
`nlist` reverse lookup) got real, independent validation in the same pass.

### Where the DSC file itself came from (important: not re-derived this
### session, and the live guest was never touched)

`InfernoData/dyld_shared_cache_arm64e` (2,052,489,216 bytes, magic
`dyld_v1  arm64e`) **was already present on this host**, dated 2026-07-29
17:07 — from an earlier session's own transfer (`InfernoData/dyld_scp.log`
records `dyld_cache.zst : 2052489216 bytes`; a sibling `scp-bashed.log`/
`scp-strap.log` pair from the same day show an (also earlier, separate,
much larger — 32GB) attempt at pulling the guest's entire `root` disk
image the same way). **This session did not re-derive it, did not touch
the live guest's serial console (4444), QMP socket, or GDB port (1234) at
all**, and did not touch `InfernoData/root` (the guest's live raw APFS
disk image) either — everything above came from reading the
already-on-disk DSC file directly, plus real Apple source fetched from
GitHub. `InfernoData/dyld_shared_cache_arm64e.a2s` (434MB, also already
present) was **not used** — confirmed still what the earlier note in this
doc said it was (`ipsw`'s own undocumented address-to-symbol cache
format), genuinely irrelevant now that a real parser exists independent of
it.

**For whoever needs a *different* guest file next** (the dyld-crash
section's own next-step #3 specifically wants `/usr/lib/dyld` itself,
which is NOT inside the DSC on this platform): this host turns out to
already have real, offline APFS read tooling installed —
`libfsapfs-utils` (`fsapfsmount`, a FUSE-based read-only mounter, and
`fsapfsinfo`), both in `/usr/bin`, no sudo needed to run (FUSE mounts as a
regular user). There's also an `apfs-dkms` package installed but its
`dpkg` status is `half-configured` (its kernel module likely never
finished building/loading) — irrelevant since the userspace FUSE tool
doesn't need it. **Not exercised this session** (no need — the file this
session needed was already sitting on the host) and, per this task's own
explicit safety instruction, **not tested against the live `InfernoData/
root` file either**, even read-only, to avoid any risk to the concurrent
session's use of that same file. If a future session needs it: copy
`InfernoData/root` first (local disk I/O, not guest-console-bound, so a
32GB copy is practical), then `fsapfsmount`/`fsapfsinfo` the *copy*, never
the live file.

### Backlog question answered: WidgetKit's real extension-registration
### protocol (directly useful to the concurrent widget-hosting session)

The widget-hosting investigation earlier in this doc got as far as: real
WidgetKit extensions have a genuine compiled `main()` (not
`-e _NSExtensionMain`), consistent with Swift's `@main`-attributed
`WidgetBundle` conformer, and concluded it has "a different registration
story with whatever WidgetKit's own host process expects, which is
unknown without DSC introspection of `WidgetKit.framework` itself." That
introspection is now done. `WidgetKit.framework` is at cache address
`0x1c0fbf000` (`/System/Library/Frameworks/WidgetKit.framework/WidgetKit`)
with 1050 exported symbols and 1196 local (non-exported) symbols, both
fully enumerable now via `dsc_parse.py dump-exports`/internal
`_local_symbols_for_image`. Findings, all address/string-backed:

- **The real entry point Swift's `@main` generates a call to**:
  `WidgetBundle.main()` — exported as
  `_$s7SwiftUI12WidgetBundleP0C3KitE4mainyyFZ` @ `0x1c100957c`. This is
  what a widget extension's autogenerated top-level `main()` actually
  calls (a `SwiftUI.WidgetConfiguration`-family static/extension method
  supplied by WidgetKit itself) — confirms structurally, from the
  framework side, what the binary-side Mach-O inspection already implied.
- **The real daemon behind WidgetKit is called `chronod`**, part of a
  private `ChronoServices.framework`
  (`/System/Library/PrivateFrameworks/ChronoServices.framework/
  ChronoServices`, referenced directly from WidgetKit's own `__TEXT`), with
  mach service name **`com.apple.chronod`** — all found as plain ASCII
  strings in WidgetKit's `__TEXT` (e.g. `0x41044df0`). WidgetKit's own
  internal version string, also found in `__TEXT` @ `0x4103b530`:
  `@(#)PROGRAM:WidgetKit  PROJECT:Chrono-97.1` — confirms "Chrono" is
  Apple's actual internal project name for the whole WidgetKit
  subsystem, not a guess. This project had never previously identified
  `chronod`/`ChronoServices` anywhere in this doc.
- **Two distinct, separately-named XPC connections**, both plain ASCII
  strings in `__TEXT`: `com.apple.chrono.widgetcenterconnection` (
  @`0x41043cf0`, matches the exported `WidgetCenter.serviceName`/
  `WidgetCenter.configuredHostXPCInterface` static getters @ `0x1c0ff8acc`/
  `0x1c0ff8ae8` — this is the app-facing side apps use via
  `WidgetCenter.shared.reloadAllTimelines()` etc.) and
  `com.apple.chrono.avocadocontrollerconnection` (@`0x41044600` — purpose
  not fully identified this session; "Avocado" reads like a second
  internal codename, plausibly the widget gallery/configuration-picker
  surface, but this is a guess, not confirmed).
- **The actual host↔extension XPC contract is a matched protocol pair**:
  `HostToExtensionXPCInterface` / `ExtensionToHostXPCInterface` (both
  `WidgetKit`-namespaced Swift protocols, symbol strings present both
  old-style-mangled and new-style-mangled in `__TEXT`, e.g.
  `$s9WidgetKit27HostToExtensionXPCInterfaceP` @ `0x41048054`), set up via
  `NSXPCInterface`/`NSXPCConnection` (`WidgetKit/XPCInterfaces.swift` is
  literally named in a nearby string). An adjacent error string pins the
  failure mode this project would actually observe if a spoofed/incomplete
  session tried to skip this: `"[%{public}s-%{public}s] Unable to create
  new WidgetExtensionSession: xpc connection was nil."` (@`0x41045030`).
- **The actual operation vocabulary of that XPC contract** is visible
  directly as the case list of an exported Swift enum,
  `WidgetKit.ExtensionSessionOperation` (`O` suffix = Swift enum type),
  whose case-witness symbols spell out the operation names verbatim:
  `getDescriptors`, `getTimeline`, `getPlaceholders`,
  `attachPreviewAgent`, `handleURLSessionEvents` — i.e. this is the exact
  set of requests a host (`chronod`, presumably, or whatever ends up
  brokering it) can make of a running widget extension process: ask what
  widget kinds it declares, ask for its actual `TimelineProvider` output,
  ask for gallery placeholders, attach a live preview agent, and deliver
  background `URLSession` completions.
- **Process lifecycle goes through RunningBoard, not classic
  ProcessAssertion/NSExtensionContext**: `WidgetKit.
  ExtensionSessionFactory.makeSession(for:requiresUserInteractive:
  priority:watchdogTimeoutProvider:suspensionObserver:completion:)`
  (sync @ `0x1c101573c`, async variant @ `0x1c1015824`) constructs the
  session and hands back an `ExtensionSessionAssertionInvalidatable`/
  `ExtensionSessionSuspensionObserving` pair, both witnessing a real
  `RBSAssertion` (`RunningBoardServices.framework`, also directly
  `__TEXT`-referenced) — i.e. the widget extension process is kept alive
  via a genuine RunningBoard assertion for the duration of a session, with
  an explicit `DefaultWatchdogTimeoutProvider` for the timeout case, not
  the older `NSExtensionContext`/backboardd assertion style this project's
  `inferno_widget_host.m` prototype was built around.
- **A runtime self-check exists and is a plausible instrumentation/gating
  point**: `+[WidgetExtensionChecker isExtensionSubsystemInitialized]`
  (Objective-C class method, found via the local-symbols table, not
  exported) @ `0x1c0fc1394`, backing the exported
  `_OBJC_CLASS_$_WidgetExtensionChecker` @ `0x1deac5508`. Worth
  breakpointing live in a future session to see exactly when/how often
  it's actually called and what gates on its result.
- Corroborating structural evidence from the local-symbols table (1196
  entries, no `ipsw` needed to enumerate them either): a concrete
  Objective-C-backed class `WidgetKit._WidgetExtensionSession`
  (`__TtC9WidgetKit23_WidgetExtensionSession`), conforming to a
  `WidgetExtensionSession` protocol, built by
  `WidgetExtensionSessionFactory` — names match the exported
  `ExtensionSessionFactory`/`ExtensionSessionOperation` surface above
  exactly, i.e. two independent symbol sources (exports trie, local nlist
  table) tell the same consistent story.

**Net assessment for the widget-hosting thread**: this confirms, in much
more concrete detail than the prior session's binary-structure inference
alone could, that `inferno_widget_host.m`'s current design
(`NSExtensionMain`-entry ObjC principal class) is fundamentally the wrong
shape for a real WidgetKit extension in this build. A viable replacement
prototype would need: a real compiled `main()` calling something
equivalent to `WidgetBundle.main()`'s role, an `NSXPCListener` implementing
`ExtensionToHostXPCInterface` and consuming `HostToExtensionXPCInterface`
calls for at least `getDescriptors`/`getTimeline` (the two that matter for
getting *any* widget content rendered), and a `com.apple.chronod`-reachable
mach service (whether that means chronod actually launches the extension
process on demand the way `launchd`/RunningBoard normally does, or expects
the extension to already be running and just connects to it, is still
open — the exact bootstrap trigger wasn't traced this session).

### Concrete next steps for whoever picks this up

1. **Disassemble `WidgetBundle.main()` @ `0x1c100957c`** and
   `ExtensionSessionFactory.makeSession` @ `0x1c101573c` (both now
   trivially locatable via `dsc_parse.py addr2sym`/reading the raw bytes
   at their file offset via `DSC.vm_to_file`) to nail down the *exact*
   bootstrap order: does the extension process create the `NSXPCListener`
   itself and somehow publish its endpoint, or does `chronod` launch it
   with a listener endpoint already handed to it via `xpc_connection_
   create_mach_service` on `com.apple.chronod`? This is the one remaining
   structural unknown blocking a real `inferno_widget_host` rewrite.
2. **Symbolicate the pre-`main()` dyld crash** (the dyld-crash-
   investigation section above) using this same parser once
   `/usr/lib/dyld` itself is obtained (via the APFS-copy-then-`fsapfsmount`
   route described above, since it's a standalone Mach-O, not
   DSC-resident) — `dsc_parse.py`'s Mach-O load-command walk and export/
   local-symbol logic apply directly to a standalone dylib too (just skip
   the DSC-header/mapping layer and treat the whole file as one "mapping").
3. **QuartzCore's private Metal-vs-software backend-selection logic** (the
   backboardd investigation's "chase the QuartzCore-internal angle" next
   step) is now equally tractable with this same tool —
   `dsc_parse.py images QuartzCore` to find the image, then
   `dump-exports`/local-symbol dump the same way this session did for
   WidgetKit. Not attempted this session — picked WidgetKit instead since
   it's directly unblocking for the concurrent live-hosting thread.
4. `dsc_parse.py` currently only handles the "flat-ish" per-mapping cache
   layout this specific iOS 14 cache uses (4 mappings, classic
   `dyld_cache_image_info` list, non-split single file). It has **not**
   been tested against (and would need extension for) the post-iOS16
   split-subcache format if this project ever moves to a newer guest
   image — not a concern for the current T8030/iOS 14 target.

**Environment note**: this entire session was host-side/offline file
reads (the DSC file already on disk, plus GitHub source fetches) and local
`git` operations only. Zero interaction with the live guest's serial
console, QMP socket, or GDB port, and zero interaction with
`InfernoData/root`, confirmed throughout by construction (no tool in this
session's history touches ports 4444/1234 or that path).

## `CARenderer`'s real Metal backend surface found via `dsc_parse.py`, live-checked, inconclusive (2026-07-31, same day, orchestrating session direct work — no subagent)

Direct follow-up to a question the user raised mid-session: given `backboardd`'s
own binary has zero Metal symbol references (see the earlier `backboardd`/
compositor investigation), how does real hardware get GPU-accelerated blur
(`UIVisualEffectView`/vibrancy) at all? Answer worked out live using the new
`dsc_parse.py` tool, done directly by the orchestrating session (not a
subagent, per explicit user instruction) — genuinely useful new API surface
found, but the live-verification half came up empty across two separate
windows, for reasons detailed below.

**Static finding: `CARenderer`/`CARenderServer` really does have a Metal
rendering mode, distinct from the public `CAMetalLayer`/`MTLDevice` surface
already known to this project.** `dsc_parse.py dump-exports
QuartzCore.framework/QuartzCore` turned up:
- `_kCARendererMetalCommandQueue` — a `CARenderer` option-dictionary key
  (paired conceptually with `_kCARendererDeepBuffers`/`_kCARendererColorSpace`/
  `_kCARendererClearsDestination`, all real `CARenderer` init options).
- `_kCARenderMetalCallbacks`/`_kCARenderMetalCallbacksRef` **and** the
  parallel `_kCARenderSoftwareCallbacks`/`_kCARenderSoftwareCallbacksRef` —
  two parallel callback-struct registrations, strongly suggesting a real
  Metal-vs-software backend switch exists inside `CARenderServer`'s own
  internals, exactly the "QuartzCore-internal angle" the backboardd
  investigation flagged as unexplored.
- `_CARenderServerSetRootQueue` — name strongly suggestive of the actual
  backend-selection entry point (never proven — see below).
- `_CARenderBackdropCollect`/`_OBJC_CLASS_$_CABackdropLayer`/
  `_kCAFilterGaussianBlur`/`_kCAFilterVariableBlur` — confirms the real
  blur/vibrancy machinery (`CABackdropLayer`, the private class backing
  `UIVisualEffectView`) is real, present, and distinct from the widget-hosting
  work elsewhere in this doc.

**Re-checked `backboardd`'s own binary against these exact new symbol names
(same `dd`/`od`/`grep -a` technique as the original investigation) — clean
negative, and it narrows things further than before**: `grep -ao
'_CARenderServer[A-Za-z]*' /usr/libexec/backboardd | sort -u` returns
**exactly one** match, `_CARenderServerRenderDisplay` — not
`CARenderServerStart`, not `CARenderServerSetRootQueue`, not any of the
Metal/Software callback constants. **`backboardd` is a pure client of
`CARenderServer` (asks it to redraw), it does not start or configure it** —
someone/something else does, most plausibly QuartzCore's own internal
framework-load-time initialization (living in the DSC, invisible to a
same-process static string search), not any single daemon's own
hand-written code.

**Live verification attempted, twice, both negative — inconclusive, not a
clean disproof.** Armed GDB breakpoints directly (`_CARenderServerStart`
`0xfffffff18820717c`→ wait, real VA `0x18820717c`, and
`_CARenderServerSetRootQueue` `0x188207180`, both DSC-resident/fixed
addresses per this project's established KASLR-off + non-slid-DSC finding)
using a new one-off script (not committed — trivial, reused
`gdb_rsp2.py`/the existing candidate-watch pattern verbatim). Breakpointed
`_CARenderServerStart` (VA `0x18820717c`) and `_CARenderServerSetRootQueue`
(VA `0x188207180`), both DSC-resident/fixed addresses per this project's
established KASLR-off + non-slid-DSC finding:
1. **Respring-triggered window** (~70s wall-clock, breakpoints armed
   throughout, `kill -9` on the already-hours-uptime `SpringBoard` pid to
   force fresh process launches mid-window): zero hits.
2. **Full fresh-boot window**: killed and relaunched QEMU (cheap, patches
   disk-resident), armed `gdbserver tcp::1234` and both breakpoints
   *immediately* after relaunch (before the kernel meaningfully starts
   executing), then watched continuously for 300s wall-clock. **Zero hits**,
   despite the guest genuinely reaching a stable, fully-booted state by the
   end of the window (confirmed via `ps`: `backboardd` pid 60 and
   `SpringBoard` pid 57 both up, `uptime` showing `up 0:04` — i.e. ~240
   guest-seconds of real boot progress got covered, not the near-total
   dilation-driven starvation this project's own MapKit investigation
   documented for a naively-long continuously-armed window. This makes the
   negative result meaningfully stronger than a first glance suggests: this
   wasn't "the window was too short/too dilated to reach the interesting
   part," both daemons' entire startup sequence happened *inside* the
   covered range).

**Honest interpretation, not yet resolved.** Two real possibilities, not
distinguished this session:
1. **These exact C symbols are called via `objc_msgSend` dispatch (e.g. a
   hypothetical `+[CARenderServer start]` class method), not a direct `BL`
   to the C function** — a breakpoint on the C symbol's own entry would
   still have to fire even for an ObjC-wrapped call (the method
   implementation would presumably still call through to this same C
   function internally, if it's a thin wrapper) — but if the *real*
   initialization instead happens via some entirely different function this
   session didn't know to breakpoint (e.g. a private, unexported
   `_CARenderServerInitialize`-style local symbol, or logic inlined directly
   into a framework-load `+load`/static-initializer rather than a
   separately-callable function at all), that would explain a clean miss
   without contradicting the "Metal backend surface is real" finding.
2. **This specific iOS 14/T8030 build genuinely never exercises the Metal
   backend path** — `CARenderServer` may default to (or be hardcoded to,
   for this device class/build) `kCARenderSoftwareCallbacks` unconditionally,
   with the Metal path only a legacy/alternate-platform option never
   actually taken on this hardware target. This would be consistent with,
   not contradicting, the existing "software-composited-then-blit" finding
   from the original backboardd investigation.

**Concrete next steps for whoever continues this:**
1. Use `dsc_parse.py` to enumerate `CARenderServer`'s/`CARenderer`'s real
   ObjC method list (via the class's method-list structure, not yet
   supported by `dsc_parse.py`'s current two query modes — would need a
   small extension to walk `objc_class`→`objc_method_list` the same way
   `dump-exports` already walks the export trie) and breakpoint the actual
   resolved method IMP addresses instead of guessing at C symbol names.
2. Alternatively (cheaper, no new tooling): breakpoint `objc_msgSend` itself
   with a *conditional* check on the selector/class name matching
   `CARenderServer`-family strings — expensive/hot in general, but this
   project has already worked around exactly this class of problem before
   (the MapKit investigation's own "too hot to breakpoint directly, need a
   narrower target" lesson) — would need the same discipline (short,
   targeted window only, arm reactively not continuously) to avoid another
   dilation trap.
3. Simplest of all, not yet tried: `grep -a` **all** DSC-resident framework
   binaries with real backing images this build actually loads (not
   just QuartzCore) for `kCARendererMetalCommandQueue`'s literal string
   bytes as a *reference*, the same "who references this constant" trick
   already used successfully elsewhere in this doc — would need extending
   `dsc_parse.py` with a raw byte-search-across-all-images mode (a small,
   mechanical addition given the mapping/file-offset translation it already
   has).

**Environment left clean**: `/sigkill_test` → `Segmentation fault: 11`,
`/compute_test` → `result = 42`, QMP `info status` → `running`, both
breakpoints removed after each window, no dangling paused state (verified
by polling `info status` immediately after the fresh-boot window's `finally:
qmp_cont()` fired). Guest is on a fresh boot (QEMU relaunched this session,
same disk-resident patches, `kernelcache.vgpu2.patched` untouched).

### UPDATE, same session: found the real bug in the live-verification method
### itself (breakpointed an export *stub*, not the real function) — fixed and
### retried, still zero hits, real blocker now narrowed to indirect dispatch

Root-caused *why* the two live windows above came up empty, independent of
whether the Metal backend is actually used: **the addresses this session
breakpointed for `CARenderServerStart`/`CARenderBackdropCollect` were thin
export *stubs*, not their real function bodies.** Checked by reading the raw
instruction word at each export address directly out of the DSC file (own
small standalone script, `find_carender_xrefs.py`, not committed — reuses
`dsc_parse.py`'s `DSC` class for `vm_to_file` only, no new parsing
infrastructure): `_CARenderServerStart`'s exported address (`0x18820717c`)
decodes as a plain `B` (unconditional branch) instruction, i.e. it's a
veneer/trampoline, not real code — its real implementation lives at
`0x1881fba20`, reached only via following that branch. (`_CARenderBackdropCollect`
is the same shape, stub at `0x1881bc78c` → real body at `0x1881f8f88`.)
**`_CARenderServerSetRootQueue`'s exported address genuinely is real code**
(starts with a `PACIBSP` prologue, not a branch) — so that address was never
the problem for that specific symbol.

This matters because DSC-internal callers (other code inside the same
`QuartzCore.framework` image) get direct-branch-optimized by the linker
straight to the real implementation, **bypassing the export stub entirely**
— confirmed empirically: wrote a small BL/ADRP+ADD cross-reference scanner
(same file, extends the "who references this constant" technique already
used successfully elsewhere in this doc for kernel gates, generalized to
scan an arbitrary VA range for `BL`-to-target and `ADRP`+`ADD`-to-target
patterns) and found a real, genuine call site: **`0x1882a30b0`, inside a
local/static C++ function named `shared_server_init(void*)`
(`__ZL18shared_server_init`, mangled per the Itanium C++ ABI) at
`0x1882a308c`, calls the real `CARenderServerStart` body directly** — this
is almost certainly the actual lazy-singleton entry point (the name and
shape — a local, unexported function containing the one real call to
`CARenderServerStart` — match a classic `dispatch_once`/`pthread_once`
lazy-init pattern precisely). A second cross-reference scan (`CARenderBackdropCollect`'s
real body also gets a direct hit, call site `0x1881b8e88`, not yet
traced further) confirms the scanning technique itself works correctly, not
just a one-off coincidence.

**Re-armed live with the corrected addresses** (`shared_server_init`
`0x1882a308c`, `CARenderServerStart`'s real body `0x1881fba20`,
`CARenderServerSetRootQueue` `0x188207180` — this last one unchanged, it was
never a stub) and repeated the same respring-trigger technique (`kill -9` on
`SpringBoard`'s pid, breakpoints armed throughout a 90s wall-clock window).
**Still zero hits**, with essentially no unmatched-stop noise either (never
reached the periodic-heartbeat print threshold) — i.e. this wasn't a
dilation/noise problem this time, the breakpoints genuinely never fired
during a full SpringBoard-and-its-widgets respawn cycle.

**Honest interpretation.** `shared_server_init`'s own name and the fact it's
never called via a direct, statically-resolvable `BL` from anywhere *within*
QuartzCore's own `__TEXT` (checked, zero hits) strongly suggests it's
reached via an **indirect call** — a function pointer loaded into a register
and invoked via `BLR`, exactly the shape `dispatch_once`'s block-based API
compiles down to (the block literal's invoke-function pointer, called
through a register, not a fixed immediate target a simple `BL`-pattern
scanner can find). This is consistent with, not contradictory to, the
"real, unfired dispatch_once-gated lazy singleton" theory — it just means
finding *its* caller needs either (a) a real ARM64 disassembler that
resolves `ADRP`+`LDR` register loads through to their eventual `BLR` target
(this project still doesn't have one — the `mini_disasm.py` referenced
elsewhere in this doc was from a different, no-longer-available session
scratchpad), or (b) breakpointing `shared_server_init`'s own address
directly (already done, above) and accepting that "it never fires" is
either a real negative (this build's `CARenderServer` truly never lazily
inits during ordinary respring-triggered process churn) or a timing gap
this session's two attempts didn't happen to cover (e.g. if the real trigger
is the *very first* process in the whole boot to ever touch `CARenderServer`
system-wide, and that already happened long before either of this session's
GDB attach points, on either the warm boot or the fresh one).

**Concrete next step, more targeted than the previous list**: breakpoint
`shared_server_init` (`0x1882a308c`) specifically, armed from the *very*
first instant of a fresh QEMU boot (this session's fresh-boot window did
have this breakpoint's sibling `CARenderServerStart`'s *stub* armed from
t=0, not the corrected real address — worth exactly repeating that specific
condition, stub-bug now fixed, before concluding anything further from a
respring-only signal). If a truly-from-t=0 window still gets zero hits on
`shared_server_init` itself, that's a much stronger, cleaner disproof of the
"Metal backend is ever used in this build" hypothesis than anything
gathered so far — worth doing before investing in a real disassembler.

**Environment re-verified clean after this update**: `/sigkill_test` →
`Segmentation fault: 11`, `/compute_test` → `result = 42`, QMP `info status`
→ `running`, both breakpoint sets fully removed, no dangling paused state.
