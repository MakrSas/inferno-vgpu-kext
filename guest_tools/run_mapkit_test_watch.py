#!/usr/bin/env python3
"""Arm breakpoints BEFORE triggering, then run /tmp/mapkit_test (the
MKMapSnapshotter direct-trigger test binary, see PROJECT_STATUS.md's
MapKit /b sandbox-deny investigation and mapkit_snapshotter_test.m) over a
SEPARATE serial connection, then poll for a bounded window. Direct sibling
of tap_maps_watch.py -- same candidate set, same SMP-safety rules, same
finally/qmp_cont() discipline -- with the trigger swapped from a QMP
touchscreen tap to actually exec'ing our own deterministic test binary on
the guest, since that's a fully on-demand, no-UI-guessing-needed trigger
for the exact same MTLCreateSystemDefaultDevice()-via-MapKit code path.

Two-phase pattern, same as this project's SIGKILL investigation throughout:
breakpoints are armed over the GDB RSP debug port (127.0.0.1:1234), the
trigger runs over the completely separate guest shell serial port
(127.0.0.1:4444) -- these are different TCP channels, so opening the
second one is safe and does NOT collide with the "never open a second
serial connection while a transfer is in flight" rule (that rule is about
two *concurrent connections to the same port 4444*, which would corrupt an
in-flight chunked file transfer; this script only opens 4444 once, well
after any transfer has already completed, and only long enough to fire a
backgrounded `nohup ... &` command before returning).

The triggered test binary itself is backgrounded on the guest side
(`nohup /tmp/mapkit_test > /tmp/mapkit_test_stdout.log 2>&1 &`) so the
trigger connection returns almost immediately, freeing it up; the real
watching happens via the GDB breakpoints over the whole WALL_CLOCK_DEADLINE_S
window, independent of how long the backgrounded test process itself takes
(it has its own internal 120s bound, see mapkit_snapshotter_test.m).

Per this project's established SMP-safety rule: NEVER touch (z0/s/Z0) a PC
that isn't one of our own explicitly-armed addresses; just `c` again.
Always qmp_cont() in a finally block.
"""
import json
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gdb_rsp2 import RSP, qmp_cont  # noqa: E402

STALE_HOT_BP = 0xfffffff008125a10  # _arm64_retention_wfi

SANDBOX_CANDIDATES = {
    "open_op15_file_read_data":            (0xfffffff0092a2528, "hook_vnode_check_open", 0x15),
    "open_op1f_maybe_write":               (0xfffffff0092a25b4, "hook_vnode_check_open", 0x1f),
    "getattr_op16_file_read_metadata":     (0xfffffff0092a3f00, "hook_vnode_check_getattr", 0x16),
    "stat_op16_file_read_metadata":        (0xfffffff0092a1c84, "hook_vnode_check_stat", 0x16),
    "readlink_op16_file_read_metadata":    (0xfffffff0092a241c, "hook_vnode_check_readlink", 0x16),
    "getattrlist_op16_file_read_metadata": (0xfffffff0092a2d38, "hook_vnode_check_getattrlist", 0x16),
    "access_op15_file_read_data":          (0xfffffff0092a3640, "hook_vnode_check_access", 0x15),
    "access_op1f_maybe_write":             (0xfffffff0092a36c4, "hook_vnode_check_access", 0x1f),
    "access_op67_maybe_exec":              (0xfffffff0092a3764, "hook_vnode_check_access", 0x67),
    "lookup_preflight_op1a":               (0xfffffff0092a0c74, "hook_vnode_check_lookup_preflight", 0x1a),
}

BLOCK_INVOKE_CHAIN = {
    "MTLCreateSystemDefaultDevice_entry": 0x1970505d0,
    "block_invoke_entry":                 0x1970506e4,
    "our_patch_body":                     0x1970506fc,
    "dlopen_stub":                        0x1970a5cc0,
    "dlsym_stub":                         0x1970a5cd0,
    "block_epilogue":                     0x197050750,
}

