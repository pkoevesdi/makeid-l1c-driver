"""Start l1c-ippd without a console window (pythonw.exe), for the Windows autostart.

Arguments are passed on to l1c-ippd. The log goes to %LOCALAPPDATA%\\l1c-ippd\\l1c-ippd.log.
"""
import http.client
import os
import runpy
import socketserver
import sys
from pathlib import Path

IPPD = Path(__file__).resolve().parent.parent / "l1c-ippd"


def running(port):
    """True if a server already answers on the port (Windows lets a second one bind it)."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/")
        c.getresponse().read()
        c.close()
        return True
    except OSError:
        return False


args = sys.argv[1:]
port = 8631
for i, a in enumerate(args):
    if a in ("-p", "--port") and i + 1 < len(args):
        port = int(args[i + 1])
    elif a.startswith("--port="):
        port = int(a.split("=", 1)[1])
if running(port):
    sys.exit()

log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "l1c-ippd"
log_dir.mkdir(parents=True, exist_ok=True)
sys.stdout = sys.stderr = open(log_dir / "l1c-ippd.log", "w", buffering=1, encoding="utf-8")

# Windows clients end keep-alive connections with a reset; do not log a traceback for each.
_handle_error = socketserver.BaseServer.handle_error
socketserver.BaseServer.handle_error = lambda self, request, address: (
    None if isinstance(sys.exc_info()[1], ConnectionResetError) else _handle_error(self, request, address))

sys.argv = [str(IPPD)] + args
runpy.run_path(str(IPPD), run_name="__main__")
