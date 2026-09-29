#!/usr/bin/env python3
"""Tests against a real L1-C. They print five short labels, 10 s apart so that the printer
feeds its lead-out after each: 20 mm, 25 mm, about 50 mm ("L1-C test"), and twice 20 mm in one
job with a dashed cut line between them. Cut the tape only after the last one.

Skipped unless L1C_PRINTER=1 is set, so that a plain unittest run needs no printer:

    L1C_PRINTER=1 python3 -m unittest -v tests.test_printer          (Linux)
    $env:L1C_PRINTER=1; python -m unittest -v tests.test_printer     (Windows PowerShell)

The printer is found by name, or taken from $L1C_ADDR.
"""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
import l1c  # noqa: E402

ENABLED = os.environ.get("L1C_PRINTER") == "1"
PAUSE = 10                              # s between labels, as l1c-ippd waits between jobs
_last = [float("-inf")]


def pause():
    time.sleep(max(0, _last[0] + PAUSE - time.monotonic()))


def printed():
    _last[0] = time.monotonic()


def pattern(x, y, length):
    """Frame, diagonal stripes and a solid block: shows missing lines, shifts and dropouts."""
    return (x < 2 or x >= length - 2 or y < 2 or y >= 94 or (x + y) % 16 < 3
            or (length // 2 - 8 <= x < length // 2 + 8 and 40 <= y < 56))


@unittest.skipUnless(ENABLED, "set L1C_PRINTER=1 to run the tests with a real printer")
class PrinterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addr = os.environ.get("L1C_ADDR") or l1c.find_printer()

    def test_1_status(self):
        with l1c.L1C(self.addr) as p:
            st = l1c.parse_status(p.status())
            fw = p.firmware()
            self.assertGreaterEqual(p.mtu, 23)
        print(f"\n  {self.addr}: {st}, firmware {fw}", file=sys.stderr)
        self.assertEqual(st["code"], 0, l1c.STATUS_TEXT.get(st["code"], st["code"]))
        self.assertTrue(st["label"], "no tape type reported")
        self.assertEqual(len(fw), 3)
        if st["battery"] < 15 and not st["charging"]:
            print(f"  battery {st['battery']} %: charge the printer", file=sys.stderr)

    def test_2_print_direct(self):
        length = 20 * 8 - 2 * l1c.FEED                           # 20 mm label
        rows = l1c.head_rows(length, lambda x, y: pattern(x, y, length))
        pause()
        with l1c.L1C(self.addr) as p:
            p.check_ready()
            st = p.print_rows(rows)
        printed()
        self.assertIsNotNone(st, "no status after printing")
        self.assertEqual(st["code"], 0)

    def ippd_job(self, length_mm, copies=1):
        """Print one page through a real l1c-ippd on a free port."""
        import test_ippd
        from test_l1c import pwg_raster, sgray_page
        ippd = test_ippd.ippd
        ippd.Handler.printer = ippd.Printer(self.addr)
        srv = ippd.ThreadingHTTPServer(("127.0.0.1", 0), ippd.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            client = test_ippd.Client(srv.server_address[1])
            width = length_mm * 8                                # page 16 mm high
            inner = width - 2 * l1c.FEED                         # without the 4 mm cut off at each end

            def black(x, y):
                return l1c.FEED <= x < width - l1c.FEED and 16 <= y < 112 and pattern(x - l1c.FEED, y - 16, inner)
            doc = pwg_raster([sgray_page(width, 128, black)])
            pause()
            status, g = client.request(ippd.PRINT_JOB, [(ippd.MIME, "document-format", ["image/pwg-raster"])],
                                       [(ippd.INTEGER, "copies", [copies])], doc)
            self.assertEqual(status, ippd.OK)
            state, job = client.wait(g[ippd.T_JOB]["job-id"][0], timeout=60 + 30 * copies)
            printed()
            self.assertEqual(state, ippd.COMPLETED, job.get("job-state-message"))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_3_print_via_ippd(self):
        self.ippd_job(25)

    def test_4_l1c_print(self):
        env = dict(os.environ, L1C_ADDR=self.addr)
        pause()
        r = subprocess.run([sys.executable, str(ROOT / "l1c-print"), "-t", "L1-C test"],
                           capture_output=True, text=True, env=env, timeout=120)
        printed()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("printed", r.stdout)

    def test_5_copies_with_cut_line(self):
        self.ippd_job(20, copies=2)


if __name__ == "__main__":
    unittest.main()
