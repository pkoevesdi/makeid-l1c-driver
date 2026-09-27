"""MakeID L1-C label printer over Bluetooth LE: connection, rasterising, printing."""
import ctypes
import os
import re
import socket
import struct
import subprocess
import time

H_WRITE, H_NOTIFY, H_CCCD = 0x2A, 0x2E, 0x2F   # ABF1 value, ABF2 value, ABF2 CCCD

HEAD = 96            # print head dots, 203 dpi = 12 mm
TAPE = 128           # tape width, 16 mm
MARGIN = (TAPE - HEAD) // 2
FEED = 32            # the printer adds 4 mm before and after the image
BLOCK_ROWS = 85
NAME_RE = re.compile(r"^L1[A-Z]")


class PrinterError(Exception):
    pass


def frame(cmd, payload=b""):
    b = bytes([0x66]) + struct.pack("<H", 5 + len(payload)) + bytes([cmd]) + payload
    return b + bytes([(-sum(b)) & 0xFF])


def parse_status(b):
    return dict(code=b[4] & 0x3F, busy=bool(b[4] & 0x80), battery=b[5] & 0x7F,
                charging=bool(b[5] & 0x80), remain=b[16] | b[17] << 8,
                total=b[18] | b[19] << 8, label=b[20:34].rstrip(b"\0").decode(errors="replace"))


def classify(b):
    if not b:
        return "none"
    if len(b) < 36:
        return "abort" if b[3] == 0x11 else "resend"
    c = b[4] & 0x3F
    if c not in (0, 23):
        return f"code{c}"
    if b[4] & 0x80:
        return "wait"
    if b[4] & 0x40:
        return "resend"
    return "ok"


STATUS_TEXT = {1: "no tape", 3: "tape used up", 4: "no tape", 5: "tape chip not detected",
               6: "cover open", 8: "overheated", 11: "busy", 13: "no tape", 15: "tape error",
               16: "cancelled on the printer"}


def find_printer(scan_seconds=10):
    """Return the address of an L1 printer known to or found by BlueZ."""
    def known():
        out = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3 and NAME_RE.match(parts[2]):
                return parts[1]
    addr = known()
    if not addr:
        subprocess.run(["bluetoothctl", "--timeout", str(scan_seconds), "scan", "le"], capture_output=True)
        addr = known()
    if not addr:
        raise PrinterError("no L1 printer found; is it switched on?")
    return addr


class L1C:
    def __init__(self, addr=None, timeout=15):
        addr = addr or os.environ.get("L1C_ADDR") or find_printer()
        # Raw ATT socket: BlueZ' Device1.Connect() tries BR/EDR for this printer and times out.
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        s.settimeout(timeout)
        s.bind(("00:00:00:00:00:00", 0, 4, 1))       # ATT channel, LE public address
        try:
            s.connect((addr, 0, 4, 1))
        except OSError as e:
            s.close()
            raise PrinterError(f"printer {addr} not reachable ({e})") from e
        self.addr = addr
        self.s = s
        self.buf = b""
        s.send(struct.pack("<BH", 0x02, 517))
        self.mtu = min(517, struct.unpack("<H", self._att(0x03)[1:3])[0])
        s.send(struct.pack("<BH", 0x12, H_CCCD) + b"\x01\x00")
        self._att(0x13)

    def close(self):
        self.s.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _att(self, want):
        while True:
            r = self.s.recv(1024)
            if r[0] == want:
                return r
            if r[0] == 0x01:
                raise PrinterError("ATT error " + r.hex())
            self._notif(r)

    def _notif(self, r):
        if r[0] == 0x1B and struct.unpack("<H", r[1:3])[0] == H_NOTIFY:
            self.buf += r[3:]

    def send(self, data, timeout=5):
        self.buf = b""
        chunk = self.mtu - 3
        for i in range(0, len(data), chunk):
            self.s.send(struct.pack("<BH", 0x52, H_WRITE) + data[i:i + chunk])
        end = time.time() + timeout
        while (left := end - time.time()) > 0:
            self.s.settimeout(left)
            try:
                self._notif(self.s.recv(1024))
            except socket.timeout:
                break
            b = self.buf[4:] if self.buf[:2] == b"##" and len(self.buf) > 4 else self.buf
            if b[:2] == b"**" or (len(b) >= 3 and len(b) >= (b[1] | b[2] << 8)):
                return b
        return self.buf or None

    def status(self):
        r = self.send(frame(0x10, b"\x00"))
        if not r or len(r) < 36:
            raise PrinterError("no status reply")
        return r

    def firmware(self):
        r = self.send(frame(0x50))
        return [s.decode(errors="replace") for s in r[4:-1].split(b"\0") if s] if r else []

    def check_ready(self):
        st = parse_status(self.status())
        if st["code"] not in (0, 23):
            raise PrinterError(STATUS_TEXT.get(st["code"], f"status {st['code']}"))
        return st

    def print_rows(self, rows, darkness=15, continuous=True):
        """rows: 12-byte print lines as built by head_rows(), one per dot along the tape."""
        blocks = [rows[i:i + BLOCK_ROWS] for i in range(0, len(rows), BLOCK_ROWS)]
        b4 = darkness | (0x20 if continuous else 0)
        self.send(frame(0x10, b"\x02"))
        for i, blk in enumerate(blocks):
            payload = (bytes([b4, 3])
                       + struct.pack("<HHBHHBB", 1, 1, 1, len(rows), len(blk), min(len(blocks) - 1 - i, 255), 0)
                       + lzo_compress(b"".join(blk)))
            for _ in range(5):
                rep = self.send(frame(0x1B, payload), timeout=10)
                c = classify(rep)
                if c != "resend":
                    break
            while c == "wait":
                time.sleep(0.2)
                rep = self.send(frame(0x10, b"\x00"))
                c = classify(rep)
            if c != "ok":
                code = rep[4] & 0x3F if rep and len(rep) >= 36 else None
                raise PrinterError(f"print aborted: {STATUS_TEXT.get(code, c)}")
        for _ in range(100):
            rep = self.send(frame(0x10, b"\x00"))
            if classify(rep) != "wait":
                break
            time.sleep(0.3)
        return parse_status(rep) if rep and len(rep) >= 36 else None


