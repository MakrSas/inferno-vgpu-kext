Continue the Inferno GPU/Metal project. Repo: `/home/makr/Documents/inferno-vgpu-kext` (GitHub `MakrSas/inferno-vgpu-kext`). QEMU working dir: `/home/makr/Documents/Inferno/InfernoData`.

**Read `/home/makr/Documents/inferno-vgpu-kext/PROJECT_STATUS.md` in full first.** It is the single source of truth — environment setup, playbooks, and every finding from today's work (5 SIGKILL/sandbox security gates found and permanently patched, real on-screen Metal triangle confirmed via screendump, backboardd confirmed to never call Metal, app-level CAMetalLayer/IOSurface hand-off mechanism identified, MapKit `/b` sandbox-deny investigation in progress with 6 precomputed candidate patches ready to apply).

**Live state when this was written**: a background agent (not resumable from a new session — it was tied to the previous conversation) was mid-task trying to reliably trigger MapKit's Metal snapshot renderer via QMP tap automation, to catch and patch the sandbox deny blocking its `dlopen("/b")`. Check `git log` first — if a new commit landed after `2a3dbca` documenting a resolution, that task may already be done; if not, it's still open and the 6 precomputed patches (VAs/bytes) are in PROJECT_STATUS.md's "MapKit `/b` sandbox-deny investigation" section, ready to use.

**Open threads, roughly in priority order**:
1. MapKit `/b` sandbox-deny (see above) — closest to done, patches precomputed.
2. `agx_system_metal_test` pre-main() dyld crash (task: localize/fix) — possibly connected to a Maps.app crash-on-tap observed live but with lost evidence; worth checking if MapKit work reproduces it again.
3. A one-time kernel panic in this project's own driver code during `/compute_test`, immediately followed by QEMU dying silently — reproduced once, not yet root-caused; may explain an earlier-observed "QEMU died silently" mystery.
4. Building an actual CAMetalLayer/CAContext-hosted test app so a real app's Metal rendering gets composited into the interface by backboardd's existing (untouched) logic — the concrete path toward "whole interface + apps via Metal" that the user wants, per the app-level investigation findings.

**Standing instruction from the user**: keep working continuously without stopping to ask permission at each step; use parallel background subagents liberally for long-running kernel-debugging investigations (this has been very effective today — each agent must read PROJECT_STATUS.md first and update it before finishing, committing and pushing incrementally, not just at the end, since session limits can cut off work mid-task).

Goal: real Metal rendering for both individual apps and the whole system interface, at 60fps, on this QEMU-emulated iOS 14 kernelcache.
