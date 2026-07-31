#!/usr/bin/env python3
"""General-purpose raw QMP client -- unlike guest_tools/qmp_client.py (which
only wraps human-monitor-command / HMP), this speaks native QMP commands
directly, needed for `screendump` (with format=png) and `input-send-event`
(absolute-pointer tap/swipe automation), neither of which has an HMP
equivalent we can drive through the existing wrapper cleanly.

The guest's touchscreen is modeled by hw/arm/apple-silicon/mt-spi.c
("Apple Multitouch HID SPI"), which registers via the legacy
qemu_add_mouse_event_handler(..., absolute=1, ...) API. Per
ui/input-legacy.c, that bridges to the modern input core as a handler
accepting INPUT_EVENT_KIND_ABS (axis x/y, value 0..0x7FFF covering the full
display) and INPUT_EVENT_KIND_BTN (button "left" == touch down/up) --
NOT 'mtt' events (those target handlers registered via the newer
qemu_input_handler_register-with-ABS-mask path directly, which this device
does not use). display_width/height fed to the device at t8030.c:2555-2556
are t8030->disp_width/disp_height, the same values used for the display
pipe itself -- i.e. the abs 0..0x7FFF range maps linearly onto the actual
screendump pixel dimensions (confirmed 828x1792 via existing screendump
PNGs in this dir).
"""
import json
import socket
import time

SOCK_PATH = "/home/makr/Documents/Inferno/InfernoData/shell-qmp.sock"
ABS_MAX = 0x7FFF
SCREEN_W = 828
SCREEN_H = 1792


class QMP:
    def __init__(self, path=SOCK_PATH):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(15)
        self.sock.connect(path)
        self.buf = b""
        greeting = self._recv_one()
        self._greeting = greeting
        self.execute("qmp_capabilities")

    def _recv_one(self, timeout=15):
        self.sock.settimeout(timeout)
        while True:
            if b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                if not line.strip():
                    continue
                return json.loads(line.decode())
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("QMP socket closed")
            self.buf += chunk

    def execute(self, cmd, **args):
        payload = {"execute": cmd}
        if args:
            payload["arguments"] = args
        self.sock.sendall((json.dumps(payload) + "\n").encode())
        # skip any async 'event' messages, return the matching 'return'/'error'
        while True:
            reply = self._recv_one()
            if "event" in reply:
                continue
            return reply

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    # ---- convenience wrappers ----

    def screendump(self, path, fmt="png"):
        return self.execute("screendump", filename=path, format=fmt)

    def status(self):
        return self.execute("query-status")

    def send_events(self, events):
        return self.execute("input-send-event", events=events)

    @staticmethod
    def abs_event(axis, px, dim):
        value = max(0, min(ABS_MAX, round(px * ABS_MAX / (dim - 1))))
        return {"type": "abs", "data": {"axis": axis, "value": value}}

    @staticmethod
    def btn_event(down, button="left"):
        return {"type": "btn", "data": {"button": button, "down": down}}

    def move_to(self, x, y):
        self.send_events([
            self.abs_event("x", x, SCREEN_W),
            self.abs_event("y", y, SCREEN_H),
        ])

    def tap(self, x, y, hold_s=0.12):
        """Single tap: move to (x,y), press, brief hold, release."""
        self.move_to(x, y)
        time.sleep(0.05)
        self.send_events([self.btn_event(True)])
        time.sleep(hold_s)
        self.send_events([self.btn_event(False)])

    def swipe(self, x0, y0, x1, y1, steps=20, step_delay=0.03, settle_s=0.1):
        """Continuous-motion swipe: press at (x0,y0), move through
        intermediate points to (x1,y1), release. Modeled as a real held
        drag (btn down held throughout), not a teleport, since mt-spi.c's
        gesture gets reconstructed from a stream of intermediate positions
        (apple_mt_spi_timer_tick fires ~50ms while LBUTTON is held and
        prev_x/y != x/y) -- a single jump would only synthesize one
        path-update sample, unlikely to satisfy SpringBoard's swipe
        gesture recognizer.
        """
        self.move_to(x0, y0)
        time.sleep(0.05)
        self.send_events([self.btn_event(True)])
        time.sleep(0.05)
        for i in range(1, steps + 1):
            fx = x0 + (x1 - x0) * i / steps
            fy = y0 + (y1 - y0) * i / steps
            self.move_to(round(fx), round(fy))
            time.sleep(step_delay)
        time.sleep(settle_s)
        self.send_events([self.btn_event(False)])


def main():
    import sys
    if len(sys.argv) < 2:
        print("usage: qmp_raw.py screendump <path> | tap <x> <y> | swipe <x0> <y0> <x1> <y1> | status")
        sys.exit(1)
    q = QMP()
    cmd = sys.argv[1]
    if cmd == "screendump":
        print(q.screendump(sys.argv[2]))
    elif cmd == "tap":
        x, y = int(sys.argv[2]), int(sys.argv[3])
        print(q.tap(x, y))
    elif cmd == "swipe":
        x0, y0, x1, y1 = (int(a) for a in sys.argv[2:6])
        print(q.swipe(x0, y0, x1, y1))
    elif cmd == "status":
        print(q.status())
    else:
        print("unknown cmd", cmd)
    q.close()


if __name__ == "__main__":
    main()
