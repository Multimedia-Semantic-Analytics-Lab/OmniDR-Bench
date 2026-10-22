"""Browser measurements for report structure, media, and interactive behavior."""

from __future__ import annotations
import argparse, functools, json, os, sys, threading, time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
from .serve import RangeHandler

DESKTOP = (1440, 900)
MOBILE = (390, 844)
SEEK_TOL = 1.5
SAMPLE_CITES = 8
SAMPLE_IMGS = 8


class _Server:

    def __init__(self, directory: str):
        d = os.path.abspath(directory)
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(RangeHandler, directory=d)
        )
        self.httpd.handle_error = lambda req, addr: None
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.t.start()
        return f"http://127.0.0.1:{self.port}/index.html"

    def __exit__(self, *e):
        self.httpd.shutdown()
        self.httpd.server_close()


JS_STATIC = r"""
() => {
  const $ = (s) => Array.from(document.querySelectorAll(s));
  const T = (n) => (n && n.textContent ? n.textContent : "").replace(/\s+/g, " ").trim();
  const D = window.DATA || {};
  const root = document.documentElement;
  const cs = getComputedStyle(root);

  const theme = root.getAttribute("data-theme") || "";
  const meta = {
    theme: theme,
    theme_declared: (D.meta && D.meta.theme) || null,
    theme_valid: /^t(0[1-9]|1[0-9]|20)$/.test(String((D.meta && D.meta.theme) || "")),
    category: (D.meta && D.meta.category) || null,
    lang: root.lang || null,
    has_bg: root.hasAttribute("data-has-bg"),
    bg: (D.meta && D.meta.background) || null,
    accent: cs.getPropertyValue("--accent").trim(),
    font_display: cs.getPropertyValue("--font-display").trim(),
  };

  const paras = $("main.right p").map((p) => T(p).length).filter((n) => n > 0);
  const headsSeq = $("main.right h1, main.right h2, main.right h3, main.right h4")
    .map((h) => +h.tagName.slice(1));
  let hSkip = [];
  for (let i = 1; i < headsSeq.length; i++)
    if (headsSeq[i] - headsSeq[i - 1] > 1) hSkip.push(headsSeq[i - 1] + "→" + headsSeq[i]);

  const tables = $("main.right table").map((t) => ({
    cols: (t.querySelector("tr") ? t.querySelector("tr").children.length : 0),
    rows: t.querySelectorAll("tbody tr").length,
    ragged: new Set($("tbody tr", t).map((r) => r.children.length)).size > 1,
  }));

  const segs = $("article.seg").map((c) => {
    const body = c.querySelector(".seg-body");
    return {
      id: c.dataset.seg || null,
      start: parseFloat(c.dataset.segStart),
      chars: T(body).length,
      paras: body ? body.querySelectorAll(":scope > p").length : 0,
      figs: body ? body.querySelectorAll("figure.media").length : 0,
      blocks: body ? body.querySelectorAll(":scope > p, :scope > figure, :scope > .flow, :scope > .mindmap, :scope > .svg-block, :scope > .tbl-wrap").length : 0,
      insights: body ? body.querySelectorAll("details.insight, div.insight").length : 0,
      title: T(c.querySelector(".seg-head h3")),
    };
  });

  let declared = 0;
  const dsegs = Array.isArray(D.segments) ? D.segments : [];
  const seen = new Set();
  dsegs.forEach((s) => {
    if (s && s.keyframe) { declared++; seen.add(s.keyframe); }
    (Array.isArray(s && s.caption) ? s.caption : []).forEach((b) => {
      if (b && b.type === "image" && b.src) { declared++; seen.add(b.src); }
    });
  });
  const imgs = $("main.right figure.media img").map((i) => {
    const r = i.getBoundingClientRect();
    const fig = i.closest("figure");
    return {
      src: i.getAttribute("src"),
      w: Math.round(r.width), h: Math.round(r.height),
      nw: i.naturalWidth, nh: i.naturalHeight,
      cap: T(fig ? fig.querySelector("figcaption") : null),
      alt: i.getAttribute("alt") || "",
      wide: !!(fig && fig.classList.contains("wide")),
    };
  });

  return {
    meta: meta,
    typography: {
      paras: paras.length,
      longest_para: paras.length ? Math.max.apply(null, paras) : 0,
      paras_over_400: paras.filter((n) => n > 400).length,
      paras_under_20: paras.filter((n) => n < 20).length,
      heads: headsSeq.length, head_skips: hSkip,
      tables: tables, lists: $("main.right ul, main.right ol").length,
      flow: $("main.right .flow").length,
      mindmap: $("main.right .mindmap").length,
      svg: $("main.right .svg-block").length,
      code: $("main.right code").length,
      strong: $("main.right strong").length,
      tl_grid: $("main.right .tl-grid").length,
    },
    segments: segs,
    images: {
      declared: declared, distinct_declared: seen.size, rendered: imgs.length,
      broken: $("figure.media-broken").length,
      no_caption: imgs.filter((i) => !i.cap).length,
      no_alt: imgs.filter((i) => !i.alt).length,
      natural_zero: imgs.filter((i) => !i.nw).length,
      min_render_w: imgs.length ? Math.min.apply(null, imgs.map((i) => i.w)) : 0,
      max_render_w: imgs.length ? Math.max.apply(null, imgs.map((i) => i.w)) : 0,
      upscaled: imgs.filter((i) => i.nw && i.w > i.nw * 1.35).length,
      items: imgs,
    },
    cites: {
      clickable: $("sup.cite[data-src]").length,
      broken: $("span.cite-broken").length,
      targets: $(".src[id^='src-S']").length,
    },
    collapse: {
      seg_details: $("details.seg-details").length,
      insight_details: $("details.insight").length,
      insight_plain: $("div.insight").length,
      hero_lede: $("details.hero-lede").length,
      toggle_all: $("button.toc-toggle-all").length,
    },
    toc: {
      items: $(".toc-item:not(.toc-anchor)").length,
      anchors: $(".toc-item.toc-anchor").length,
      labels: $(".toc-item").map((b) => T(b)),
    },
    layout: {
      page_h: document.documentElement.scrollHeight,
      hscroll_px: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
      player: (() => { const v = document.getElementById("player");
        if (!v) return null; const r = v.getBoundingClientRect();
        return {w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top)}; })(),
      h1: T(document.querySelector("main.right .hero h1")),
      kicker: T(document.querySelector("main.right .hero .kicker")),
      sec_insights: !!document.getElementById("sec-insights"),
      sec_sources: !!document.getElementById("sec-sources"),
      footer: T(document.querySelector(".footer")),
    },
  };
}
"""


