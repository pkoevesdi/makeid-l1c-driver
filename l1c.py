"""MakeID L1-C label printer over Bluetooth LE: connection, rasterising, printing."""
import asyncio
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time

H_WRITE, H_NOTIFY, H_CCCD = 0x2A, 0x2E, 0x2F   # ABF1 value, ABF2 value, ABF2 CCCD
UUID_WRITE = "0000abf1-0000-1000-8000-00805f9b34fb"
UUID_NOTIFY = "0000abf2-0000-1000-8000-00805f9b34fb"
LINUX = sys.platform.startswith("linux")

HEAD = 96            # print head dots, 203 dpi = 12 mm
TAPE = 128           # tape width, 16 mm
MARGIN = (TAPE - HEAD) // 2
FEED = 32            # the printer adds 4 mm before and after the image
BLOCK_ROWS = 85
THRESHOLD = 170      # grey levels (0 = black, 255 = white) below this print black: thin
                     # anti-aliased lines (0.1 mm is less than a dot) stay visible
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
    """Return the address of an L1 printer (Linux: known to or found by BlueZ; else a BLE scan)."""
    if not LINUX:
        return _bleak_find(scan_seconds)

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


# --- Transports: write() sends one ATT write without response, notification() returns the
# --- next ABF2 value or None when the timeout runs out.

class SocketTransport:
    """Linux: raw L2CAP socket on the ATT channel.

    BlueZ' Device1.Connect() tries BR/EDR for this printer and times out.
    """

    def __init__(self, addr, timeout):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        s.settimeout(timeout)
        s.bind(("00:00:00:00:00:00", 0, 4, 1))       # ATT channel, LE public address
        try:
            s.connect((addr, 0, 4, 1))
        except OSError as e:
            s.close()
            raise PrinterError(f"printer {addr} not reachable ({e})") from e
        self.s = s
        s.send(struct.pack("<BH", 0x02, 517))
        self.mtu = min(517, struct.unpack("<H", self._att(0x03)[1:3])[0])
        s.send(struct.pack("<BH", 0x12, H_CCCD) + b"\x01\x00")
        self._att(0x13)

    def _att(self, want):
        while True:
            r = self.s.recv(1024)
            if r[0] == want:
                return r
            if r[0] == 0x01:
                raise PrinterError("ATT error " + r.hex())

    def write(self, data):
        self.s.send(struct.pack("<BH", 0x52, H_WRITE) + data)

    def notification(self, timeout):
        end = time.time() + timeout
        while (left := end - time.time()) > 0:
            self.s.settimeout(left)
            try:
                r = self.s.recv(1024)
            except socket.timeout:
                return None
            if r[0] == 0x1B and struct.unpack("<H", r[1:3])[0] == H_NOTIFY:
                return r[3:]
        return None

    def close(self):
        self.s.close()


_loop = None
_found = {}          # address → BLEDevice from the last scan, saves a second scan on connect


def _run(coro, timeout=None):
    """Run a coroutine on the bleak event loop thread and wait for its result."""
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, daemon=True, name="bleak").start()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


def _bleak_find(scan_seconds):
    from bleak import BleakScanner

    def match(dev, adv):
        return bool(NAME_RE.match(adv.local_name or dev.name or ""))
    dev = _run(BleakScanner.find_device_by_filter(match, timeout=scan_seconds))
    if not dev:
        raise PrinterError("no L1 printer found; is it switched on?")
    _found[dev.address] = dev
    return dev.address


class BleakTransport:
    """Windows and macOS: GATT through bleak (service ABF0)."""

    def __init__(self, addr, timeout):
        from bleak import BleakClient
        self.q = queue.Queue()
        # Right after a disconnect Windows may cancel the next connect; try a few times.
        for attempt in range(3):
            self.c = BleakClient(_found.get(addr, addr), timeout=timeout)
            try:
                _run(self.c.connect(), timeout + 5)
                _run(self.c.start_notify(UUID_NOTIFY, lambda _, data: self.q.put(bytes(data))), timeout)
                break
            except Exception as e:  # noqa: BLE001  (BleakError, TimeoutError, OSError from WinRT)
                self.close()
                if attempt == 2:
                    raise PrinterError(f"printer {addr} not reachable ({e or type(e).__name__})") from e
                time.sleep(2)
        self.mtu = min(517, self.c.mtu_size)

    def write(self, data):
        try:
            _run(self.c.write_gatt_char(UUID_WRITE, data, response=False), 10)
        except Exception as e:  # noqa: BLE001
            raise PrinterError(f"write failed ({e or type(e).__name__})") from e

    def notification(self, timeout):
        try:
            return self.q.get(timeout=max(0, timeout))
        except queue.Empty:
            return None

    def close(self):
        try:
            _run(self.c.disconnect(), 10)
        except Exception:  # noqa: BLE001
            pass


