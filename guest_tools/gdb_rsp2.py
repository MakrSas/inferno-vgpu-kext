#!/usr/bin/env python3
"""Robust-ish minimal GDB Remote Serial Protocol client for QEMU's
system-mode gdbstub. Rewrite of gdb_rsp.py: properly drains stray/queued
packets before each logical operation (QEMU's gdbstub appears to preserve
debug-session state across TCP reconnects, so a fresh connection can have
an old stop-notification already queued), and ALWAYS resumes the target via
QMP `cont` at the end (in a finally block) regardless of what happened on
the GDB side -- sidesteps any ambiguity about whether the target ended up
paused, which bit us hard last attempt (had to QMP-rescue a frozen VM).

Sets breakpoints at MULTIPLE candidate kill-path kernel functions at once
(cs_invalid_page, psignal_sigkill_with_reason, memorystatus_kill_proc,
proc_exit, exit_with_reason) so whichever one actually fires gets caught in
a single pass.
"""
import json
import socket
import struct
import subprocess
import sys
import time

GDB_HOST, GDB_PORT = "127.0.0.1", 1234
QMP_SOCK = "/home/makr/Documents/Inferno/InfernoData/shell-qmp.sock"

CANDIDATES = {
    "cs_invalid_page": 0xfffffff007e3f7a0,
    "psignal_sigkill_with_reason": 0xfffffff007e840cc,
    "memorystatus_kill_proc": 0xfffffff007e8ec40,
    "proc_exit": 0xfffffff007e6761c,
    "exit_with_reason": 0xfffffff007e658e4,
}


def qmp_cont():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(QMP_SOCK)
        s.recv(65536)  # greeting
        s.sendall((json.dumps({"execute": "qmp_capabilities"}) + "\n").encode())
        s.recv(65536)
        s.sendall((json.dumps({"execute": "cont"}) + "\n").encode())
        time.sleep(0.3)
        reply = s.recv(65536)
        s.close()
        print(f"[safety] QMP cont issued, reply={reply!r}", flush=True)
    except Exception as e:
        print(f"[safety] QMP cont FAILED: {e}", flush=True)


class RSP:
    def __init__(self, host=GDB_HOST, port=GDB_PORT):
        self.sock = socket.create_connection((host, port), timeout=30)
        self.buf = b""

    def _fill(self, timeout):
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return False
        if not chunk:
            return False
        self.buf += chunk
        return True

    def drain_stray(self, window=1.0):
        """Consume and report anything sitting in the pipe before we've
        sent anything of our own -- leftover packets from a prior session,
        ACKing each so the server doesn't get stuck waiting."""
        deadline = time.time() + window
        found = []
        while time.time() < deadline:
            if not self._fill(0.2):
                continue
            while True:
                if self.buf[:1] == b"+" or self.buf[:1] == b"-":
                    self.buf = self.buf[1:]
                    continue
                if b"$" in self.buf and b"#" in self.buf.split(b"$", 1)[1]:
                    idx = self.buf.index(b"$")
                    end = self.buf.index(b"#", idx)
                    if end + 2 < len(self.buf):
                        pkt = self.buf[idx + 1:end]
                        self.buf = self.buf[end + 3:]
                        found.append(pkt.decode(errors="replace"))
                        self.sock.sendall(b"+")
                        continue
                break
        return found

    def _read_one_packet(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if b"$" in self.buf:
                idx = self.buf.index(b"$")
                if b"#" in self.buf[idx:]:
                    end = self.buf.index(b"#", idx)
                    if end + 2 <= len(self.buf) - 1 or end + 2 == len(self.buf):
                        if len(self.buf) >= end + 3:
                            pkt = self.buf[idx + 1:end]
                            self.buf = self.buf[end + 3:]
                            self.sock.sendall(b"+")
                            return pkt.decode(errors="replace")
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._fill(min(remaining, 1.0))
        return None

    def cmd(self, packet, timeout=30):
        data = packet.encode()
        checksum = sum(data) & 0xFF
        pkt = b"$" + data + b"#" + f"{checksum:02x}".encode()

        for attempt in range(3):
            self.sock.sendall(pkt)
            # Wait for a '+' ack, tolerating stray '$...#XX' packets that
            # might arrive interleaved (ack them and keep waiting for ours).
            ack_deadline = time.time() + 5
            got_ack = False
            while time.time() < ack_deadline:
                if not self.buf:
                    if not self._fill(min(ack_deadline - time.time(), 1.0)):
                        continue
                if self.buf[:1] == b"+":
                    self.buf = self.buf[1:]
                    got_ack = True
                    break
                if self.buf[:1] == b"-":
                    self.buf = self.buf[1:]
                    break  # retransmit
                if self.buf[:1] == b"$":
                    # Unsolicited packet arrived before our ack -- consume
                    # and ack it, then keep waiting for our own ack.
                    if b"#" in self.buf and len(self.buf) >= self.buf.index(b"#") + 3:
                        end = self.buf.index(b"#")
                        stray = self.buf[1:end]
                        self.buf = self.buf[end + 3:]
                        self.sock.sendall(b"+")
                        print(f"  (consumed stray packet while waiting for ack: {stray.decode(errors='replace')!r})", flush=True)
                        continue
                    else:
                        self._fill(1.0)
                        continue
                # Unknown byte, drop it
                self.buf = self.buf[1:]
            if got_ack:
                return self._read_one_packet(timeout)
        return None


def main():
    r = RSP()
    print("draining stray packets on connect...", flush=True)
    stray = r.drain_stray(window=1.5)
    print(f"drained: {stray}", flush=True)

    print("? (status):", r.cmd("?"), flush=True)

    addrs = {}
    for name, addr in CANDIDATES.items():
        result = r.cmd(f"Z0,{addr:x},4")
        addrs[name] = addr
        print(f"breakpoint {name}@{addr:#x}: {result}", flush=True)

    print("READY_FOR_TRIGGER", flush=True)

    hit_name = None
    for round_i in range(60):  # loop past benign/idle stops
        print(f"continuing (round {round_i}, up to 20s)...", flush=True)
        stop = r.cmd("c", timeout=20)
        print("stop reply:", stop, flush=True)
        if not stop:
            print("no reply within round timeout, trying again...", flush=True)
            continue
        if not (stop.startswith("T") or stop.startswith("S")):
            continue

        regs = r.cmd("g")
        if not regs or regs.startswith("E"):
            continue
        raw = bytes.fromhex(regs)
        names = [f"x{i}" for i in range(31)] + ["sp", "pc"]
        off = 0
        values = {}
        for name in names:
            values[name] = struct.unpack_from("<Q", raw, off)[0]
            off += 8
        if off + 4 <= len(raw):
            values["cpsr"] = struct.unpack_from("<I", raw, off)[0]
        pc = values.get("pc")
        matched = None
        if pc is not None:
            for name, addr in addrs.items():
                if addr <= pc < addr + 0x200:
                    matched = name
                    break
        print(f"  pc={pc:#018x} matched={matched}", flush=True)
        if matched:
            hit_name = matched
            for name in names + (["cpsr"] if "cpsr" in values else []):
                print(f"  {name:5s} = {values[name]:#018x}", flush=True)
            break
        # Not one of our breakpoints (idle-loop/other benign stop) -- loop
        # again to keep the target moving.

    if hit_name is None:
        print("NO BREAKPOINT HIT after all rounds", flush=True)

    print("removing all breakpoints...", flush=True)
    for name, addr in addrs.items():
        print(f"  z0 {name}: {r.cmd(f'z0,{addr:x},4')}", flush=True)

    try:
        r.sock.close()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        qmp_cont()
