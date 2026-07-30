#!/usr/bin/env python3
"""Minimal QMP client: connect, handshake, run one HMP command via
human-monitor-command, print the result.
Usage: qmp_client.py '<hmp command>'
"""
import json
import socket
import sys

SOCK_PATH = "/home/makr/Documents/Inferno/InfernoData/shell-qmp.sock"


def recv_json(sock):
    buf = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        try:
            return json.loads(buf.decode())
        except json.JSONDecodeError:
            continue
    return None


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info status"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(SOCK_PATH)

    greeting = recv_json(s)
    print("greeting:", greeting)

    s.sendall((json.dumps({"execute": "qmp_capabilities"}) + "\n").encode())
    print("capabilities:", recv_json(s))

    s.sendall((json.dumps({
        "execute": "human-monitor-command",
        "arguments": {"command-line": cmd}
    }) + "\n").encode())
    print("result:", recv_json(s))
    s.close()


if __name__ == "__main__":
    main()
