#!/usr/bin/env python3
"""Arm breakpoints BEFORE triggering, then tap the Maps home-screen icon via
QMP input-send-event, then poll for a short, bounded window. This sidesteps
the previously-diagnosed GDB-breakpoint/guest-time-dilation problem (a 2400s
real-time passive window only advanced guest uptime ~200s) entirely, because
we're not waiting an unbounded amount of guest-time for MapKit's own
snapshot-refresh timer -- we're triggering the event synchronously right
after arming.

Watches THREE independent hypotheses at once, since we don't know in advance
whether tapping the full Maps.app icon will (a) hit the same sandbox
file-read-data/file-read-metadata deny on /b that the widget's
MapKit.SnapshotService.xpc already hit, (b) reach further into the real
MTLCreateSystemDefaultDevice()/block_invoke chain than the XPC service did,
or (c) crash before any of that, the same class of pre-main()-ish crash
already documented for agx_system_metal_test:

  1. The 10 candidate sandbox vnode-check return points (same addresses as
     mapkit_sandbox/verify_gate_mapkit2.py -- reused unmodified).
  2. The 6-address MTLCreateSystemDefaultDevice/block_invoke chain (same
     addresses as the agx_system_metal_test crash investigation).
  3. _handle_user_abort + _exception_triage (same addresses/offsets as
     macho_diff/catch_fault3.py's crash-catching technique).

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
from qmp_raw import QMP  # noqa: E402

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

MAPS_ICON_X = int(sys.argv[2]) if len(sys.argv) > 2 else 320
MAPS_ICON_Y = int(sys.argv[3]) if len(sys.argv) > 3 else 434
TAP_LABEL = sys.argv[4] if len(sys.argv) > 4 else "Maps"

WALL_CLOCK_DEADLINE_S = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
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


def do_tap(delay_s):
    time.sleep(delay_s)
    print(f"[trigger] tapping {TAP_LABEL} icon at ({MAPS_ICON_X},{MAPS_ICON_Y}) via QMP", flush=True)
    try:
        q = QMP()
        q.tap(MAPS_ICON_X, MAPS_ICON_Y, hold_s=0.15)
        q.close()
        print("[trigger] tap sent OK", flush=True)
    except Exception as e:
        print(f"[trigger] TAP FAILED: {e}", flush=True)


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

    print(f"[{time.strftime('%X')}] ALL BREAKPOINTS ARMED. Triggering tap in 3s...", flush=True)

    th = threading.Thread(target=do_tap, args=(3.0,), daemon=True)
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
        summary_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"tap_watch_summary_{TAP_LABEL}.json")
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
