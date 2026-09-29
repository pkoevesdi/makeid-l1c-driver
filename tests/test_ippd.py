#!/usr/bin/env python3
"""l1c-ippd end to end without a printer: the real server on a free port, a fake L1C behind it."""
import http.client
import importlib.util
import struct
import sys
import threading
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_l1c import black1_page, expected_rows, pwg_raster, sgray_page  # noqa: E402


def load_ippd():
    loader = SourceFileLoader("l1c_ippd", str(ROOT / "l1c-ippd"))
    spec = importlib.util.spec_from_loader("l1c_ippd", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ippd = load_ippd()


class FakeL1C:
    printed = []                       # (rows, darkness) per label
    times = []                         # time.monotonic() per label

    def __init__(self, addr=None, timeout=15):
        self.addr = addr or "fake"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def check_ready(self):
        return {}

    def print_rows(self, rows, darkness=15, continuous=True):
        FakeL1C.printed.append((rows, darkness))
        FakeL1C.times.append(time.monotonic())
        return {"battery": 99}


class Client:
    def __init__(self, port):
        self.port = port
        self.uri = f"ipp://127.0.0.1:{port}/ipp/print"
        self.rid = 0

    def request(self, op, op_attrs=(), job_attrs=(), doc=b"", version=0x0200):
        self.rid += 1
        body = bytearray(struct.pack(">HHI", version, op, self.rid))
        body.append(ippd.T_OP)
        for a in [(ippd.CHARSET, "attributes-charset", ["utf-8"]),
                  (ippd.LANG, "attributes-natural-language", ["en"]),
                  (ippd.URI, "printer-uri", [self.uri])] + list(op_attrs):
            body += ippd.encode_attr(*a)
        if job_attrs:
            body.append(ippd.T_JOB)
            for a in job_attrs:
                body += ippd.encode_attr(*a)
        body.append(ippd.T_END)
        body += doc
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/ipp/print", bytes(body), {"Content-Type": "application/ipp"})
        r = c.getresponse()
        data = r.read()
        c.close()
        _, status, rid, groups, _ = ippd.parse_request(data)
        assert rid == self.rid
        return status, groups

    def wait(self, job_id, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            _, g = self.request(ippd.GET_JOB_ATTRIBUTES, [(ippd.INTEGER, "job-id", [job_id])])
            state = g[ippd.T_JOB]["job-state"][0]
            if state >= ippd.CANCELED:
                return state, g[ippd.T_JOB]
            time.sleep(0.05)
        raise AssertionError("job did not finish")


class IppdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_l1c, cls.real_pause = ippd.l1c.L1C, ippd.LABEL_PAUSE
        ippd.l1c.L1C = FakeL1C
        ippd.LABEL_PAUSE = 0
        ippd.Handler.printer = ippd.Printer(None)
        ippd.log = lambda msg: None
        cls.srv = ippd.ThreadingHTTPServer(("127.0.0.1", 0), ippd.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.ipp = Client(cls.srv.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        ippd.l1c.L1C, ippd.LABEL_PAUSE = cls.real_l1c, cls.real_pause

    def setUp(self):
        FakeL1C.printed.clear()
        FakeL1C.times.clear()
        ippd.LABEL_PAUSE = 0

    def test_printer_attributes(self):
        status, g = self.ipp.request(ippd.GET_PRINTER_ATTRIBUTES)
        self.assertEqual(status, ippd.OK)
        p = g[ippd.T_PRINTER]
        self.assertIn("image/pwg-raster", p["document-format-supported"])
        self.assertEqual(p["pwg-raster-document-type-supported"], ["sgray_8"])   # l1c-ippd, not Windows, turns grey into black
        self.assertEqual(p["printer-resolution-supported"], [(203, 203, 3)])
        self.assertIn("ipp-everywhere", p["ipp-features-supported"])
        self.assertEqual(p["color-supported"], [False])
        self.assertEqual(p["print-scaling-supported"], ["none"])
        self.assertEqual(p["print-color-mode-supported"], ["monochrome"])

    def test_media_for_windows_and_cups(self):
        p = self.ipp.request(ippd.GET_PRINTER_ATTRIBUTES)[1][ippd.T_PRINTER]
        # Windows' IPP class driver rejects the printer if media-col-database holds a range
        # one paper size, nothing to choose in the print dialog
        self.assertEqual([c["media-size"][0] for c in p["media-col-database"]],
                         [{"x-dimension": [ippd.MAX_LEN * 100], "y-dimension": [1600]}])
        # CUPS takes custom lengths from the range in media-size-supported and custom_min/max
        ranges = [s["x-dimension"][0] for s in p["media-size-supported"] if isinstance(s["x-dimension"][0], tuple)]
        self.assertEqual(ranges, [(ippd.MIN_LEN * 100, ippd.MAX_LEN * 100)])
        self.assertIn(f"custom_min_{ippd.MIN_LEN}x16mm", p["media-supported"])
        self.assertIn(f"custom_max_{ippd.MAX_LEN}x16mm", p["media-supported"])

    def test_print_job_sgray(self):
        doc = pwg_raster([sgray_page()])
        status, g = self.ipp.request(ippd.PRINT_JOB, [(ippd.MIME, "document-format", ["image/pwg-raster"])],
                                     [(ippd.ENUM, "print-quality", [5])], doc)
        self.assertEqual(status, ippd.OK)
        state, _ = self.ipp.wait(g[ippd.T_JOB]["job-id"][0])
        self.assertEqual(state, ippd.COMPLETED)
        self.assertEqual(FakeL1C.printed, [(expected_rows(), 20)])

    def test_create_job_send_document_black1_copies(self):
        # The way Windows prints: Create-Job, then Send-Document
        status, g = self.ipp.request(ippd.CREATE_JOB, job_attrs=[(ippd.INTEGER, "copies", [2])])
        self.assertEqual(status, ippd.OK)
        jid = g[ippd.T_JOB]["job-id"][0]
        doc = pwg_raster([black1_page(), black1_page(400)])
        status, _ = self.ipp.request(ippd.SEND_DOCUMENT, [(ippd.INTEGER, "job-id", [jid]),
                                                          (ippd.BOOLEAN, "last-document", [True])], doc=doc)
        self.assertEqual(status, ippd.OK)
        state, _ = self.ipp.wait(jid)
        self.assertEqual(state, ippd.COMPLETED)
        # every label after the first is preceded by the 8 mm cut gap
        gap = ippd.l1c.cut_gap()
        n = len(expected_rows())                                    # both pages end with the box
        self.assertEqual([len(r) for r, _ in FakeL1C.printed], [n, 64 + n, 64 + n, 64 + n])
        self.assertEqual(FakeL1C.printed[0][0], expected_rows())
        self.assertEqual(FakeL1C.printed[2][0], gap + expected_rows())
        self.assertTrue(all(d == 15 for _, d in FakeL1C.printed))

    def test_pause_between_jobs(self):
        ippd.LABEL_PAUSE = 0.8
        doc = pwg_raster([sgray_page()])
        for _ in range(2):
            status, g = self.ipp.request(ippd.PRINT_JOB, doc=doc)
            self.assertEqual(self.ipp.wait(g[ippd.T_JOB]["job-id"][0])[0], ippd.COMPLETED)
        self.assertEqual(len(FakeL1C.times), 2)
        self.assertGreaterEqual(FakeL1C.times[1] - FakeL1C.times[0], 0.8)
        self.assertEqual(FakeL1C.printed[1][0], expected_rows())    # a new job starts without a gap

    def test_bad_document(self):
        status, g = self.ipp.request(ippd.PRINT_JOB, doc=b"%PDF-1.7\n")
        self.assertEqual(status, ippd.OK)
        state, job = self.ipp.wait(g[ippd.T_JOB]["job-id"][0])
        self.assertEqual(state, ippd.ABORTED)
        self.assertIn("not PWG", job["job-state-message"][0])
        self.assertEqual(FakeL1C.printed, [])

    def test_one_paper_size_and_the_end_trimmed(self):
        p = self.ipp.request(ippd.GET_PRINTER_ATTRIBUTES)[1][ippd.T_PRINTER]
        self.assertEqual(p["media-default"], [ippd.media_name(ippd.MAX_LEN)])
        content = lambda x, y: 40 <= x < 100 and 30 <= y < 90   # page x 40..99, rest blank
        for width in (ippd.MAX_LEN * 8, 60 * 8, 101):              # default paper, a custom page, just enough
            FakeL1C.printed.clear()
            status, g = self.ipp.request(ippd.PRINT_JOB, doc=pwg_raster([sgray_page(width, 128, content)]))
            self.assertEqual(self.ipp.wait(g[ippd.T_JOB]["job-id"][0])[0], ippd.COMPLETED)
            self.assertEqual(FakeL1C.printed[0][0], expected_rows(100, 128, content), f"page {width} dots")

    def test_unsupported_format(self):
        status, _ = self.ipp.request(ippd.VALIDATE_JOB, [(ippd.MIME, "document-format", ["application/pdf"])])
        self.assertEqual(status, ippd.FORMAT_UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
