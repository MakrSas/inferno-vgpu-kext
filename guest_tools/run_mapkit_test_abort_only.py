#!/usr/bin/env python3
"""Minimal variant of run_mapkit_test_watch.py: arms ONLY
handle_user_abort + exception_triage (drops the 10 sandbox candidates + 6
block_invoke-chain addresses), specifically to minimize GDB-breakpoint-
induced guest-time dilation (see PROJECT_STATUS.md's documented ~12x-to-
much-worse dilation finding when many software breakpoints are active) for
a tight, clean correlation between the trigger and the resulting fault PC.

Rationale for dropping the other 16: a first full-18-breakpoint run already
established (a) /tmp/mapkit_test.log is never created (main() never
reached) and (b) the sandbox/chain breakpoints stayed at zero hits/zero
denies across two full windows -- fully expected once main() is known to
be unreachable. This run exists purely to try to pin the exact dyld-bootstrap
fault PC, the same diagnostic already done for agx_system_metal_test.
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

BP_ABORT = 0xfffffff007b574f0    # _handle_user_abort
BP_TRIAGE = 0xfffffff007a2c850   # _exception_triage
STATE_PC_OFF = 0x108
STATE_CPSR_OFF = 0x110
STATE_FAR_OFF = 0x118
STATE_ESR_OFF = 0x120

GUEST_HOST, GUEST_PORT = "127.0.0.1", 4444
WALL_CLOCK_DEADLINE_S = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
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
    print("[trigger] connecting to guest shell (127.0.0.1:4444) to run /mapkit_test...", flush=True)
    try:
        sock = socket.create_connection((GUEST_HOST, GUEST_PORT), timeout=10)
        sock.settimeout(0.3)
        cmd = ("rm -f /tmp/mapkit_test.log /tmp/mapkit_test_stdout.log; "
               "/mapkit_test > /tmp/mapkit_test_stdout.log 2>&1; "
               "echo TRIGGER_EXIT_$?\n")
        sock.sendall(cmd.encode())
        buf = b""
        deadline = time.time() + 40
        idle_deadline = time.time() + 5.0
        while time.time() < deadline and time.time() < idle_deadline:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                idle_deadline = time.time() + 3.0
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

    print(f"bp handle_user_abort {BP_ABORT:#x}:", r.cmd(f"Z0,{BP_ABORT:x},4"), flush=True)
    print(f"bp exception_triage {BP_TRIAGE:#x}:", r.cmd(f"Z0,{BP_TRIAGE:x},4"), flush=True)

    print(f"[{time.strftime('%X')}] BREAKPOINTS ARMED (abort+triage only). Triggering /mapkit_test in 2s...", flush=True)

    th = threading.Thread(target=do_run_test, args=(2.0,), daemon=True)
    th.start()

    t0 = time.time()
    round_i = 0
    abort_hits = []
    triage_hits = []
    unmatched = 0
    seen_states = {}

    try:
        while time.time() - t0 < WALL_CLOCK_DEADLINE_S:
            round_i += 1
            stop = r.cmd("c", timeout=ROUND_TIMEOUT)
            if not stop or not (stop.startswith("T") or stop.startswith("S")):
                if round_i % 20 == 0:
                    print(f"  [heartbeat] round={round_i} elapsed={time.time()-t0:.0f}s "
                          f"abort_hits={len(abort_hits)}", flush=True)
                continue

            regs = read_regs(r)
            if regs is None:
                continue
            pc = regs.get("pc")
            if pc is None:
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
                is_new_state = x0 not in seen_states
                seen_states[x0] = seen_states.get(x0, 0) + 1
                entry = {
                    "t": time.time() - t0, "state": x0, "esr_arg": x1 & 0xFFFFFFFF,
                    "fault_addr": x2, "fault_code": x3, "fault_type": x4,
                    "state_pc": state_pc, "state_cpsr": state_cpsr,
                    "state_far": state_far, "state_esr": state_esr,
                    "state_first_seen": is_new_state,
                }
                abort_hits.append(entry)
                spc = f"{state_pc:#018x}" if state_pc is not None else "None"
                print(f"\n*** ABORT HIT #{len(abort_hits)} t={entry['t']:.1f}s state={x0:#018x} "
                      f"(seen_count={seen_states[x0]}, first_seen={is_new_state}) "
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
        print(f"abort_hits ({len(abort_hits)}): {abort_hits}", flush=True)
        print(f"triage_hits ({len(triage_hits)}): {triage_hits}", flush=True)
        print(f"unique states seen: {len(seen_states)}, counts={seen_states}", flush=True)
        print(f"unmatched={unmatched}", flush=True)

    finally:
        print("removing breakpoints...", flush=True)
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
