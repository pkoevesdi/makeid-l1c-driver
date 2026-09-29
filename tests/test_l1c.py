#!/usr/bin/env python3
"""Protocol side of l1c.py without a printer: frames, reply reassembly, print blocks, raster."""
import io
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import l1c  # noqa: E402
from test_lzo import decompress  # noqa: E402


def status_reply(code=0, busy=False, battery=80):
    b = bytearray(37)
    b[0], b[1], b[2], b[3] = 0x66, 37, 0, 0x10
    b[4] = code | (0x80 if busy else 0)
    b[5] = battery
    b[16:20] = struct.pack("<HH", 3000, 4000)
    b[20:26] = b"LC-16W"
    return bytes(b)


class FakeTransport:
    """Collects written frames and answers each complete one, like the printer."""

    def __init__(self, mtu=23, prefix=b"", split=20, answer=None):
        self.mtu, self.prefix, self.split = mtu, prefix, split
        self.answer = answer or (lambda frame: status_reply())
        self.chunks, self.frames, self.pending, self.buf = [], [], [], b""

    def write(self, data):
        assert len(data) <= self.mtu - 3, "chunk larger than MTU - 3"
        self.chunks.append(data)
        self.buf += data
        while len(self.buf) >= 3 and len(self.buf) >= (n := self.buf[1] | self.buf[2] << 8):
            frame, self.buf = self.buf[:n], self.buf[n:]
            self.frames.append(frame)
            reply = self.answer(frame)
            if reply is not None:
                r = self.prefix + reply
                self.pending += [r[i:i + self.split] for i in range(0, len(r), self.split)]

    def notification(self, timeout):
        return self.pending.pop(0) if self.pending else None

    def close(self):
        pass


def printer(transport):
    p = object.__new__(l1c.L1C)
    p.addr, p.t, p.mtu, p.buf = "fake", transport, transport.mtu, b""
    return p


class FrameTest(unittest.TestCase):
    def test_known_frames(self):
        self.assertEqual(l1c.frame(0x10, b"\x00").hex(" "), "66 06 00 10 00 84")
        self.assertEqual(l1c.frame(0x10, b"\x02").hex(" "), "66 06 00 10 02 82")
        self.assertEqual(l1c.frame(0x50).hex(" "), "66 05 00 50 45")

    def test_classify(self):
        self.assertEqual(l1c.classify(status_reply()), "ok")
        self.assertEqual(l1c.classify(status_reply(busy=True)), "wait")
        self.assertEqual(l1c.classify(status_reply(code=6)), "code6")
        self.assertEqual(l1c.classify(status_reply(code=23)), "ok")
        self.assertEqual(l1c.classify(None), "none")


class SendTest(unittest.TestCase):
    def test_reply_in_several_notifications(self):
        t = FakeTransport(split=20)
        r = printer(t).status()
        self.assertEqual(r, status_reply())
        st = l1c.parse_status(r)
        self.assertEqual((st["code"], st["battery"], st["total"], st["label"]), (0, 80, 4000, "LC-16W"))

    def test_hash_prefix_is_dropped(self):
        t = FakeTransport(prefix=b"##\x00\x00", split=7)
        self.assertEqual(printer(t).status(), status_reply())

    def test_no_reply(self):
        t = FakeTransport(answer=lambda f: None)
        self.assertIsNone(printer(t).send(l1c.frame(0x10, b"\x00"), timeout=0.2))

    def test_chunks_follow_mtu(self):
        for mtu in (23, 185, 517):
            t = FakeTransport(mtu=mtu)
            data = l1c.frame(0x1B, bytes(range(256)) * 8)
            printer(t).send(data)
            self.assertEqual(b"".join(t.chunks), data)
            self.assertTrue(all(len(c) == mtu - 3 for c in t.chunks[:-1]))