def _wait_ready(page):
    page.wait_for_selector("article.seg, p.notice", timeout=20000)
    page.wait_for_timeout(600)
    try:
        page.wait_for_function(
            "() => { const v = document.getElementById('player'); return v && v.readyState >= 1; }",
            timeout=20000,
        )
    except Exception:
        pass
    page.evaluate(
        "async () => {\n        const h = document.documentElement.scrollHeight;\n        for (let y = 0; y < h; y += window.innerHeight * 0.9) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 60)); }\n        window.scrollTo(0, 0);\n        document.querySelectorAll('img[loading=lazy]').forEach(i => i.loading = 'eager');\n        await new Promise(r => setTimeout(r, 400));\n    }"
    )


def probe_cites(page, limit=SAMPLE_CITES):
    page.evaluate(
        "() => { const d = document.querySelector('details.hero-lede'); if (d) d.open = true; }"
    )
    page.wait_for_timeout(400)
    page.evaluate(
        "() => { const v = document.getElementById('player'); if (v) { try { v.pause(); } catch (e) {} } }"
    )
    n = page.locator("sup.cite[data-src]").count()
    out = {"n": n, "clicked": 0, "jumped": 0, "flashed": 0, "misses": []}
    step = max(1, n // limit) if n else 1
    for i in range(0, n, step):
        if out["clicked"] >= limit:
            break
        c = page.locator("sup.cite[data-src]").nth(i)
        sid = c.get_attribute("data-src")
        try:
            c.scroll_into_view_if_needed()
            c.click(timeout=4000)
        except Exception as e:
            out["misses"].append(f"{sid}:click failed {type(e).__name__}")
            continue
        out["clicked"] += 1
        r = page.evaluate(
            "async (sid) => {\n            const t = document.getElementById('src-' + sid);\n            if (!t) return {found: false};\n            let flash = false, b = null;\n            for (let i = 0; i < 50; i++) {\n                await new Promise(r => setTimeout(r, 100));\n                flash = flash || t.classList.contains('flash');\n                b = t.getBoundingClientRect();\n                if (b.top >= -20 && b.top < window.innerHeight - 40)\n                    return {found: true, in_view: true, ms: i * 100, flash: flash};\n            }\n            return {found: true, in_view: false, ms: 5000, flash: flash,\n                    top: Math.round(b.top)};\n        }",
            sid,
        )
        if r.get("in_view"):
            out["jumped"] += 1
        else:
            out["misses"].append(
                f"{sid}:"
                + (
                    "no target"
                    if not r.get("found")
                    else f"not scrolled (top={r.get('top')}px)"
                )
            )
        if r.get("flash"):
            out["flashed"] += 1
    return out


def probe_lightbox(page, limit=SAMPLE_IMGS):
    n = page.locator("main.right figure.media img").count()
    out = {
        "n": n,
        "clicked": 0,
        "opened": 0,
        "decoded": 0,
        "with_caption": 0,
        "esc_closes": None,
        "misses": [],
    }
    step = max(1, n // limit) if n else 1
    for i in range(0, n, step):
        if out["clicked"] >= limit:
            break
        img = page.locator("main.right figure.media img").nth(i)
        try:
            img.scroll_into_view_if_needed()
            src = img.get_attribute("src")
            img.click(timeout=4000)
        except Exception as e:
            out["misses"].append(f"#{i}:click failed {type(e).__name__}")
            continue
        out["clicked"] += 1
        page.wait_for_timeout(250)
        r = page.evaluate(
            "() => {\n            const lb = document.querySelector('.lightbox');\n            if (!lb) return {open: false};\n            const im = lb.querySelector('img');\n            return {open: !lb.hidden, nw: im ? im.naturalWidth : 0,\n                    alt: im ? (im.getAttribute('alt') || '') : ''};\n        }"
        )
        if r.get("open"):
            out["opened"] += 1
        else:
            out["misses"].append(f"{src}:lightbox not shown")
        if r.get("nw"):
            out["decoded"] += 1
        if (r.get("alt") or "").strip():
            out["with_caption"] += 1
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        closed = page.evaluate(
            "() => { const lb = document.querySelector('.lightbox'); return !lb || lb.hidden; }"
        )
        out["esc_closes"] = (
            closed if out["esc_closes"] is None else out["esc_closes"] and closed
        )
    return out


def _open_state(page):
    return page.evaluate(
        "() => {\n        const ds = Array.from(document.querySelectorAll('details.seg-details, details.insight'));\n        return {n: ds.length, open: ds.filter(d => d.open).length};\n    }"
    )


def probe_collapse(page):
    btn = page.locator("button.toc-toggle-all")
    out = {"button": btn.count(), "before": _open_state(page)}
    if not btn.count():
        return out
    btn.first.click()
    page.wait_for_timeout(200)
    out["after_1"] = _open_state(page)
    btn.first.click()
    page.wait_for_timeout(200)
    out["after_2"] = _open_state(page)
    b, a1, a2 = (out["before"], out["after_1"], out["after_2"])
    out["collapses_all"] = bool(b["n"]) and a1["open"] == 0
    out["expands_all"] = bool(b["n"]) and a2["open"] == a2["n"]
    return out


def probe_toc_seek(page):
    btn = page.locator("button.toc-toggle-all")
    for _ in range(2):
        if not btn.count():
            break
        if _open_state(page)["open"] == 0:
            break
        btn.first.click()
        page.wait_for_timeout(150)
    items = page.locator(".toc-item:not(.toc-anchor)")
    n = items.count()
    rows = []
    for i in range(n):
        it = items.nth(i)
        seg_id = it.get_attribute("data-toc-seg")
        try:
            it.click(timeout=4000)
        except Exception as e:
            rows.append(
                {"i": i, "seg": seg_id, "error": f"click failed {type(e).__name__}"}
            )
            continue
        page.wait_for_timeout(320)
        r = page.evaluate(
            "(sid) => {\n            const v = document.getElementById('player');\n            const card = document.getElementById('seg-' + sid);\n            const d = card ? card.querySelector('details') : null;\n            const b = card ? card.getBoundingClientRect() : null;\n            return {t: v ? v.currentTime : null,\n                    expect: card ? parseFloat(card.dataset.segStart) : null,\n                    opened: d ? !!d.open : null,\n                    in_view: b ? (b.top < window.innerHeight * 0.9 && b.bottom > 0) : false,\n                    active: card ? card.classList.contains('is-active') : false};\n        }",
            seg_id,
        )
        got, exp = (r.get("t"), r.get("expect"))
        r.update(
            {
                "i": i,
                "seg": seg_id,
                "seek_ok": got is not None
                and exp is not None
                and (abs(got - exp) <= SEEK_TOL),
            }
        )
        rows.append(r)
    return {
        "n": n,
        "rows": rows,
        "seek_ok": sum((1 for r in rows if r.get("seek_ok"))),
        "open_ok": sum((1 for r in rows if r.get("opened"))),
        "view_ok": sum((1 for r in rows if r.get("in_view"))),
        "monotonic": _monotonic([r.get("expect") for r in rows]),
    }


def _monotonic(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return all((xs[i] < xs[i + 1] for i in range(len(xs) - 1))) if len(xs) > 1 else None


def probe_toc_anchors(page):
    items = page.locator(".toc-item.toc-anchor")
    rows = []
    for i in range(items.count()):
        it = items.nth(i)
        label = (it.inner_text() or "").strip()
        it.click()
        page.wait_for_timeout(600)
        r = page.evaluate(
            "() => {\n            const ids = ['sec-insights', 'sec-sources'];\n            const out = {};\n            ids.forEach(id => { const e = document.getElementById(id);\n                if (e) { const b = e.getBoundingClientRect();\n                         out[id] = b.top > -80 && b.top < window.innerHeight * 0.6; } });\n            return out;\n        }"
        )
        rows.append({"label": label, "in_view": any(r.values()), "detail": r})
    return {
        "n": items.count(),
        "rows": rows,
        "ok": sum((1 for r in rows if r["in_view"])),
    }


def probe_sync(page, k=6):
    starts = page.evaluate(
        "() => Array.from(document.querySelectorAll('article.seg'))\n        .map(c => parseFloat(c.dataset.segStart)).filter(x => isFinite(x)).sort((a,b)=>a-b)"
    )
    if len(starts) < 2:
        return {"n": 0, "rows": [], "toc_ok": 0, "card_ok": 0}
    rows = []
    idxs = sorted(set((round(i * (len(starts) - 1) / max(1, k - 1)) for i in range(k))))
    for j in idxs:
        mid = (
            starts[j]
            + ((starts[j + 1] if j + 1 < len(starts) else starts[j] + 20) - starts[j])
            * 0.5
        )
        r = page.evaluate(
            "async ([t, j]) => {\n            const v = document.getElementById('player');\n            if (!v) return {err: 'no player'};\n            v.currentTime = t;                       // 真的设播放位置\n            v.dispatchEvent(new Event('timeupdate'));  // 只有通知是手动的，见函数注释\n            await new Promise(r => setTimeout(r, 80));\n            const items = Array.from(document.querySelectorAll('.toc-item:not(.toc-anchor)'));\n            const cards = Array.from(document.querySelectorAll('article.seg'))\n                .sort((a, b) => parseFloat(a.dataset.segStart) - parseFloat(b.dataset.segStart));\n            return {\n                t: v.currentTime,\n                toc_active: items.findIndex(e => e.classList.contains('is-active')),\n                card_active: cards.findIndex(e => e.classList.contains('is-active')),\n                want: j,\n            };\n        }",
            [mid, j],
        )
        r.update(
            {"toc_ok": r.get("toc_active") == j, "card_ok": r.get("card_active") == j}
        )
        rows.append(r)
    return {
        "n": len(rows),
        "rows": rows,
        "toc_ok": sum((1 for r in rows if r.get("toc_ok"))),
        "card_ok": sum((1 for r in rows if r.get("card_ok"))),
    }


def measure_images(page):
    page.evaluate(
        "() => { document.querySelectorAll('details').forEach(d => d.open = true);\n                             document.querySelectorAll('img').forEach(i => i.loading = 'eager'); }"
    )
    page.wait_for_timeout(200)
    return page.evaluate(
        "async () => {\n        const h0 = document.documentElement.scrollHeight;\n        for (let y = 0; y < h0; y += window.innerHeight * 0.8) {\n            window.scrollTo(0, y); await new Promise(r => setTimeout(r, 70)); }\n        window.scrollTo(0, 0);\n        const all = Array.from(document.querySelectorAll('main.right figure.media img'));\n        // 等解码，最多 8 秒：complete 为真即可，decode() 在部分 jpg 上会 reject\n        const t0 = Date.now();\n        while (Date.now() - t0 < 8000 && all.some(i => !i.complete))\n            await new Promise(r => setTimeout(r, 150));\n        await new Promise(r => setTimeout(r, 250));\n        const T = (e) => (e ? (e.textContent || '').trim() : '');\n        const imgs = all.map((i) => {\n            const r = i.getBoundingClientRect(), fig = i.closest('figure.media');\n            return {src: i.getAttribute('src') || '', w: Math.round(r.width), h: Math.round(r.height),\n                    nw: i.naturalWidth, nh: i.naturalHeight, complete: i.complete,\n                    cap: T(fig && fig.querySelector('figcaption')), alt: i.getAttribute('alt') || ''};\n        });\n        const seen = new Set(imgs.map((i) => i.src));\n        return {\n            rendered: imgs.length, broken: document.querySelectorAll('figure.media-broken').length,\n            no_caption: imgs.filter((i) => !i.cap).length,\n            no_alt: imgs.filter((i) => !i.alt).length,\n            natural_zero: imgs.filter((i) => !i.nw).length,\n            incomplete: imgs.filter((i) => !i.complete).length,\n            min_render_w: imgs.length ? Math.min.apply(null, imgs.map((i) => i.w)) : 0,\n            max_render_w: imgs.length ? Math.max.apply(null, imgs.map((i) => i.w)) : 0,\n            min_natural_w: imgs.length ? Math.min.apply(null, imgs.map((i) => i.nw)) : 0,\n            upscaled: imgs.filter((i) => i.nw && i.w > i.nw * 1.35).length,\n            distinct_rendered: seen.size, items: imgs,\n        };\n    }"
    )


def probe_one(pw, report_dir: str, label: str, outdir: str, shots: bool = True) -> dict:
    console = {"warn": [], "error": []}
    with _Server(report_dir) as url:
        browser = pw.chromium.launch(
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--mute-audio",
                "--no-sandbox",
            ]
        )
        try:
            page = browser.new_page(
                viewport={"width": DESKTOP[0], "height": DESKTOP[1]},
                device_scale_factor=1,
                reduced_motion="reduce",
            )
            page.on(
                "console",
                lambda m: (
                    console.setdefault(m.type, []).append(m.text[:400])
                    if m.type in ("warning", "error")
                    else None
                ),
            )
            page.on("pageerror", lambda e: console["error"].append(str(e)[:400]))
            page.goto(url, wait_until="load", timeout=45000)
            _wait_ready(page)
            res = {
                "label": label,
                "report": os.path.relpath(report_dir),
                "viewport": {"w": DESKTOP[0], "h": DESKTOP[1]},
                "probe_version": 2,
            }
            res.update(page.evaluate(JS_STATIC))
            res["interaction"] = {}
            for name, fn in (
                ("cites", probe_cites),
                ("lightbox", probe_lightbox),
                ("collapse", probe_collapse),
                ("toc_seek", probe_toc_seek),
                ("toc_anchor", probe_toc_anchors),
                ("sync", probe_sync),
            ):
                try:
                    res["interaction"][name] = fn(page)
                except Exception as e:
                    res["interaction"][name] = {"error": f"{type(e).__name__}: {e}"}
            try:
                res["images"].update(measure_images(page))
            except Exception as e:
                res["images"]["measure_error"] = f"{type(e).__name__}: {e}"
            if shots:
                sd = os.path.join(outdir, "probe_shots")
                os.makedirs(sd, exist_ok=True)
                btn = page.locator("button.toc-toggle-all")
                if btn.count() and _open_state(page)["open"] == 0:
                    btn.first.click()
                    page.wait_for_timeout(200)
                page.evaluate("() => window.scrollTo(0, 0)")
                page.wait_for_timeout(200)
                page.screenshot(path=os.path.join(sd, "desktop.png"))
                page.screenshot(path=os.path.join(sd, "full.png"), full_page=True)
                page.set_viewport_size({"width": MOBILE[0], "height": MOBILE[1]})
                page.wait_for_timeout(400)
                res["mobile"] = page.evaluate(
                    "() => ({\n                    hscroll_px: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),\n                    page_h: document.documentElement.scrollHeight,\n                    widest: (() => { let w = 0, tag = null;\n                      document.querySelectorAll('main.right *').forEach(e => {\n                        const r = e.getBoundingClientRect();\n                        if (r.width > w) { w = r.width; tag = e.tagName + '.' + (e.className || ''); } });\n                      return {px: Math.round(w), el: tag}; })(),\n                })"
                )
                page.screenshot(path=os.path.join(sd, "mobile.png"))
                res["shots"] = {
                    k: os.path.relpath(os.path.join(sd, f"{k}.png"), outdir)
                    for k in ("desktop", "full", "mobile")
                }
            res["console"] = {
                "warn": console.get("warning", [])[:20],
                "error": console.get("error", [])[:20],
            }
            res["ok"] = True
            return res
        finally:
            browser.close()


def main():
    ap = argparse.ArgumentParser(
        description="Measure report rendering and interactive behavior with Chromium."
    )
    ap.add_argument(
        "--report",
        required=True,
        help="Input report directory; it is read without modification.",
    )
    ap.add_argument(
        "--out",
        required=True,
        help="Output probe JSON; screenshots are stored in probe_shots/ beside this file.",
    )
    ap.add_argument("--label", default="report")
    ap.add_argument("--no-shots", action="store_true")
    args = ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Install playwright, then run: python -m playwright install chromium")
    output = os.path.abspath(args.out)
    outdir = os.path.dirname(output)
    os.makedirs(outdir, exist_ok=True)
    try:
        with sync_playwright() as pw:
            result = probe_one(
                pw, args.report, args.label, outdir, shots=not args.no_shots
            )
    except Exception as error:
        result = {
            "label": args.label,
            "report": args.report,
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
        }
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(f"Browser probe: ok={result.get('ok')} -> {output}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