# --- Rasterising -------------------------------------------------------------

def head_rows(length, black):
    """Print lines for one label in design orientation (tape running left to right).

    length: image length in dots.
    black(x, y): True for a dot; x = 0..length-1 from the left, y = 0..HEAD-1 from the top
    of the 12 mm printable band. The right edge of the design goes through the head first.
    """
    out = []
    for i in range(length):
        x = length - 1 - i
        r = bytearray(HEAD // 8)
        for y in range(HEAD):
            if black(x, y):
                r[y >> 3] |= 0x80 >> (y & 7)
        for k in range(0, len(r), 2):
            r[k], r[k + 1] = r[k + 1], r[k]
        out.append(bytes(r))
    return out


def read_raster(f):
    """Pages of a PWG or CUPS raster stream: (width, height, bits per pixel, colour space, lines)."""
    sync = f.read(4)
    if sync in (b"RaSt", b"RaS2", b"RaS3"):
        e = ">"
    elif sync in (b"tSaR", b"2SaR", b"3SaR"):
        e = "<"
    else:
        raise PrinterError("document is not PWG/CUPS raster")
    compressed = sync in (b"RaS2", b"2SaR")
    while len(hdr := f.read(1796)) == 1796:
        def u(off):
            return struct.unpack_from(e + "I", hdr, off)[0]
        width, height, bpp, bpl, cspace = u(372), u(376), u(388), u(392), u(400)
        blank = 0xFF if cspace in (18, 1, 19) else 0x00          # sgray, rgb, srgb: white = 255
        lines = []
        if compressed:
            px = max(1, bpp // 8)
            while len(lines) < height:
                rep = f.read(1)[0] + 1
                line = bytearray()
                while len(line) < bpl:
                    n = f.read(1)[0]
                    if n == 128:
                        line += bytes([blank]) * (bpl - len(line))
                    elif n < 128:
                        line += f.read(px) * (n + 1)
                    else:
                        line += f.read(px * (257 - n))
                lines += [bytes(line[:bpl])] * rep
            lines = lines[:height]
        else:
            lines = [f.read(bpl) for _ in range(height)]
        yield width, height, bpp, cspace, lines


def raster_label(width, height, bpp, cspace, lines):
    """One raster page (whole label, 16 mm high) → print lines, lead-in and lead-out cut off."""
    if height < HEAD:
        raise PrinterError(f"page is {height} dots high, expected {TAPE} (16 mm)")
    if width <= 2 * FEED:
        raise PrinterError("label shorter than the 8 mm the printer feeds anyway")
    top = (height - HEAD) / 2
    rows = [lines[min(height - 1, max(0, round(top + y)))] for y in range(HEAD)]
    light_is_high = cspace in (18, 1, 19)
    if bpp == 1:
        def black(x, y):
            return bool(rows[y][x >> 3] & (0x80 >> (x & 7))) != light_is_high
    else:
        step = bpp // 8
        if light_is_high:
            def black(x, y):
                return rows[y][x * step] < 128
        else:
            def black(x, y):
                return rows[y][x * step] >= 128
    return head_rows(width - 2 * FEED, lambda x, y: black(x + FEED, y))


# --- LZO (lzo1x_1, as used by the vendor app) --------------------------------

_lzo = ctypes.CDLL("liblzo2.so.2")
_lzo.__lzo_init_v2(0x2100, *([-1] * 9))
_wrk = ctypes.create_string_buffer(16384 * 8)


def lzo_compress(data):
    dst = ctypes.create_string_buffer(len(data) + len(data) // 16 + 67)
    n = ctypes.c_size_t(0)
    if _lzo.lzo1x_1_compress(data, ctypes.c_size_t(len(data)), dst, ctypes.byref(n), _wrk) != 0:
        raise PrinterError("LZO compression failed")
    return dst.raw[:n.value]
