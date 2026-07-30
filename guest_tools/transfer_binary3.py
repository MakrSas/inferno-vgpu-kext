#!/usr/bin/env python3
"""Chunked binary transfer to the guest over the serial console (127.0.0.1:4444).
Recreated this session per documented project memory: CHUNK=100 bytes via
printf hex-escapes appended to the destination file is the proven-safe size;
CHUNK=150/300 caused silent corruption (serial TTY line-length limit) in
earlier sessions. Verifies the final size with wc -c and reports mismatch.

Usage: transfer_binary3.py <local_path> <remote_path>
"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444
CHUNK = 100


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
    if len(sys.argv) != 3:
        print("usage: transfer_binary3.py <local_path> <remote_path>", file=sys.stderr)
        sys.exit(1)
    local_path, remote_path = sys.argv[1], sys.argv[2]

    with open(local_path, "rb") as f:
        data = f.read()
    total = len(data)
    print(f"transferring {local_path} -> {remote_path} ({total} bytes, chunk={CHUNK})")

    sock = socket.create_connection((HOST, PORT), timeout=10)
    sock.settimeout(0.3)
    guest_cmd(sock, f"rm -f {remote_path}; echo XFER_START_$$", idle=2.0)

    start = time.time()
    for i in range(0, total, CHUNK):
        piece = data[i:i + CHUNK]
        hexescapes = "".join(f"\\x{b:02x}" for b in piece)
        cmd = f"printf '{hexescapes}' >> {remote_path}"
        out = guest_cmd(sock, cmd, idle=1.0, deadline_total=15.0)
        if (i // CHUNK) % 100 == 0:
            elapsed = time.time() - start
            print(f"  {i}/{total} bytes ({elapsed:.1f}s elapsed)")

    out = guest_cmd(sock, f"wc -c < {remote_path}; chmod 755 {remote_path}; echo RC=$?", idle=2.0)
    sock.close()
    elapsed = time.time() - start
    print(f"done in {elapsed:.1f}s, guest reports: {out.strip()!r}")

    actual = None
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            actual = int(line)
            break
    if actual == total:
        print(f"OK size match: {actual} == {total}")
    else:
        print(f"SIZE MISMATCH: expected {total}, got {actual}")
        sys.exit(1)


if __name__ == "__main__":
    main()