class PrintTest(unittest.TestCase):
    def test_blocks(self):
        rows = l1c.head_rows(300, lambda x, y: (x // 10 + y // 10) % 2 == 0)
        t = FakeTransport(mtu=517)
        st = printer(t).print_rows(rows, darkness=20)
        self.assertEqual(st["battery"], 80)
        self.assertEqual(t.frames[0], l1c.frame(0x10, b"\x02"))
        blocks = [f for f in t.frames if f[3] == 0x1B]
        self.assertEqual(len(blocks), 4)                           # 300 lines = 85 + 85 + 85 + 45
        data = b""
        for i, f in enumerate(blocks):
            self.assertEqual(sum(f) & 0xFF, 0, "checksum")
            b4, cut, labels, label, one, length, n, left, zero = struct.unpack_from("<BBHHBHHBB", f, 4)
            self.assertEqual((b4, cut, labels, label, one, length, zero), (20 | 0x20, 3, 1, 1, 1, 300, 0))
            self.assertEqual(left, len(blocks) - 1 - i)
            block = decompress(f[17:-1])
            self.assertEqual(len(block), 12 * n)
            data += block
        self.assertEqual(data, b"".join(rows))

    def test_resend(self):
        sent = []

        def answer(frame):
            sent.append(frame[3])
            if frame[3] == 0x1B and sent.count(0x1B) == 1:
                b = bytearray(status_reply())
                b[4] |= 0x40                                       # resend bit
                return bytes(b)
            return status_reply()
        t =FakeTransport(mtu=517, answer=answer)
        printer(t).print_rows(l1c.head_rows(50, lambda x, y: x == y))
        self.assertEqual(sent.count(0x1B), 2)

    def test_error_aborts(self):
        t = FakeTransport(mtu=517, answer=lambda f: status_reply(code=6 if f[3] == 0x1B else 0))
        with self.assertRaisesRegex(l1c.PrinterError, "cover open"):
            printer(t).print_rows(l1c.head_rows(50, lambda x, y: True))


class RowsTest(unittest.TestCase):
    def test_head_rows_layout(self):
        # left edge first; turned by 180 degrees, so the top dot of the design is the last head dot
        rows = l1c.head_rows(2, lambda x, y: x == 1 and y == 0)
        self.assertEqual(rows[0], bytes(12))
        self.assertEqual(rows[1], bytes(10) + bytes([0x01, 0]))   # byte pairs swapped

    def test_trim_end(self):
        rows = l1c.head_rows(100, lambda x, y: 10 <= x < 30)       # blank from x = 30 to the right
        self.assertEqual(l1c.trim_end(rows), l1c.head_rows(30, lambda x, y: 10 <= x < 30))
        self.assertEqual(len(l1c.trim_end(l1c.head_rows(50, lambda x, y: False))), 1)

    def test_cut_gap(self):
        gap = l1c.cut_gap()
        self.assertEqual(len(gap), 2 * l1c.FEED)                  # 8 mm
        marked = [i for i, r in enumerate(gap) if any(r)]
        self.assertEqual(marked, [l1c.FEED])                       # one dashed line in the middle
        self.assertTrue(0 < sum(bin(b).count("1") for b in gap[l1c.FEED]) < l1c.HEAD)


def pwg_raster(pages):
    """PWG raster (RaS2) from pages of (width, height, bits per pixel, colour space, lines)."""
    out = bytearray(b"RaS2")
    for width, height, bpp, cspace, lines in pages:
        bpl = len(lines[0])
        h = bytearray(1796)
        h[0:9] = b"PwgRaster"
        for off, v in ((276, 203), (280, 203), (372, width), (376, height), (384, 1 if bpp == 1 else 8),
                       (388, bpp), (392, bpl), (400, cspace)):
            struct.pack_into(">I", h, off, v)
        out += h
        px = max(1, bpp // 8)
        for line in lines:
            out.append(0)                                          # line repeat: once
            units = [line[i:i + px] for i in range(0, len(line), px)]
            i = 0
            while i < len(units):
                n = 1
                while i + n < len(units) and n < 128 and units[i + n] == units[i]:
                    n += 1
                out.append(n - 1)
                out += units[i]
                i += n
    return bytes(out)


def box(x, y):
    return 40 <= x < 280 and 40 <= y < 88


def sgray_page(width=320, height=128, black=box):
    return (width, height, 8, 18, [bytes(0 if black(x, y) else 255 for x in range(width)) for y in range(height)])


def black1_page(width=320, height=128, black=box):
    lines = []
    for y in range(height):
        b = bytearray((width + 7) // 8)
        for x in range(width):
            if black(x, y):
                b[x >> 3] |= 0x80 >> (x & 7)
        lines.append(bytes(b))
    return (width, height, 1, 3, lines)


def expected_rows(width=320, height=128, black=box):
    """What raster_label() should make of the page: from 4 mm in (or the first dot, if that is
    nearer the edge) up to the last dot."""
    top = (height - l1c.HEAD - 1) // 2
    inked = [x for x in range(width) if any(black(x, y + top) for y in range(l1c.HEAD))]
    start = min(l1c.FEED, inked[0])
    return l1c.head_rows(inked[-1] + 1 - start, lambda x, y: black(x + start, y + top))


class RasterTest(unittest.TestCase):
    def test_sgray_8(self):
        pages = list(l1c.read_raster(io.BytesIO(pwg_raster([sgray_page()]))))
        self.assertEqual(len(pages), 1)
        self.assertEqual(l1c.raster_label(*pages[0]), expected_rows())

    def test_black_1(self):
        pages = list(l1c.read_raster(io.BytesIO(pwg_raster([black1_page()]))))
        self.assertEqual(l1c.raster_label(*pages[0]), expected_rows())

    def test_windows_page_size(self):
        # Windows sends 40 x 16 mm as 319 x 127 dots (it converts through 1/100 inch)
        pages = list(l1c.read_raster(io.BytesIO(pwg_raster([sgray_page(319, 127)]))))
        self.assertEqual(l1c.raster_label(*pages[0]), expected_rows(319, 127))

    def test_content_up_to_the_page_end_is_kept(self):
        full = lambda x, y: y == 60 or x == 319                      # a line to the very last column
        pages = list(l1c.read_raster(io.BytesIO(pwg_raster([sgray_page(black=full)]))))
        self.assertEqual(len(l1c.raster_label(*pages[0])), 320)        # the line starts at x = 0: nothing cut

    def test_threshold(self):
        # anti-aliased thin lines arrive as mid grey and must still print
        grey = {100: 90, 101: 150, 102: 165, 103: 175, 104: 200, 105: 240}    # page columns
        lines = [bytes(grey.get(x, 255) for x in range(320))] * 128
        rows = l1c.raster_label(320, 128, 8, 18, lines)
        printed = [l1c.FEED + i for i, r in enumerate(rows) if any(r)]   # left edge first
        self.assertEqual(printed, [100, 101, 102])                  # up to 165 black, from 175 white
        self.assertEqual(rows[100 - l1c.FEED], b"\xff" * 12)

    def test_odd_height_keeps_every_line(self):
        # Windows sends 16 mm as 127 lines; every line of the band must reach the head once
        for row in range(15, 15 + l1c.HEAD):
            page = [bytes([0 if y == row else 255]) * 319 for y in range(127)]
            rows = l1c.raster_label(319, 127, 8, 18, page)
            dots = {y for r in rows for y in range(l1c.HEAD) if self._dot(r, y)}
            self.assertEqual(dots, {l1c.HEAD - 1 - (row - 15)}, f"page line {row}")   # turned by 180 degrees

    def test_cups_band_and_edge_lines(self):
        # CUPS sends 128 lines with the image one line up: 2 mm is line 15, 14 mm line 110.
        # A 0.1 mm line on either edge of the band lands on line 14 or 111 and must still print.
        for row, dot in ((14, l1c.HEAD - 1), (15, l1c.HEAD - 1), (110, 0), (111, 0)):
            page = [bytes([0 if y == row else 255]) * 320 for y in range(128)]
            rows = l1c.raster_label(320, 128, 8, 18, page)
            dots = {y for r in rows for y in range(l1c.HEAD) if self._dot(r, y)}
            self.assertEqual(dots, {dot}, f"page line {row}")
        for row in (13, 112):
            page = [bytes([0 if y == row else 255]) * 320 for y in range(128)]
            self.assertFalse(any(any(r) for r in l1c.raster_label(320, 128, 8, 18, page)), f"page line {row}")

    @staticmethod
    def _dot(row, y):
        b = bytearray(row)
        for k in range(0, 12, 2):
            b[k], b[k + 1] = b[k + 1], b[k]
        return bool(b[y >> 3] & (0x80 >> (y & 7)))

    def test_content_in_the_first_4_mm_is_kept(self):
        near = lambda x, y: 10 <= x < 20 and 40 <= y < 80           # starts 1.25 mm from the edge
        pages = list(l1c.read_raster(io.BytesIO(pwg_raster([sgray_page(black=near)]))))
        rows = l1c.raster_label(*pages[0])
        self.assertEqual(rows, expected_rows(black=near))
        self.assertEqual(len(rows), 10)                              # from x = 10 to x = 19

    def test_short_page(self):
        pages = list(l1c.read_raster(io.BytesIO(pwg_raster([sgray_page(60, 128)]))))   # box from x = 40
        self.assertEqual(len(l1c.raster_label(*pages[0])), 60 - l1c.FEED)

    def test_blank_page(self):
        # a short blank label, as other printers put out a blank sheet
        for width, black in ((20, lambda x, y: False), (320, lambda x, y: False), (320, lambda x, y: y < 10)):
            pages = list(l1c.read_raster(io.BytesIO(pwg_raster([sgray_page(width, 128, black)]))))
            self.assertEqual(l1c.raster_label(*pages[0]), [bytes(12)])


if __name__ == "__main__":
    unittest.main()
