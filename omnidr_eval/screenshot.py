"""Capture the report screenshot used by visual judgments."""

from __future__ import annotations
import argparse
import os
import sys


def _err(msg: str, code: int = 1):
    print(f"[screenshot] {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> None:
    p = argparse.ArgumentParser(description="Screenshot a web page with Playwright.")
    p.add_argument("url", help="http(s):// URL or file:///abs/path.html")
    p.add_argument("out", help="output PNG path")
    p.add_argument(
        "--full-page", action="store_true", help="capture full scrollable page"
    )
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=800)
    p.add_argument("--wait", type=int, default=1500, help="ms to wait after load")
    p.add_argument("--selector", default=None, help="screenshot only this element")
    p.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY"))
    args = p.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _err(
            "Playwright not installed. Run:\n    pip install playwright && playwright install chromium"
        )
    launch_kwargs = {"headless": True}
    if args.proxy:
        launch_kwargs["proxy"] = {"server": args.proxy}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        try:
            page.goto(args.url, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"[screenshot] warn: {e}", file=sys.stderr)
        page.wait_for_timeout(args.wait)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        if args.selector:
            el = page.query_selector(args.selector)
            if not el:
                browser.close()
                _err(f"selector not found: {args.selector}")
            el.screenshot(path=args.out)
        else:
            page.screenshot(path=args.out, full_page=args.full_page)
        browser.close()
    print(f"[screenshot] saved {args.out}")


if __name__ == "__main__":
    main()
