#!/usr/bin/env python3
"""Send one command to the guest's serial console (127.0.0.1:4444) and print
whatever comes back within the idle window. Recreated this session -- the
original from earlier in this project lived in a now-gone scratchpad dir.
Mirrors the guest_cmd() helper already proven in patch_block_invoke.py.

Usage: shell_cmd.py 'command string' [idle_seconds] [total_deadline_seconds]
"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444


def guest_cmd(sock, cmd, idle=2.0, deadline_total=30.0):
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
    if len(sys.argv) < 2:
        print("usage: shell_cmd.py 'command' [idle_seconds] [total_deadline]", file=sys.stderr)
        sys.exit(1)
    cmd = sys.argv[1]
    idle = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    deadline = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0

    sock = socket.create_connection((HOST, PORT), timeout=10)
    sock.settimeout(0.3)
    out = guest_cmd(sock, cmd, idle=idle, deadline_total=deadline)
    sock.close()
    print(out)


if __name__ == "__main__":
    main()
