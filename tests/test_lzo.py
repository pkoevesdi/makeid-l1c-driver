#!/usr/bin/env python3
"""Check l1c.lzo_compress against liblzo2, byte for byte.

References, whichever are available:
  - liblzo2 through ctypes (Linux: apt install liblzo2-2): lzo1x_1_compress, lzo1x_decompress_safe
  - python-lzo (Windows wheels up to Python 3.11), built from the liblzo2 sources

Every case is also decompressed by a strict decoder written here, independent of liblzo2.
The digest at the end covers all compressed outputs; it must be the same on every system
(liblzo2 as built for 64-bit x86 and ARM; 32-bit builds extend matches differently).
Run directly for a report, or through unittest (python -m unittest discover tests).
"""
import ctypes
import hashlib
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import l1c  # noqa: E402


# --- references --------------------------------------------------------------

def ctypes_ref():
    for name in ("liblzo2.so.2", "liblzo2.2.dylib", "lzo2.dll", "liblzo2-2.dll"):
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        if lib.__lzo_init_v2(0x2100, *([-1] * 9)) != 0:
            raise SystemExit(f"{name}: lzo_init failed")
        wrk = ctypes.create_string_buffer(16384 * 8)

        def compress(data):
            dst = ctypes.create_string_buffer(len(data) + len(data) // 16 + 67)
            n = ctypes.c_size_t(0)
            assert lib.lzo1x_1_compress(data, ctypes.c_size_t(len(data)), dst, ctypes.byref(n), wrk) == 0
            return dst.raw[:n.value]

        def decompress(data, size):
            dst = ctypes.create_string_buffer(size + 1)       # one spare byte: detects overlong output
            n = ctypes.c_size_t(size + 1)
            r = lib.lzo1x_decompress_safe(data, ctypes.c_size_t(len(data)), dst, ctypes.byref(n), None)
            if r != 0:
                raise ValueError(f"lzo1x_decompress_safe returned {r}")
            return dst.raw[:n.value]
        return f"liblzo2 ({name})", compress, decompress
    return None


def python_lzo_ref():
    try:
        import lzo
    except ImportError:
        return None
    return (f"python-lzo {lzo.__version__.decode()} (LZO {lzo.LZO_VERSION_STRING.decode()})",
            lambda d: lzo.compress(d, 1, False), lambda d, size: lzo.decompress(d, False, size))


# --- independent strict LZO1X decoder ----------------------------------------

def decompress(src):
    """LZO1X decoder, rejects anything malformed: overruns, bad distances, trailing bytes."""
    out = bytearray()
    ip = 0

    def byte():
        nonlocal ip
        if ip >= len(src):
            raise ValueError("input overrun")
        ip += 1
        return src[ip - 1]

    def length(t, bits):
        if t:
            return t
        n = 0
        while (b := byte()) == 0:
            n += 255
        return n + bits + b

    def literals(n):
        nonlocal ip
        if ip + n > len(src):
            raise ValueError("input overrun in literals")
        out.extend(src[ip:ip + n])
        ip += n

    def copy(dist, n):
        if dist <= 0 or dist > len(out):
            raise ValueError(f"match distance {dist} outside output ({len(out)} bytes)")
        for _ in range(n):
            out.append(out[-dist])

    # mode: "run" = a literal run may follow, "short" = 1–3 literals were copied,
    # "long" = 4+ literals were copied (changes the meaning of instructions below 16)
    t = byte()
    if t > 17:
        literals(t - 17)
        mode = "short" if t - 17 < 4 else "long"
    else:
        ip -= 1
        mode = "run"
    while True:
        t = byte()
        if t < 16 and mode == "run":                    # literal run
            literals(length(t, 15) + 3)
            mode = "long"
            continue
        if t < 16:                                      # M1
            b = byte()
            if mode == "long":
                copy(2049 + (t >> 2) + (b << 2), 3)
            else:
                copy(1 + (t >> 2) + (b << 2), 2)
            low = t
        elif t >= 64:                                   # M2
            copy(1 + ((t >> 2) & 7) + (byte() << 3), (t >> 5) + 1)
            low = t
        elif t >= 32:                                   # M3
            n = length(t & 31, 31) + 2
            b0, b1 = byte(), byte()
            copy(1 + ((b0 | b1 << 8) >> 2), n)
            low = b0
        else:                                           # M4 or end of stream
            n = length(t & 7, 7) + 2
            b0, b1 = byte(), byte()
            dist = ((t & 8) << 11) + ((b0 | b1 << 8) >> 2)
            if dist == 0:
                if t != 0x11 or b0 or b1:
                    raise ValueError("bad end marker")
                if ip != len(src):
                    raise ValueError(f"{len(src) - ip} bytes after end marker")
                return bytes(out)
            copy(dist + 0x4000, n)
            low = b0
        if low & 3:
            literals(low & 3)
            mode = "short"
        else:
            mode = "run"


# --- test data -----------------------------------------------------------------

def head_rows(length, black):
    """Print lines as l1c built them when the stored digest was checked against liblzo2.

    A fixed copy, so that a change of the print orientation in l1c does not change the inputs.
    """
    out = []
    for i in range(length):
        x = length - 1 - i
        r = bytearray(12)
        for y in range(96):
            if black(x, y):
                r[y >> 3] |= 0x80 >> (y & 7)
        for k in range(0, 12, 2):
            r[k], r[k + 1] = r[k + 1], r[k]
        out.append(bytes(r))
    return out


def label_blocks(rng):
    """Print blocks as l1c sends them: 85 lines of 12 bytes, from synthetic label images."""
    shapes = {
        "blank": lambda x, y: False,
        "black": lambda x, y: True,
        "stripes": lambda x, y: (x // 7) % 2 == 0,
        "hstripes": lambda x, y: (y // 5) % 2 == 0,
        "checker": lambda x, y: (x // 4 + y // 4) % 2 == 0,
        "frame": lambda x, y: x < 3 or y < 3 or y > 92 or x % 200 > 196,
        "diagonal": lambda x, y: (x + y) % 23 < 3,
        "circles": lambda x, y: ((x % 96 - 48) ** 2 + (y - 48) ** 2) // 60 % 3 == 0,
    }
    dots = {}
    for name, f in shapes.items():
        yield name, head_rows(170, f)
    for density in (0.01, 0.1, 0.5, 0.9):
        pts = {(x, y) for x in range(400) for y in range(96) if rng.random() < density}
        dots[density] = pts
        yield f"dots{density}", head_rows(400, lambda x, y, p=pts: (x, y) in p)
    # text-like: random glyph boxes of strokes
    glyphs = [{(x, y) for x in range(12) for y in range(40) if rng.random() < 0.35} for _ in range(20)]
    text = [rng.randrange(20) for _ in range(60)]
    yield "text", head_rows(60 * 14, lambda x, y: 28 <= y < 68 and (x % 14, y - 28) in glyphs[text[x // 14]])


def cases():
    rng = random.Random(20260929)
    for name, rows in label_blocks(rng):
        for i in range(0, len(rows), l1c.BLOCK_ROWS):
            yield f"label-{name}-{i // l1c.BLOCK_ROWS}", b"".join(rows[i:i + l1c.BLOCK_ROWS])
    for n in list(range(0, 80)) + [100, 237, 238, 239, 240, 255, 256, 257, 273, 274, 275, 1020]:
        yield f"random-{n}", rng.randbytes(n)
        yield f"zeros-{n}", bytes(n)
        yield f"ff-{n}", b"\xff" * n
        yield f"period3-{n}", (b"abc" * n)[:n]
    for n in (1000, 5000, 49151, 49152, 49153, 49172, 49173, 65535, 65536, 65537, 98304, 150000):
        yield f"zeros-{n}", bytes(n)
        yield f"random-{n}", rng.randbytes(n)
        yield f"sparse-{n}", bytes(rng.choice(b"\0\0\0\0\0\0\x01\x80\xff") for _ in range(n))
        yield f"text-{n}", bytes(rng.choice(b"the quick brown fox jumps over a lazy dog  ") for _ in range(n))
    # long matches at every distance class (M2 <= 2 kB, M3 <= 16 kB, M4 beyond) and long literal runs
    for dist in (1, 2, 3, 7, 8, 9, 2047, 2048, 2049, 2050, 16383, 16384, 16385, 16386, 30000, 49000):
        for mlen in (3, 4, 8, 9, 33, 34, 300, 700):
            pre = rng.randbytes(dist)
            yield f"match-d{dist}-l{mlen}", pre + (pre * (mlen // dist + 2))[:mlen] + rng.randbytes(40)
    for n in (19, 20, 21, 31, 32, 33, 50, 290, 1000):
        body = rng.randbytes(n)
        yield f"litrun-{n}", body + body[:64] + rng.randbytes(n) + bytes(300)
    for k in range(300):
        n = rng.choice((rng.randrange(21, 200), rng.randrange(200, 3000), rng.randrange(3000, 70000)))
        alphabet = rng.randbytes(rng.choice((1, 2, 3, 4, 8, 16, 64, 256)))
        run = rng.choice((1, 1, 2, 5, 20))
        data = bytearray()
        while len(data) < n:
            if rng.random() < 0.1 and len(data) > 8:
                d = rng.randrange(1, len(data) + 1)
                m = rng.randrange(3, 120)
                for _ in range(m):
                    data.append(data[-d])
            else:
                data += bytes([rng.choice(alphabet)]) * run
        yield f"mixed-{k}", bytes(data[:n])


# --- run -----------------------------------------------------------------------

# Digest of all outputs as produced by liblzo2 2.10 (python-lzo 1.15, Windows x64). With it the
# test proves byte equality even where no liblzo2 is installed.
EXPECTED_DIGEST = "aedc7a49d71f15db481a192c04934461"


def run(refs, report=print):
    """→ (cases, failures, digest)."""
    digest = hashlib.sha256()
    failures = 0
    count = 0
    for name, data in cases():
        count += 1
        ours = l1c.lzo_compress(data)
        digest.update(len(ours).to_bytes(4, "little") + ours)
        problems = []
        try:
            if decompress(ours) != data:
                problems.append("own decoder: wrong output")
        except ValueError as e:
            problems.append(f"own decoder: {e}")
        for rname, rcompress, rdecompress in refs:
            ref = rcompress(data)
            if ref != ours:
                i = next((i for i, (a, b) in enumerate(zip(ref, ours)) if a != b), min(len(ref), len(ours)))
                problems.append(f"{rname}: output differs at byte {i} (ours {len(ours)}, ref {len(ref)} bytes)")
            try:
                if rdecompress(ours, len(data)) != data:
                    problems.append(f"{rname} decompress_safe: wrong output")
            except Exception as e:  # noqa: BLE001
                problems.append(f"{rname} decompress_safe: {e}")
            try:
                if decompress(ref) != data:
                    problems.append(f"own decoder on {rname} output: wrong output")
            except ValueError as e:
                problems.append(f"own decoder on {rname} output: {e}")
        if problems:
            failures += 1
            report(f"FAIL {name} ({len(data)} bytes): " + "; ".join(problems))
    return count, failures, digest.hexdigest()[:32]


def references():
    return [r for r in (ctypes_ref(), python_lzo_ref()) if r]


class LzoTest(unittest.TestCase):
    def test_against_liblzo2(self):
        refs = references()
        count, failures, digest = run(refs, report=lambda m: None)
        self.assertEqual(failures, 0, f"{failures} of {count} cases failed; run tests/test_lzo.py for details")
        self.assertEqual(digest, EXPECTED_DIGEST, "output differs from liblzo2 2.10")


def main():
    refs = references()
    print("reference:", ", ".join(r[0] for r in refs) or "none (stored digest only)")
    count, failures, digest = run(refs)
    ok = digest == EXPECTED_DIGEST
    print(f"{count} cases, {failures} failed, digest {digest} ({'matches' if ok else 'DIFFERS FROM'} liblzo2 2.10)")
    return 1 if failures or not ok else 0


if __name__ == "__main__":
    sys.exit(main())