BP_ABORT = 0xfffffff007b574f0    # _handle_user_abort
BP_TRIAGE = 0xfffffff007a2c850   # _exception_triage
STATE_PC_OFF = 0x108
STATE_CPSR_OFF = 0x110
STATE_FAR_OFF = 0x118
STATE_ESR_OFF = 0x120

GUEST_HOST, GUEST_PORT = "127.0.0.1", 4444
RUN_LABEL = sys.argv[2] if len(sys.argv) > 2 else "mapkit_snapshotter"

WALL_CLOCK_DEADLINE_S = float(sys.argv[1]) if len(sys.argv) > 1 else 240.0
ROUND_TIMEOUT = 6


def read_regs(r):
    regs = r.cmd("g")
    if not regs or regs.startswith("E"):
        return None
    raw = bytes.fromhex(regs)
    names = [f"x{i}" for i in range(31)] + ["sp", "pc"]
    off = 0
    values = {}
    for name in names:
        if off + 8 > len(raw):
            break
        values[name] = struct.unpack_from("<Q", raw, off)[0]
        off += 8
    if off + 4 <= len(raw):
        values["cpsr"] = struct.unpack_from("<I", raw, off)[0]
    return values


def read_mem(r, addr, length):
    resp = r.cmd(f"m{addr:x},{length:x}", timeout=10)
    if not resp or resp.startswith("E"):
        return None
    try:
        return bytes.fromhex(resp)
    except ValueError:
        return None


def do_run_test(delay_s):
    time.sleep(delay_s)
    # NOTE: runs /mapkit_test (root), NOT /tmp/mapkit_test -- a live run
    # this session found a genuine, previously-undocumented Sandbox.kext
    # "System Policy: <proc> deny(1) process-exec* /private/var/tmp/..."
    # denial specific to executing from /tmp (distinct from all 5 already-
    # patched SIGKILL gates, and distinct from the target sandbox-deny this
    # whole investigation is chasing). Fixed the same way this project
    # already fixed the analogous /tmp-mmap-blocked issue for the
    # bash-builtin dylib route: cp to / first. Run in the foreground
    # (not backgrounded via nohup) since the crash below reproduces almost
    # instantly and synchronously reading its output is simpler/more direct.
    print("[trigger] connecting to guest shell (127.0.0.1:4444) to run /mapkit_test...", flush=True)
    try:
        sock = socket.create_connection((GUEST_HOST, GUEST_PORT), timeout=10)
        sock.settimeout(0.3)
        cmd = ("rm -f /tmp/mapkit_test.log /tmp/mapkit_test_stdout.log; "
               "/mapkit_test > /tmp/mapkit_test_stdout.log 2>&1; "
               "echo TRIGGER_EXIT_$?\n")
        sock.sendall(cmd.encode())
        buf = b""
        deadline = time.time() + 25
        idle_deadline = time.time() + 3.0
        while time.time() < deadline and time.time() < idle_deadline:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                idle_deadline = time.time() + 2.0
            except socket.timeout:
                continue
        sock.close()
        print(f"[trigger] run command sent, guest replied: {buf.decode(errors='replace')!r}", flush=True)
    except Exception as e:
        print(f"[trigger] RUN FAILED: {e}", flush=True)


