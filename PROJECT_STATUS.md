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

## `agx_system_metal_test` crash investigation (2026-07-30)

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
3. Transfer the resulting binary to the guest (any path, `/tmp` is fine
   for a plain executable that only needs to survive until you run it;
   the builtin route additionally needs `/` specifically, not `/tmp` — see
   the historical workaround section above for why).
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
