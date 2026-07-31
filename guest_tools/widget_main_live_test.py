#!/usr/bin/env python3
"""One-shot live-test driver for the inferno_widget_host_main.m live test
(see PROJECT_STATUS.md's "Widget-hosted Metal compositing: live test
attempt" section). Assumes /inferno_widget_host_main has ALREADY been
transferred to the guest (via transfer_binary3.py, over a separate,
already-closed serial connection -- this script opens its own single,
persistent connection and does everything else in one session to minimize
serial round-trips):

1. dd the new binary (no conv=notrunc, so the file is correctly truncated
   to the new, smaller size -- see PROJECT_STATUS.md's gate-#6 finding for
   why this is known to work) onto the real StocksWidget path, verify size.
2. Snapshot dmesg's current tail (so the post-trigger diff is unambiguous).
3. Kill SpringBoard (pid looked up fresh, not hardcoded) to force a
   respring -- the "stronger lever" this doc's MapKit investigation already
   established for forcing a genuinely fresh Today-View/widget-list reload.
4. Poll `ps auxww | grep -i widget` for up to POLL_DEADLINE_S looking for
   StocksWidget specifically.
5. Dump dmesg again and the trace log, regardless of whether step 4 found
   the process (a "never appeared" result is itself real information).

Usage: widget_main_live_test.py <bundle_stocks_widget_path>
"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444
POLL_DEADLINE_S = 90
POLL_INTERVAL_S = 5


def guest_cmd(sock, cmd, idle=1.5, deadline_total=20.0):
    sock.sendall((cmd + "\n").encode())
    buf = b""
    deadline = time.time() + deadline_total
    idle_deadline = time.time() + idle
    while time.time() < deadline and time.time() < idle_deadline:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            idle_deadline = time.time() + idle
        except socket.timeout:
            continue
    return buf.decode(errors="replace")


def main():
    if len(sys.argv) != 2:
        print("usage: widget_main_live_test.py <StocksWidget path in bundle>", file=sys.stderr)
        sys.exit(1)
    target = sys.argv[1]

    sock = socket.create_connection((HOST, PORT), timeout=10)
    sock.settimeout(0.3)

    print("=== step 1: swap binary in place (dd, no conv=notrunc) ===")
    out = guest_cmd(sock, f"dd if=/inferno_widget_host_main of={target}; echo DD_RC=$?", idle=3.0, deadline_total=30.0)
    print(out)
    out = guest_cmd(sock, f"ls -la {target}; wc -c < {target}", idle=2.0)
    print(out)

    print("=== step 2: dmesg tail BEFORE trigger (for later diffing) ===")
    out = guest_cmd(sock, "dmesg | tail -n 40", idle=2.0, deadline_total=15.0)
    print(out)
    before_dmesg = out

    print("=== step 3: find + kill SpringBoard to force a respring ===")
    out = guest_cmd(sock, "ps auxww | grep '[S]pringBoard'", idle=2.0)
    print(out)
    pid = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 1 and "SpringBoard" in line:
            try:
                pid = int(parts[1])
                break
            except ValueError:
                continue
    if pid is None:
        print("!!! could not find SpringBoard pid, aborting trigger step")
    else:
        print(f"killing SpringBoard pid {pid}")
        out = guest_cmd(sock, f"kill -9 {pid}; echo KILL_RC=$?", idle=2.0)
        print(out)

    print(f"=== step 4: poll for StocksWidget process, up to {POLL_DEADLINE_S}s ===")
    deadline = time.time() + POLL_DEADLINE_S
    found = False
    while time.time() < deadline:
        out = guest_cmd(sock, "ps auxww | grep '[S]tocksWidget'", idle=2.0, deadline_total=15.0)
        print(f"[{POLL_DEADLINE_S - int(deadline - time.time())}s] {out.strip()!r}")
        if "StocksWidget" in out:
            found = True
            print(">>> StocksWidget process FOUND <<<")
            break
        time.sleep(POLL_INTERVAL_S)
    print(f"process observed: {found}")

    print("=== step 5: dmesg tail AFTER trigger ===")
    out = guest_cmd(sock, "dmesg | tail -n 80", idle=2.0, deadline_total=15.0)
    print(out)

    print("=== step 6: trace log ===")
    out = guest_cmd(sock, "cat /tmp/widget_host_main_trace.log 2>&1 || echo NO_TRACE_LOG", idle=2.0, deadline_total=15.0)
    print(out)

    print("=== step 7: full ps auxww | grep -i stocks (any residual Stocks-related process) ===")
    out = guest_cmd(sock, "ps auxww | grep -i stocks", idle=2.0, deadline_total=15.0)
    print(out)

    sock.close()


if __name__ == "__main__":
    main()