def main():
    r = RSP()
    print(f"[{time.strftime('%X')}] connected, draining stray...", flush=True)
    stray = r.drain_stray(window=2.0)
    print(f"drained {len(stray)} stray packets", flush=True)
    print("status:", r.cmd("?"), flush=True)

    print(f"proactively clearing known-hot stale bp @ {STALE_HOT_BP:#x}...", flush=True)
    print(" z0:", r.cmd(f"z0,{STALE_HOT_BP:x},4"), flush=True)

    addrs = {}
    for name, (addr, fn, op) in SANDBOX_CANDIDATES.items():
        res = r.cmd(f"Z0,{addr:x},4")
        addrs[name] = addr
        print(f"[sandbox] bp {name} ({fn}, op={op:#x}) @ {addr:#x}: {res}", flush=True)
    for name, addr in BLOCK_INVOKE_CHAIN.items():
        res = r.cmd(f"Z0,{addr:x},4")
        addrs[name] = addr
        print(f"[chain] bp {name} @ {addr:#x}: {res}", flush=True)
    print(f"bp handle_user_abort {BP_ABORT:#x}:", r.cmd(f"Z0,{BP_ABORT:x},4"), flush=True)
    print(f"bp exception_triage {BP_TRIAGE:#x}:", r.cmd(f"Z0,{BP_TRIAGE:x},4"), flush=True)

    print(f"[{time.strftime('%X')}] ALL BREAKPOINTS ARMED. Triggering /tmp/mapkit_test in 3s...", flush=True)

    th = threading.Thread(target=do_run_test, args=(3.0,), daemon=True)
    th.start()

    t0 = time.time()
    round_i = 0
    sandbox_deny_log = []
    sandbox_allow_counts = {n: 0 for n in SANDBOX_CANDIDATES}
    chain_hits = []
    abort_hits = []
    triage_hits = []
    unmatched = 0

    try:
        while time.time() - t0 < WALL_CLOCK_DEADLINE_S:
            round_i += 1
            stop = r.cmd("c", timeout=ROUND_TIMEOUT)
            if not stop or not (stop.startswith("T") or stop.startswith("S")):
                if round_i % 20 == 0:
                    print(f"  [heartbeat] round={round_i} elapsed={time.time()-t0:.0f}s "
                          f"sandbox_denies={len(sandbox_deny_log)} chain_hits={len(chain_hits)} "
                          f"abort_hits={len(abort_hits)}", flush=True)
                continue

            regs = read_regs(r)
            if regs is None:
                continue
            pc = regs.get("pc")
            if pc is None:
                continue

            matched_sandbox = None
            for name, addr in [(n, a) for n, (a, _, _) in SANDBOX_CANDIDATES.items()]:
                if addr == pc:
                    matched_sandbox = name
                    break
            matched_chain = None
            for name, addr in BLOCK_INVOKE_CHAIN.items():
                if addr == pc:
                    matched_chain = name
                    break

            if matched_sandbox:
                x0 = regs.get("x0", 0)
                is_deny = (x0 & 0xFFFFFFFF) != 0
                addr = addrs[matched_sandbox]
                if not is_deny:
                    sandbox_allow_counts[matched_sandbox] += 1
                else:
                    entry = {
                        "t": time.time() - t0, "matched": matched_sandbox, "pc": pc, "x0": x0,
                        "regs": {k: regs[k] for k in ("x0", "x1", "x2", "x3", "x4",
                                                       "x19", "x20", "x21", "x22") if k in regs},
                    }
                    sandbox_deny_log.append(entry)
                    print(f"\n*** SANDBOX DENY HIT #{len(sandbox_deny_log)} t={entry['t']:.1f}s "
                          f"matched={matched_sandbox} pc={pc:#018x} x0={x0:#010x} ***", flush=True)
                    for k, v in entry["regs"].items():
                        print(f"    {k} = {v:#018x}", flush=True)
                r.cmd(f"z0,{addr:x},4")
                r.cmd("s", timeout=10)
                r.cmd(f"Z0,{addr:x},4")
                continue

            if matched_chain:
                addr = addrs[matched_chain]
                entry = {"t": time.time() - t0, "matched": matched_chain, "pc": pc,
                          "regs": {f"x{i}": regs[f"x{i}"] for i in range(9) if f"x{i}" in regs}}
                chain_hits.append(entry)
                print(f"\n*** BLOCK_INVOKE CHAIN HIT #{len(chain_hits)} t={entry['t']:.1f}s "
                      f"matched={matched_chain} pc={pc:#018x} ***", flush=True)
                for k, v in entry["regs"].items():
                    print(f"    {k} = {v:#018x}", flush=True)
                r.cmd(f"z0,{addr:x},4")
                r.cmd("s", timeout=10)
                r.cmd(f"Z0,{addr:x},4")
                continue

            if BP_ABORT <= pc < BP_ABORT + 0x10:
                x0, x1, x2, x3, x4 = regs["x0"], regs["x1"], regs["x2"], regs["x3"], regs["x4"]
                mem = read_mem(r, x0 + STATE_PC_OFF, 0x20)
                state_pc = state_cpsr = state_far = state_esr = None
                if mem and len(mem) >= 0x20:
                    state_pc = struct.unpack_from("<Q", mem, 0)[0]
                    state_cpsr = struct.unpack_from("<I", mem, 8)[0]
                    state_far = struct.unpack_from("<Q", mem, 0x10)[0]
                    state_esr = struct.unpack_from("<I", mem, 0x18)[0]
                entry = {
                    "t": time.time() - t0, "state": x0, "esr_arg": x1 & 0xFFFFFFFF,
                    "fault_addr": x2, "fault_code": x3, "fault_type": x4,
                    "state_pc": state_pc, "state_cpsr": state_cpsr,
                    "state_far": state_far, "state_esr": state_esr,
                }
                abort_hits.append(entry)
                spc = f"{state_pc:#018x}" if state_pc is not None else "None"
                print(f"\n*** ABORT HIT #{len(abort_hits)} t={entry['t']:.1f}s state={x0:#018x} "
                      f"pc={spc} far={x2:#018x} esr={x1&0xFFFFFFFF:#010x} "
                      f"code={x3:#x} type={x4:#x} ***", flush=True)
                r.cmd(f"z0,{BP_ABORT:x},4")
                r.cmd("s", timeout=10)
                r.cmd(f"Z0,{BP_ABORT:x},4")
                continue

            if BP_TRIAGE <= pc < BP_TRIAGE + 0x10:
                x0, x1, x2 = regs["x0"], regs["x1"], regs["x2"]
                mem = read_mem(r, x1, 16)
                code0 = code1 = None
                if mem and len(mem) >= 16:
                    code0, code1 = struct.unpack_from("<qq", mem, 0)
                entry = {"t": time.time() - t0, "exception_type": x0, "code_ptr": x1,
                          "codeCnt": x2, "code0": code0, "code1": code1}
                triage_hits.append(entry)
                print(f"\n*** TRIAGE HIT #{len(triage_hits)} t={entry['t']:.1f}s "
                      f"exception_type={x0} codeCnt={x2} code0={code0} code1={code1} ***", flush=True)
                r.cmd(f"z0,{BP_TRIAGE:x},4")
                r.cmd("s", timeout=10)
                r.cmd(f"Z0,{BP_TRIAGE:x},4")
                continue

            unmatched += 1
            if unmatched % 50 == 1:
                print(f"  (unmatched stop #{unmatched}, pc={pc:#018x} -- leaving untouched)", flush=True)
            continue

        print(f"\n[{time.strftime('%X')}] window complete, rounds={round_i} elapsed={time.time()-t0:.0f}s", flush=True)
        print(f"sandbox_allow_counts={sandbox_allow_counts}", flush=True)
        print(f"sandbox_deny_log ({len(sandbox_deny_log)}): {sandbox_deny_log}", flush=True)
        print(f"chain_hits ({len(chain_hits)}): {chain_hits}", flush=True)
        print(f"abort_hits ({len(abort_hits)}): {abort_hits}", flush=True)
        print(f"triage_hits ({len(triage_hits)}): {triage_hits}", flush=True)
        print(f"unmatched={unmatched}", flush=True)

        summary = {
            "sandbox_allow_counts": sandbox_allow_counts,
            "sandbox_deny_log": sandbox_deny_log,
            "chain_hits": chain_hits,
            "abort_hits": abort_hits,
            "triage_hits": triage_hits,
            "unmatched": unmatched,
            "rounds": round_i,
        }
        summary_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"tap_watch_summary_{RUN_LABEL}.json")
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=1, default=str)

    finally:
        print("removing all breakpoints...", flush=True)
        for name, addr in addrs.items():
            r.cmd(f"z0,{addr:x},4")
        r.cmd(f"z0,{BP_ABORT:x},4")
        r.cmd(f"z0,{BP_TRIAGE:x},4")
        try:
            r.sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    finally:
        qmp_cont()