class L1C:
    def __init__(self, addr=None, timeout=15):
        addr = addr or os.environ.get("L1C_ADDR") or find_printer()
        self.addr = addr
        self.t = (SocketTransport if LINUX else BleakTransport)(addr, timeout)
        self.mtu = self.t.mtu
        self.buf = b""

    def close(self):
        self.t.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def send(self, data, timeout=5):
        self.buf = b""
        chunk = self.mtu - 3
        for i in range(0, len(data), chunk):
            self.t.write(data[i:i + chunk])
        end = time.time() + timeout
        while (left := end - time.time()) > 0:
            r = self.t.notification(left)
            if r is None:
                break
            self.buf += r
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
    """Print lines for one label in design orientation.

    length: image length in dots.
    black(x, y): True for a dot; x = 0..length-1 from the left, y = 0..HEAD-1 from the top
    of the 12 mm printable band. The left edge of the design goes through the head first and
    the image is turned by 180°: with the LED side towards you, the tape comes out to the left
    and reads normally.
    """
    out = []
    for x in range(length):
        r = bytearray(HEAD // 8)
        for y in range(HEAD):
            if black(x, HEAD - 1 - y):
                r[y >> 3] |= 0x80 >> (y & 7)
        for k in range(0, len(r), 2):
            r[k], r[k + 1] = r[k + 1], r[k]
        out.append(bytes(r))
    return out


def trim_end(rows):
    """Drop the blank tape at the right-hand end of the design: the lines printed last.

    The printer still feeds its 4 mm lead-out, so the label ends 4 mm after the last dot.
    A blank page keeps one line: it comes out as a short blank label, as on other printers.
    """
    for i in range(len(rows), 0, -1):
        if any(rows[i - 1]):
            return rows[:i]
    return rows[:1]


def cut_gap():
    """Print lines to put between two labels of a job, in front of the second.

    Labels printed one after the other follow without a gap; the printer feeds its 4 mm
    lead-out only after a pause. These 8 mm of blank tape, with a dashed cut line in the
    middle, give each label the same 4 mm on both sides as a label printed alone.
    """
    blank = bytes(HEAD // 8)
    return [blank] * FEED + head_rows(1, lambda x, y: (y // 4) % 2 == 0) + [blank] * (FEED - 1)


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
    """One raster page (16 mm high, the whole label) → print lines.

    The printer feeds 4 mm of blank tape before and after the image. So the first 4 mm of the
    page are left out as far as they are blank, and the blank end of the page is dropped: the
    label ends 4 mm after its last dot, whatever the page length. Nothing drawn is cut off.
    """
    light_is_high = cspace in (18, 1, 19)
    if bpp == 1:
        def pixel(x, y):
            return bool(lines[y][x >> 3] & (0x80 >> (x & 7))) != light_is_high
    else:
        step = bpp // 8
        if light_is_high:
            def pixel(x, y):
                return lines[y][x * step] < THRESHOLD
        else:
            def pixel(x, y):
                return lines[y][x * step] > 255 - THRESHOLD
    if height < HEAD:
        raise PrinterError(f"page is {height} dots high, expected {TAPE} (16 mm)")
    # Whole lines: Windows sends 127, and x.5 would drop every other line. CUPS sends 128 but
    # hands Ghostscript the page height rounded down to 45 pt, which moves the image up one line;
    # 15 is the line at 2 mm in both.
    top = (height - HEAD - 1) // 2

    def band(x, y):
        # A line on the edge of the 12 mm band is split between the last line inside and the
        # first outside; take the outside line in so that it prints either way.
        if y == 0 and top > 0:
            return pixel(x, top) or pixel(x, top - 1)
        if y == HEAD - 1 and top + HEAD < height:
            return pixel(x, top + y) or pixel(x, top + HEAD)
        return pixel(x, top + y)

    start = next((x for x in range(min(FEED, width)) if any(band(x, y) for y in range(HEAD))), FEED)
    if width <= start:                 # blank page no wider than the lead-in
        return [bytes(HEAD // 8)]
    return trim_end(head_rows(width - start, lambda x, y: band(x + start, y)))


# --- LZO (lzo1x_1, as used by the vendor app) --------------------------------
# Port of lzo1x_1_compress from LZO 2.10 as liblzo2 is built on 64-bit x86 and ARM:
# deterministic 16-bit dictionary, match length extended 8 bytes at a time.
# The output is byte for byte that of liblzo2; tests/test_lzo.py checks this.

def _lzo_le32(b, p):
    return b[p] | b[p + 1] << 8 | b[p + 2] << 16 | b[p + 3] << 24


def _lzo_literals(out, src, ii, t):
    if t <= 3:
        out[-2] |= t
    elif t <= 18:
        out.append(t - 3)
    else:
        tt = t - 18
        out.append(0)
        while tt > 255:
            tt -= 255
            out.append(0)
        out.append(tt)
    out += src[ii:ii + t]


def _lzo_block(src, start, ll, out, ti):
    """do_compress(): one chunk of at most 49152 bytes; returns the literals left over."""
    ip_end = start + ll - 20
    d = [0] * (1 << 14)
    ii = start
    ip = start + (4 - ti if ti < 4 else 0)
    ip += 1 + ((ip - ii) >> 5)
    while ip < ip_end:
        dv = _lzo_le32(src, ip)
        k = ((0x1824429D * dv) & 0xFFFFFFFF) >> 18
        m_pos = start + d[k]
        d[k] = (ip - start) & 0xFFFF
        if dv != _lzo_le32(src, m_pos):
            ip += 1 + ((ip - ii) >> 5)
            continue
        ii -= ti
        ti = 0
        if ip > ii:
            _lzo_literals(out, src, ii, ip - ii)
        m_len = 4
        v = int.from_bytes(src[ip + 4:ip + 12], "little") ^ int.from_bytes(src[m_pos + 4:m_pos + 12], "little")
        done = False
        if v == 0:
            while True:
                m_len += 8
                v = (int.from_bytes(src[ip + m_len:ip + m_len + 8], "little")
                     ^ int.from_bytes(src[m_pos + m_len:m_pos + m_len + 8], "little"))
                if ip + m_len >= ip_end:
                    done = True
                    break
                if v != 0:
                    break
        if not done:
            m_len += ((v & -v).bit_length() - 1) >> 3
        m_off = ip - m_pos
        ip += m_len
        ii = ip
        if m_len <= 8 and m_off <= 0x800:
            m_off -= 1
            out += bytes([((m_len - 1) << 5) | ((m_off & 7) << 2), m_off >> 3])
            continue
        if m_off <= 0x4000:
            m_off -= 1
            marker, max_len = 32, 33
        else:
            m_off -= 0x4000
            marker, max_len = 16 | ((m_off >> 11) & 8), 9
        if m_len <= max_len:
            out.append(marker | (m_len - 2))
        else:
            m_len -= max_len
            out.append(marker)
            while m_len > 255:
                m_len -= 255
                out.append(0)
            out.append(m_len)
        out += bytes([(m_off << 2) & 0xFF, (m_off >> 6) & 0xFF])
    return start + ll - (ii - ti)


def lzo_compress(data):
    """LZO1X-1, raw stream without header."""
    src = bytes(data)
    out = bytearray()
    ip, left, t = 0, len(src), 0
    while left > 20:
        ll = min(left, 49152)
        if (t + ll) >> 5 == 0:          # liblzo2's pointer overflow check, true below 32 bytes
            break
        t = _lzo_block(src, ip, ll, out, t)
        ip += ll
        left -= ll
    t += left
    if t > 0:
        if not out and t <= 238:
            out.append(17 + t)
            out += src[len(src) - t:]
        else:
            _lzo_literals(out, src, len(src) - t, t)
    out += b"\x11\x00\x00"
    return bytes(out)
