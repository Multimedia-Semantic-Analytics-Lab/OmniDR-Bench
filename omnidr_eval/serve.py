"""Local static HTTP server with byte range support for video seeking."""

from __future__ import annotations
import argparse, os, re, socket
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

RANGE_RE = re.compile("bytes=(\\d*)-(\\d*)")


class RangeHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".m4a": "audio/mp4",
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".json": "application/json",
        ".svg": "image/svg+xml",
    }

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()
        path = self.translate_path(self.path)
        if os.path.isdir(path) or not os.path.exists(path):
            return super().send_head()
        m = RANGE_RE.match(rng.strip())
        if not m:
            self.send_error(400, "malformed Range")
            return None
        size = os.path.getsize(path)
        first, last = (m.group(1), m.group(2))
        if first == "":
            length = min(int(last or 0), size)
            start, end = (size - length, size - 1)
        else:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        return _Capped(f, end - start + 1)

    def log_message(self, fmt, *a):
        print(f"  {self.address_string()} {fmt % a}")


class _Capped:

    def __init__(self, f, n):
        self.f, self.left = (f, n)

    def read(self, sz=-1):
        if self.left <= 0:
            return b""
        if sz is None or sz < 0:
            sz = self.left
        b = self.f.read(min(sz, self.left))
        self.left -= len(b)
        return b

    def close(self):
        self.f.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8008)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dir", default=".")
    a = ap.parse_args()
    root = os.path.abspath(a.dir)
    os.chdir(root)
    ip = socket.gethostbyname(socket.gethostname())
    srv = ThreadingHTTPServer((a.host, a.port), RangeHandler)
    srv.daemon_threads = True
    print(
        f"serving {root}\n  http://127.0.0.1:{a.port}/\n  http://{ip}:{a.port}/\n  (Range/206 enabled — video seeking works)  Ctrl-C to stop",
        flush=True,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
