"""Binary report judgments, machine checks, cached rescoring, and score aggregation."""

from __future__ import annotations
import argparse, base64, json, os, random, re, subprocess, sys, time, urllib.request, urllib.error, urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_URL = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
DEFAULT_MODEL = os.environ.get("OMNIDR_JUDGE_MODEL", "gpt-5.6-sol")
_CACHE_DIR = os.path.abspath("results/cache")
_PROBE_PATH = None
_SKIP_VISION = False
_SHOT_LOCK = __import__("threading").Lock()


def _redact_error(error):
    value = str(error)
    return value.replace(API_KEY, "[REDACTED]") if API_KEY else value


RATE_LIMIT_RETRIES = 12
_NO_TEMPERATURE_RE = re.compile("^claude", re.I)
RATE_LIMIT_MAX_WAIT = 60.0


def _is_rate_limit(e):
    code = getattr(e, "code", None)
    if code in (429, 502, 503, 504):
        return True
    return bool(
        re.search("\\b(429|502|503|504)\\b|too many requests|rate.?limit", str(e), re.I)
    )


def _retry_after(e):
    try:
        v = e.headers.get("Retry-After")
        return max(0.0, min(RATE_LIMIT_MAX_WAIT, float(v)))
    except Exception:
        return None


def _chat(
    messages,
    model=DEFAULT_MODEL,
    max_tokens=2200,
    temperature=0.0,
    retries=3,
    rate_retries=RATE_LIMIT_RETRIES,
):
    if not API_KEY or not BASE_URL:
        raise RuntimeError(
            "Set OPENAI_BASE_URL and OPENAI_API_KEY before requesting LLM judgments."
        )
    url = f"{BASE_URL}/chat/completions"
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None and (not _NO_TEMPERATURE_RE.search(model)):
        payload["temperature"] = temperature
    last, rl = (None, 0)
    attempt = 0
    while attempt < retries:
        body = json.dumps(payload).encode()
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode())
            ch = d["choices"][0]
            txt = ch["message"].get("content") or ""
            if not txt.strip():
                fr = ch.get("finish_reason")
                rt = (
                    (d.get("usage") or {}).get("completion_tokens_details") or {}
                ).get("reasoning_tokens")
                raise RuntimeError(
                    f"empty content (finish_reason={fr}, reasoning_tokens={rt}, max_tokens={max_tokens})"
                )
            return txt
        except Exception as e:
            last = e
            if getattr(e, "code", None) in (401, 403):
                detail = ""
                try:
                    detail = e.read()[:200].decode("utf-8", "ignore").replace("\n", " ")
                except Exception:
                    pass
                raise RuntimeError(
                    f"HTTP {e.code}: authentication or model access was rejected."
                ) from e
            if getattr(e, "code", None) == 400 and "temperature" in payload:
                detail = ""
                try:
                    detail = e.read()[:300].decode("utf-8", "ignore")
                except Exception:
                    pass
                if "temperature" in detail:
                    payload.pop("temperature")
                    continue
                last = RuntimeError(
                    f"HTTP 400: request rejected for model {model}: {_redact_error(detail)}"
                )
            if _is_rate_limit(e) and rl < rate_retries:
                rl += 1
                wait = _retry_after(e)
                if wait is None:
                    wait = min(RATE_LIMIT_MAX_WAIT, 4.0 * 2 ** min(rl - 1, 4))
                time.sleep(wait * (0.5 + random.random()))
                continue
            attempt += 1
            time.sleep(2 * attempt * (0.5 + random.random()))
    raise RuntimeError(
        f"gateway call failed after {retries} (+{rl} rate-limit waits): {_redact_error(last)}"
    )


def _parse_json(text):
    text = text.strip()
    m = re.search("```(?:json)?\\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    for pat in ("\\{.*\\}", "\\[.*\\]"):
        mm = re.search(pat, text, re.S)
        if mm:
            try:
                return json.loads(mm.group(0))
            except Exception:
                pass
    return None


def load_report(report_dir):
    out = subprocess.run(
        ["node", os.path.join(HERE, "load_report.mjs"), report_dir],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"load_report.mjs failed: {out.stderr}")
    rep = json.loads(out.stdout)
    if not rep["ok"]:
        raise RuntimeError(f"data.js parse error: {rep['error']}")
    return rep


SEARCH_URL_RE = re.compile(
    "search\\.(bilibili|youtube|google)|[?&]keyword=|/all\\?", re.I
)
SECRET_RE = re.compile(
    "\\b(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|ghp_[A-Za-z0-9]{20,})\\b"
)
BANNED_NARRATION = [
    "研究锚点",
    "我们深挖",
    "交叉验证 ≥2",
    "交叉验证≥2",
    "ASR 已",
    "ASR已",
    "见 TOOLS_USED",
    "见TOOLS_USED",
    "口误已修正",
]
PLACEHOLDER_URL_RE = re.compile(
    "example\\.(?:com|org|net)|example$|replace[-_]me|待填|TODO|localhost|127\\.0\\.0\\.1",
    re.I,
)


def _research(data):
    out = []
    for r in data.get("research", []) or []:
        if not isinstance(r, dict):
            continue
        u = (r.get("url") or "").strip()
        if not u or PLACEHOLDER_URL_RE.search(u):
            continue
        out.append(r)
    return out


def m_count_research_min(data, files, rep, a):
    n = len(_research(data))
    return (n >= a["n"], f"research={n} (need ≥{a['n']})")


def m_count_research_type_min(data, files, rep, a):
    n = sum((1 for r in _research(data) if r.get("type") == a["type"]))
    return (n >= a["n"], f"{a['type']}={n} (need ≥{a['n']})")


def m_no_search_urls(data, files, rep, a):
    bad = [
        r.get("url", "")
        for r in _research(data)
        if SEARCH_URL_RE.search(r.get("url", "") or "")
    ]
    return (len(bad) == 0, "clean" if not bad else f"search-like urls: {bad[:3]}")


def m_no_secrets(data, files, rep, a):
    hit = SECRET_RE.findall(rep["flatText"])
    return (len(hit) == 0, "clean" if not hit else "possible secret leak")


def m_no_process_narration(data, files, rep, a):
    hits = [p for p in BANNED_NARRATION if p in rep["flatText"]]
    return (len(hits) == 0, "clean" if not hits else f"found: {hits}")


def m_cite_closure(data, files, rep, a):
    if not rep["citeIds"]:
        return (False, "正文没有任何 [S#] 引用（空集上的『全部能解析』不算通过）")
    ok_ids = {r.get("id") for r in _research(data) if r.get("id")}
    missing = [c for c in rep["citeIds"] if c not in ok_ids]
    if not missing:
        return (True, f"{len(rep['citeIds'])} 处 [S#] 全部解析到有 URL 的来源")
    naked = [c for c in missing if c in set(rep["researchIds"])]
    return (
        False,
        f"dangling: {missing[:8]}"
        + (f"（其中 {len(naked)} 处指向无 URL/占位来源）" if naked else ""),
    )


def m_segments_valid(data, files, rep, a):
    segs = data.get("segments", []) or []
    bad = [
        s.get("id")
        for s in segs
        if not (
            isinstance(s.get("start"), (int, float))
            and isinstance(s.get("end"), (int, float))
            and (s["start"] < s["end"])
        )
    ]
    return (
        len(segs) >= a.get("min", 1) and (not bad),
        f"segments={len(segs)} bad={bad}",
    )


def m_validate_data(data, files, rep, a):
    ui = os.path.join(HERE, "validate_data.mjs")
    dj = os.path.join(a["_report_dir"], "js", "data.js")
    if not os.path.exists(ui):
        raise RuntimeError(
            "Bundled validate_data.mjs is missing; reinstall the evaluation package."
        )
    r = subprocess.run(["node", ui, dj], capture_output=True, text=True, timeout=60)
    return (r.returncode == 0, (r.stdout + r.stderr)[:300])


def _resolves(report_dir, rel):
    if not rel or not isinstance(rel, str):
        return False
    p = os.path.join(report_dir, rel)
    try:
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except OSError:
        return False


def m_distinct_keyframes(data, files, rep, a):
    root = a["_report_dir"]
    decl = [
        s.get("keyframe") for s in data.get("segments", []) or [] if s.get("keyframe")
    ]
    ks = [k for k in decl if _resolves(root, k)]
    if not ks:
        return (False, f"没有任何能解析到文件的关键帧（声明 {len(decl)} 个）")
    dup = len(ks) - len(set(ks))
    return (dup == 0, f"{len(ks)}/{len(decl)} keyframes 落地, {dup} dup")


def m_anchors_used(data, files, rep, a):
    anchor_files = [
        f["path"]
        for f in files
        if f["path"].startswith("anchors/")
        and f["path"].lower().endswith((".jpg", ".jpeg", ".png"))
    ]
    if not anchor_files:
        return (False, "no anchors/*.jpg present")
    txt = json.dumps(data, ensure_ascii=False)
    used = [p for p in anchor_files if os.path.basename(p) in txt or p in txt]
    return (
        len(used) >= a.get("n", 1),
        f"anchor imgs={len(anchor_files)} referenced={len(used)}",
    )


def m_each_anchor_min_sources(data, files, rep, a):
    anchors = data.get("anchors", []) or []
    if not anchors:
        return (False, "no anchors")
    n = a.get("n", 3)
    cnt = defaultdict(int)
    for r in _research(data):
        if r.get("anchorId"):
            cnt[r["anchorId"]] += 1
    research_anchors = [ac for ac in anchors if cnt[ac["id"]] >= 1]
    if not research_anchors:
        return (False, f"no research anchors (all {len(anchors)} decorative)")
    weak = [ac["id"] for ac in research_anchors if cnt[ac["id"]] < n]
    return (
        len(weak) == 0,
        f"research_anchors={len(research_anchors)} weak(<{n})={weak}",
    )


def m_research_media_downloaded(data, files, rep, a):
    have = sum((1 for r in _research(data) if r.get("media") or r.get("thumb")))
    assets = sum((1 for f in files if f["path"].startswith("_assets/")))
    ok = have >= a.get("n", 1) or assets >= a.get("n", 1)
    return (ok, f"research w/ local media={have}, _assets files={assets}")


def _blocks(data):
    for s in data.get("segments", []) or []:
        for b in s.get("caption", []) or []:
            if isinstance(b, dict):
                yield b


def _insights(data):
    for i in data.get("insights", []) or []:
        if isinstance(i, dict):
            yield i
    for s in data.get("segments", []) or []:
        for i in s.get("insights", []) or []:
            if isinstance(i, dict):
                yield i


def m_block_type_min(data, files, rep, a):
    types = set(a.get("types", []))
    n = a.get("n", 1)
    c = sum((1 for b in _blocks(data) if b.get("type") in types))
    return (c >= n, f"blocks{sorted(types)}={c} (need ≥{n})")


def m_insight_kind_min(data, files, rep, a):
    kinds = set(a.get("kinds", []))
    n = a.get("n", 1)
    c = sum((1 for i in _insights(data) if i.get("kind") in kinds))
    return (c >= n, f"insights{sorted(kinds)}={c} (need ≥{n})")


def m_table_min(data, files, rep, a):
    n = a.get("n", 1)
    c = sum(
        (
            1
            for i in _insights(data)
            if isinstance(i.get("table"), dict) and (i["table"].get("rows") or [])
        )
    )
    return (c >= n, f"insight tables={c} (need ≥{n})")


def m_distinct_domains_min(data, files, rep, a):
    doms = set()
    for r in _research(data):
        h = urllib.parse.urlparse(r.get("url") or "").netloc.lower()
        if h.startswith("www."):
            h = h[4:]
        if h:
            doms.add(h)
    n = a.get("n", 5)
    return (len(doms) >= n, f"distinct domains={len(doms)} (need ≥{n})")


def m_research_types_min(data, files, rep, a):
    ts = sorted({r.get("type") for r in _research(data) if r.get("type")})
    n = a.get("n", 3)
    return (len(ts) >= n, f"types={ts} (need ≥{n} kinds)")


_EVAL_CTX = {}


def m_research_recency(data, files, rep, a):
    win = recency_window(_EVAL_CTX.get("video_published"), _EVAL_CTX.get("today"))
    if win is None:
        return (None, "无 video_published 或视频够新（<0.5 年），时效题不适用（N/A）")
    label, since = win
    since_year, n = (since.year, a.get("n", 3))
    c = 0
    for r in _research(data):
        yrs = [
            int(y)
            for y in re.findall("(?:19|20)\\d{2}", json.dumps(r, ensure_ascii=False))
        ]
        if any((since_year <= y <= 2100 for y in yrs)):
            c += 1
    return (c >= n, f"{label}（来源年份 ≥{since_year}）：{c} 个（need ≥{n}）")


def m_image_reuse_max(data, files, rep, a):
    mx = a.get("max", 1)
    root = a["_report_dir"]
    uses = defaultdict(int)
    for s in data.get("segments", []) or []:
        if _resolves(root, s.get("keyframe")):
            uses[s["keyframe"]] += 1
    for b in _blocks(data):
        if (
            b.get("type") == "image"
            and _resolves(root, b.get("src"))
            and (not b.get("wide"))
        ):
            uses[b["src"]] += 1
    if not uses:
        return (False, "报告没有任何图片（无图可谈『不重复』）")
    over = {k: v for k, v in uses.items() if v > mx}
    return (
        len(over) == 0,
        "no image shown >%d×" % mx if not over else f"reused: {list(over.items())[:3]}",
    )


def m_segments_have_media(data, files, rep, a):
    root = a["_report_dir"]
    segs = data.get("segments", []) or []
    if not segs:
        return (False, "no segments")
    bad = [
        s.get("id")
        for s in segs
        if not _resolves(root, s.get("keyframe"))
        and (
            not any(
                (
                    _resolves(root, b.get("src"))
                    for b in s.get("caption") or []
                    if isinstance(b, dict) and b.get("type") == "image"
                )
            )
        )
    ]
    frac = 1 - len(bad) / len(segs)
    return (
        frac >= a.get("min_frac", 1.0),
        f"{len(segs) - len(bad)}/{len(segs)} segments have media, missing={bad[:4]}",
    )


GENERIC_TITLE_RE = re.compile(
    "^\\s*(?:第?\\s*\\d+\\s*(?:段|部分|节|章)?|片段\\s*\\d+|seg(?:ment)?\\s*\\d+|\\d{1,2}:\\d{2}(?:\\s*[-–~]\\s*\\d{1,2}:\\d{2})?|(?:段落|片段|章节|第)?\\s*\\d*\\s*(?:段|部分|节|章)?\\s*标题\\s*\\d*|(?:TODO|待填|占位|replace[-_ ]?me|untitled|placeholder).*)\\s*$",
    re.I,
)


def m_segment_titles_descriptive(data, files, rep, a):
    segs = data.get("segments", []) or []
    bad = [
        s.get("id")
        for s in segs
        if not s.get("title")
        or GENERIC_TITLE_RE.match(str(s["title"]))
        or len(str(s["title"]).strip()) < 4
    ]
    return (len(segs) > 0 and (not bad), f"{len(segs)} titles, generic/too-short={bad}")


def m_segments_not_uniform(data, files, rep, a):
    segs = [
        s
        for s in data.get("segments") or []
        if isinstance(s.get("start"), (int, float))
        and isinstance(s.get("end"), (int, float))
    ]
    if len(segs) < 3:
        return (False, f"only {len(segs)} timed segments")
    lens = [s["end"] - s["start"] for s in segs]
    mean = sum(lens) / len(lens)
    cv = (
        (sum(((l - mean) ** 2 for l in lens)) / len(lens)) ** 0.5 / mean
        if mean
        else 0.0
    )
    need = a.get("min_cv", 0.15)
    return (
        cv >= need,
        f"segment-length cv={cv:.3f} (need ≥{need}), lens={[round(l) for l in lens]}",
    )


def m_images_resolve(data, files, rep, a):
    root, min_w = (a["_report_dir"], a.get("min_w", 320))
    srcs = {s["keyframe"] for s in data.get("segments") or [] if s.get("keyframe")}
    srcs |= {
        b["src"] for b in _blocks(data) if b.get("type") == "image" and b.get("src")
    }
    if not srcs:
        return (False, "no images referenced")
    missing, small = ([], [])
    for s in sorted(srcs):
        p = os.path.join(root, s)
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            missing.append(s)
            continue
        try:
            from PIL import Image

            with Image.open(p) as im:
                if im.width < min_w:
                    small.append(f"{s}({im.width}px)")
        except ImportError:
            pass
        except Exception:
            missing.append(s)
    ok = not missing and (not small)
    return (
        ok,
        (
            f"{len(srcs)} images all resolve, ≥{min_w}px"
            if ok
            else f"missing/broken={missing[:3]} tooSmall={small[:3]}"
        ),
    )


def m_video_playable(data, files, rep, a):
    v = data.get("video") or {}
    src = (v.get("src") or "").strip()
    if not src:
        return (False, "data.video.src 为空，页面没有可播的视频")
    if not _resolves(a["_report_dir"], src):
        return (False, f"video.src={src} 在报告目录里不存在")
    dur = v.get("duration")
    if not isinstance(dur, (int, float)) or dur <= 0:
        return (False, f"video.duration={dur!r} 非法")
    ends = [
        s.get("end")
        for s in data.get("segments") or []
        if isinstance(s.get("end"), (int, float))
    ]
    if ends and max(ends) > dur * 1.05:
        return (False, f"分段最晚 end={max(ends)} 超出 duration={dur}，时间轴对不上")
    return (True, f"{src} 落地，duration={dur}")


def m_cite_reverse_closure(data, files, rep, a):
    rs = _research(data)
    if not rs:
        return (False, "报告没有任何来源条目")
    cited = set(rep["citeIds"])
    orphan = [r.get("id") for r in rs if r.get("id") not in cited]
    share = len(orphan) / len(rs)
    mx = a.get("max_share", 0.15)
    return (
        share <= mx,
        f"{len(rs)} 条来源里 {len(orphan)} 条正文从未引用（{share:.0%}，上限 {mx:.0%}）"
        + (f"，如 {orphan[:6]}" if orphan else ""),
    )


_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def m_assets_unused_max(data, files, rep, a):
    root = a["_report_dir"]
    dirs = tuple(a.get("dirs", ("img/", "anchors/")))
    on_disk = [
        f["path"]
        for f in files
        if f["path"].startswith(dirs) and f["path"].lower().endswith(_IMG_EXT)
    ]
    if not on_disk:
        return (False, f"{list(dirs)} 下没有任何图片素材")
    blob = json.dumps(data, ensure_ascii=False)
    unused = [p for p in on_disk if p not in blob and os.path.basename(p) not in blob]
    share = len(unused) / len(on_disk)
    mx = a.get("max_share", 0.1)
    return (
        share <= mx,
        f"{len(on_disk)} 张素材里 {len(unused)} 张正文未引用（{share:.0%}，上限 {mx:.0%}）"
        + (f"，如 {unused[:5]}" if unused else ""),
    )


CAP_TS_RE = re.compile("(?<!\\d)(\\d{1,2}):([0-5]\\d)(?!\\d)")


def m_imgcap_time_in_segment(data, files, rep, a):
    segs = data.get("segments") or []
    tol = a.get("tol", 3)
    checked, bad = (0, [])
    for s in segs:
        st, en = (s.get("start"), s.get("end"))
        if not (isinstance(st, (int, float)) and isinstance(en, (int, float))):
            continue
        for b in s.get("caption") or []:
            if not (isinstance(b, dict) and b.get("type") == "image"):
                continue
            for mm, ss in CAP_TS_RE.findall(str(b.get("cap") or "")):
                t = int(mm) * 60 + int(ss)
                checked += 1
                if not st - tol <= t <= en + tol:
                    bad.append(f"{s.get('id')}:{mm}:{ss}(段界{int(st)}-{int(en)})")
    if not checked:
        return (False, "图注里没有任何帧时间戳可核（配图无法定位回视频）")
    return (
        not bad,
        (
            f"{checked} 处图注时间戳都落在本段区间内"
            if not bad
            else f"{len(bad)}/{checked} 处越界：{bad[:4]}"
        ),
    )


NARRATIVE_RE = re.compile(
    "^\\[(?:summary|segment\\.[^.]+\\.(?:text|flow)|(?:segment\\.[^.]+\\.)?insight\\.[^\\]]+)\\]"
)


def m_external_beyond_recital(data, files, rep, a):
    ok_ids = {r.get("id") for r in _research(data) if r.get("id")}
    blocks = [l for l in rep["flatText"].split("\n") if NARRATIVE_RE.match(l)]
    if not blocks:
        return (False, "报告没有任何叙述块")
    sourced = [l for l in blocks if any((f"[{i}]" in l for i in ok_ids))]
    share = len(sourced) / len(blocks)
    mn = a.get("min_share", 0.25)
    return (
        share >= mn,
        f"{len(sourced)}/{len(blocks)} 个叙述块带可解析的外部出处（{share:.0%}，门槛 {mn:.0%}）",
    )


ABSOLUTE_RE = re.compile(
    "没有任何一|没有一(?:个|条|处|例|份)|无一(?:例外|不|是)|从未有(?:过)?任何|全部(?:都)?(?:未|没有|不)|一律(?:未|没有|不)|均(?:未|无)(?!详|载)|所有[^，。；]{0,12}(?:都未|都没有|均未)|概不|绝无"
)


def m_no_unsourced_absolutes(data, files, rep, a):
    mx = a.get("max", 1)
    hits, n_text = ([], 0)
    for b in _blocks(data):
        if b.get("type") != "text":
            continue
        txt = str(b.get("text") or "")
        naked = re.sub("[「『“][^」』”]*[」』”]", "", txt)
        if naked.strip():
            n_text += 1
        for m in ABSOLUTE_RE.finditer(naked):
            sent = naked[max(0, m.start() - 40) : m.end() + 40]
            if "[S" not in sent:
                hits.append(sent.strip()[:60])
    if not n_text:
        return (False, "报告没有任何正文叙述，排他性断言的引用纪律无从查证")
    return (
        len(hits) <= mx,
        f"排他性断言无引用 {len(hits)} 处（上限 {mx}）"
        + (f"：{hits[:3]}" if hits else "，clean"),
    )


def m_html_contains(data, files, rep, a):
    txt = ""
    for rel in ("index.html", "js/app.js", "js/link.js"):
        p = os.path.join(a["_report_dir"], rel)
        if os.path.exists(p):
            txt += open(p, encoding="utf-8", errors="ignore").read()
    missing = [p for p in a.get("patterns", []) if p not in txt]
    return (
        not missing,
        "all patterns present" if not missing else f"missing: {missing}",
    )


def _seg_at(data, t):
    for s in data.get("segments") or []:
        st, en = (s.get("start"), s.get("end"))
        if (
            isinstance(st, (int, float))
            and isinstance(en, (int, float))
            and (st <= t < en)
        ):
            return s
    return None


def _seg_imgs(seg):
    out = [
        b["src"]
        for b in seg.get("caption") or []
        if isinstance(b, dict) and b.get("type") == "image" and b.get("src")
    ]
    if seg.get("keyframe"):
        out.append(seg["keyframe"])
    return out


def m_seg_at_has_media(data, files, rep, a):
    s = _seg_at(data, a["at"])
    if not s:
        return (False, f"{a['at']}s 无分段覆盖")
    root = a["_report_dir"]
    ok = any((_resolves(root, p) for p in _seg_imgs(s)))
    return (ok, f"{s.get('id')} 图 {len(_seg_imgs(s))} 张，落地={ok}")


def m_seg_at_images_resolve(data, files, rep, a):
    s = _seg_at(data, a["at"])
    if not s:
        return (False, f"{a['at']}s 无分段覆盖")
    root, min_w = (a["_report_dir"], a.get("min_w", 320))
    srcs = _seg_imgs(s)
    if not srcs:
        return (False, f"{s.get('id')} 一张图都没有")
    for rel in srcs:
        p = os.path.join(root, rel)
        if not (os.path.isfile(p) and os.path.getsize(p) > 0):
            return (False, f"{s.get('id')} 缺 {rel}")
        try:
            from PIL import Image

            with Image.open(p) as im:
                if im.width < min_w:
                    return (False, f"{s.get('id')} {rel} 仅 {im.width}px")
        except ImportError:
            pass
        except Exception:
            return (False, f"{s.get('id')} 坏图 {rel}")
    return (True, f"{s.get('id')} {len(srcs)} 张图都 ≥{min_w}px")


def m_seg_at_imgcap_in_segment(data, files, rep, a):
    s = _seg_at(data, a["at"])
    if not s:
        return (False, f"{a['at']}s 无分段覆盖")
    st, en, tol = (s.get("start"), s.get("end"), a.get("tol", 3))
    checked, bad = (0, [])
    for b in s.get("caption") or []:
        if not (isinstance(b, dict) and b.get("type") == "image"):
            continue
        for mm, ss in CAP_TS_RE.findall(str(b.get("cap") or "")):
            checked += 1
            t = int(mm) * 60 + int(ss)
            if not st - tol <= t <= en + tol:
                bad.append(f"{mm}:{ss}")
    if not checked:
        return (False, f"{s.get('id')} 图注无帧时间戳")
    return (
        not bad,
        (
            f"{s.get('id')} {checked} 处都在段内"
            if not bad
            else f"{s.get('id')} 越界 {bad[:3]}（段界 {int(st)}-{int(en)}）"
        ),
    )


def m_media_dir_unused_max(data, files, rep, a):
    root = a["_report_dir"]
    refd = set()
    for s in data.get("segments") or []:
        for rel in _seg_imgs(s):
            refd.add(os.path.normpath(rel))
    for an in data.get("anchors") or []:
        if an.get("frame"):
            refd.add(os.path.normpath(an["frame"]))
    v = data.get("video") or {}
    for k in ("poster", "src"):
        if v.get(k):
            refd.add(os.path.normpath(str(v[k])))
    unused, total = ([], 0)
    for d in a.get("dirs", ["img"]):
        dp = os.path.join(root, d)
        if not os.path.isdir(dp):
            continue
        for fn in sorted(os.listdir(dp)):
            fp = os.path.join(dp, fn)
            if not os.path.isfile(fp):
                continue
            total += 1
            if os.path.normpath(f"{d}/{fn}") not in refd:
                unused.append(f"{d}/{fn}")
    need = int(a.get("min_files", 1))
    if total < need:
        return (
            False,
            f"{a.get('dirs', ['img'])} 下只有 {total} 个素材文件（至少要 {need} 个）",
        )
    cap = a.get("max", 0)
    return (
        len(unused) <= cap,
        (
            f"闲置 {len(unused)} 个（上限 {cap}）：{unused[:4]}"
            if unused
            else f"{total} 个素材全部被正文引用"
        ),
    )


_PROBE_CACHE = {}
_PROBE_WARNED = set()


def _probe(a):
    root = os.path.abspath(a["_report_dir"])
    p = _PROBE_PATH or os.path.join(_CACHE_DIR, "render_probe.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        if p not in _PROBE_WARNED:
            _PROBE_WARNED.add(p)
            print(
                f"[probe] Missing {p}; run the browser probe and provide --probe <render_probe.json>.",
                file=sys.stderr,
            )
        return (None, f"缺 {os.path.relpath(p)}（先跑 render_probe.py）")
    hit = _PROBE_CACHE.get(p)
    if not hit or hit[0] != mt:
        try:
            hit = (mt, json.load(open(p, encoding="utf-8")))
        except Exception as e:
            return (None, f"{os.path.relpath(p)} 读不出来：{e}")
        _PROBE_CACHE[p] = hit
    pr = hit[1]
    if not pr.get("ok"):
        return (None, f"探针本身失败：{pr.get('error')}")
    return (pr, "")


def _dig(d, path, default=None):
    for k in path.split("."):
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d


PROBE_FRAC = {
    "toc_seek": (
        "interaction.toc_seek.seek_ok",
        "interaction.toc_seek.n",
        "时间轴条目点击后播放器跳到该段起点",
    ),
    "toc_expand": (
        "interaction.toc_seek.open_ok",
        "interaction.toc_seek.n",
        "点击折叠中的条目会自动展开该段",
    ),
    "toc_view": (
        "interaction.toc_seek.view_ok",
        "interaction.toc_seek.n",
        "点击后目标段滚入视口",
    ),
    "toc_anchor": (
        "interaction.toc_anchor.ok",
        "interaction.toc_anchor.n",
        "洞察/来源锚点条目点击后到位",
    ),
    "cite_jump": (
        "interaction.cites.jumped",
        "interaction.cites.clicked",
        "点 [S#] 跳到对应来源条目",
    ),
    "cite_flash": (
        "interaction.cites.flashed",
        "interaction.cites.clicked",
        "跳达后来源条目高亮提示落点",
    ),
    "lightbox_open": (
        "interaction.lightbox.opened",
        "interaction.lightbox.clicked",
        "点图打开灯箱",
    ),
    "lightbox_decode": (
        "interaction.lightbox.decoded",
        "interaction.lightbox.clicked",
        "灯箱里的大图真的解码出来",
    ),
    "lightbox_caption": (
        "interaction.lightbox.with_caption",
        "interaction.lightbox.clicked",
        "灯箱大图带图注",
    ),
    "sync_toc": (
        "interaction.sync.toc_ok",
        "interaction.sync.n",
        "播放位置变化时时间轴高亮跟随",
    ),
    "sync_card": (
        "interaction.sync.card_ok",
        "interaction.sync.n",
        "播放位置变化时正文卡片高亮跟随",
    ),
    "images_rendered": (
        "images.rendered",
        "images.declared",
        "声明的图片真的渲染出来（未被去重丢弃）",
    ),
}


def m_probe_frac(data, files, rep, a):
    pr, why = _probe(a)
    if pr is None:
        return (False, why)
    key = a["metric"]
    if key in PROBE_FRAC:
        np_, dp_, name = PROBE_FRAC[key]
        num, den = (_dig(pr, np_), _dig(pr, dp_))
    elif key in ("seg_media", "seg_blocks"):
        segs = pr.get("segments") or []
        need = int(a.get("blocks", 3))
        den = len(segs)
        num = (
            sum((1 for s in segs if (s.get("figs") or 0) > 0))
            if key == "seg_media"
            else sum((1 for s in segs if (s.get("blocks") or 0) >= need))
        )
        name = "每段配了图" if key == "seg_media" else f"每段正文有 ≥{need} 个块"
    elif key in ("img_captioned", "img_alt", "img_decoded"):
        im = pr.get("images") or {}
        den = im.get("rendered")
        miss = {
            "img_captioned": "no_caption",
            "img_alt": "no_alt",
            "img_decoded": "natural_zero",
        }[key]
        num = den - (im.get(miss) or 0) if isinstance(den, int) else None
        name = {
            "img_captioned": "图都有图注",
            "img_alt": "图都有 alt 文本",
            "img_decoded": "图都解码成功（不是破图）",
        }[key]
    else:
        return (None, f"未知比例指标 {key}")
    if not isinstance(num, (int, float)) or not isinstance(den, (int, float)):
        return (False, f"{name}：探针没量到（num={num} den={den}）")
    if den <= 0:
        return (False, f"{name}：分母为 0（一次都没发生，不能算通过）")
    frac, need = (num / den, float(a.get("min_frac", 1.0)))
    return (frac >= need, f"{name} {num}/{den}={frac:.2f}（需 ≥{need:g}）")


PROBE_FLAG = {
    "collapse_all": (
        lambda p, a: _dig(p, "interaction.collapse.collapses_all"),
        "点「全部展开/折叠」能把所有折叠块收起",
    ),
    "expand_all": (
        lambda p, a: _dig(p, "interaction.collapse.expands_all"),
        "再点一次能把所有折叠块展开",
    ),
    "lightbox_esc": (
        lambda p, a: _dig(p, "interaction.lightbox.esc_closes"),
        "灯箱能用 Esc 关闭",
    ),
    "toc_order": (
        lambda p, a: _dig(p, "interaction.toc_seek.monotonic"),
        "时间轴条目按时间递增排列",
    ),
    "head_no_skip": (
        lambda p, a: not (_dig(p, "typography.head_skips") or []),
        "标题层级不跳级",
    ),
    "no_broken_cite": (
        lambda p, a: (_dig(p, "cites.broken") or 0) == 0
        and (_dig(p, "cites.clickable") or 0) > 0,
        "页面上没有点不动的失效引用角标",
    ),
    "no_broken_media": (
        lambda p, a: (_dig(p, "images.broken") or 0) == 0
        and (_dig(p, "images.rendered") or 0) > 0,
        "页面上没有加载失败的图片占位",
    ),
    "hero_lede": (
        lambda p, a: (_dig(p, "collapse.hero_lede") or 0) > 0,
        "视频简介是可折叠抽屉（作者写了 summary）",
    ),
    "kicker": (
        lambda p, a: bool((_dig(p, "layout.kicker") or "").strip()),
        "首屏有栏目标签（meta.category）",
    ),
    "h1": (
        lambda p, a: len((_dig(p, "layout.h1") or "").strip()) >= 4,
        "首屏有实质标题",
    ),
    "player_visible": (
        lambda p, a: (_dig(p, "layout.player.h") or 0) >= 120
        and (_dig(p, "layout.player.top") or 999) < 400,
        "播放器在首屏且有可用高度",
    ),
    "sec_insights": (
        lambda p, a: bool(_dig(p, "layout.sec_insights")),
        "有独立的综合洞察小节",
    ),
    "sec_sources": (
        lambda p, a: bool(_dig(p, "layout.sec_sources")),
        "有独立的参考来源小节",
    ),
    "no_hscroll": (
        lambda p, a: (_dig(p, "layout.hscroll_px") or 0) <= 2,
        "桌面宽度下没有横向滚动",
    ),
    "no_hscroll_mobile": (
        lambda p, a: (_dig(p, "mobile.hscroll_px") or 0) <= 2,
        "移动端宽度（390px）下没有横向滚动",
    ),
    "tables_regular": (
        lambda p, a: bool(_dig(p, "typography.tables"))
        and (not any((t.get("ragged") for t in _dig(p, "typography.tables") or []))),
        "表格每行列数一致",
    ),
    "theme_valid": (
        lambda p, a: bool(_dig(p, "meta.theme_valid")),
        "选定的主题在 t01–t20 之内（非法值会被静默退回 t01）",
    ),
    "theme_fits": (
        lambda p, a: _dig(p, "meta.theme") in (a.get("allowed") or []),
        "主题与题材相称",
    ),
    "lang_zh": (
        lambda p, a: str(_dig(p, "meta.lang") or "").lower().startswith("zh"),
        "页面语言标为中文",
    ),
    "page_lang": (
        lambda p, a: str(_dig(p, "meta.lang") or "")
        .lower()
        .startswith(a.get("lang") or "zh"),
        "页面语言标记与本任务一致",
    ),
    "no_console_error": (
        lambda p, a: not (_dig(p, "console.error") or []),
        "渲染过程没有 JS 报错",
    ),
}


def m_probe_flag(data, files, rep, a):
    pr, why = _probe(a)
    if pr is None:
        return (False, why)
    key = a["flag"]
    if key not in PROBE_FLAG:
        return (None, f"未知布尔指标 {key}")
    fn, name = PROBE_FLAG[key]
    try:
        v = fn(pr, a)
    except Exception as e:
        return (False, f"{name}：探针读数异常 {e}")
    if v is None:
        return (False, f"{name}：探针没量到（机制未触发）")
    extra = ""
    if key == "page_lang":
        extra = f"（量到 {_dig(pr, 'meta.lang')!r}，本任务要 {a.get('lang') or 'zh'}）"
    elif key == "theme_fits":
        extra = f"（量到 {_dig(pr, 'meta.theme')}／{_dig(pr, 'meta.category')}，相称集 {a.get('allowed')}）"
    elif key == "no_hscroll_mobile":
        extra = f"（溢出 {_dig(pr, 'mobile.hscroll_px')}px，最宽元素 {_dig(pr, 'mobile.widest.px')}px）"
    elif key == "head_no_skip" and (not v):
        extra = f"（跳级处 {_dig(pr, 'typography.head_skips')}）"
    return (bool(v), f"{name}：{('是' if v else '否')}{extra}")


PROBE_COUNT = {
    "segments": (lambda p, a: len(p.get("segments") or []), "分段数", "段"),
    "toc_items": (lambda p, a: _dig(p, "toc.items"), "时间轴条目数", "条"),
    "cites_clickable": (
        lambda p, a: _dig(p, "cites.clickable"),
        "可点击引用角标数",
        "处",
    ),
    "collapsible_insights": (
        lambda p, a: _dig(p, "collapse.insight_details"),
        "可折叠洞察卡数",
        "张",
    ),
    "plain_insights": (
        lambda p, a: _dig(p, "collapse.insight_plain"),
        "没给标题、折不起来的洞察卡数",
        "张",
    ),
    "images": (lambda p, a: _dig(p, "images.rendered"), "实际渲染出的图片数", "张"),
    "min_render_w": (
        lambda p, a: _dig(p, "images.min_render_w"),
        "最窄一张图的渲染宽度",
        "px",
    ),
    "upscaled": (
        lambda p, a: _dig(p, "images.upscaled"),
        "被放大到超过原始分辨率 1.35 倍（发虚）的图片数",
        "张",
    ),
    "longest_para": (
        lambda p, a: _dig(p, "typography.longest_para"),
        "最长段落字数",
        "字",
    ),
    "paras_over": (
        lambda p, a: (
            sum((1 for _ in [0])) if False else _dig(p, "typography.paras_over_400")
        ),
        "超过 400 字的段落数",
        "段",
    ),
    "paras_tiny": (
        lambda p, a: _dig(p, "typography.paras_under_20"),
        "不足 20 字的碎段数",
        "段",
    ),
    "tables": (
        lambda p, a: sum(
            (
                1
                for t in _dig(p, "typography.tables") or []
                if (t.get("cols") or 0) >= int(a.get("cols", 1))
                and (t.get("rows") or 0) >= int(a.get("rows", 1))
            )
        ),
        "达到列数/行数要求的表格数",
        "个",
    ),
    "block_kinds": (
        lambda p, a: sum(
            (
                1
                for k in ("flow", "mindmap", "svg")
                if (_dig(p, f"typography.{k}") or 0) > 0
            )
        )
        + (1 if _dig(p, "typography.tables") or [] else 0),
        "结构化块的种类数（流程图/脑图/SVG/表格）",
        "种",
    ),
    "lists": (lambda p, a: _dig(p, "typography.lists"), "列表数", "个"),
    "timeliness_grid": (
        lambda p, a: _dig(p, "typography.tl_grid"),
        "「视频原述 vs 最新事实」对照块数",
        "个",
    ),
    "page_screens": (
        lambda p, a: round((_dig(p, "layout.page_h") or 0) / 900, 1),
        "整页高度（首屏数）",
        "屏",
    ),
    "seg_chars_max": (
        lambda p, a: max([s.get("chars") or 0 for s in p.get("segments") or []] or [0]),
        "最长一段的正文字数",
        "字",
    ),
    "seg_chars_min": (
        lambda p, a: min([s.get("chars") or 0 for s in p.get("segments") or []] or [0]),
        "最短一段的正文字数",
        "字",
    ),
}


def m_probe_count(data, files, rep, a):
    pr, why = _probe(a)
    if pr is None:
        return (False, why)
    key = a["metric"]
    if key not in PROBE_COUNT:
        return (None, f"未知计数指标 {key}")
    fn, name, unit = PROBE_COUNT[key]
    v = fn(pr, a)
    if not isinstance(v, (int, float)):
        return (False, f"{name}：探针没量到")
    lo, hi = (a.get("min"), a.get("max"))
    ok = (lo is None or v >= lo) and (hi is None or v <= hi)
    need = "／".join(
        (
            x
            for x in [
                f"≥{lo:g}" if lo is not None else "",
                f"≤{hi:g}" if hi is not None else "",
            ]
            if x
        )
    )
    return (ok, f"{name} {v:g}{unit}（需 {need}）")


MACHINE_OPS = {k[2:]: v for k, v in globals().items() if k.startswith("m_")}
JUDGE_SYS_ZH = '你是严谨的深度研究报告评审员。针对给定的一道二元(是/否)问题，只依据【被评报告】中实际出现的内容，如实回答该问题本身。重要：直接回答问题字面（『报告是否……？』——事实成立则 answer=yes，不成立则 answer=no），不要根据『期望答案』去反推或调整你的回答；是否得分由程序判定，与你无关。不臆测、不脑补，证据必须引用报告原句。严格输出JSON：{"answer":"yes"|"no","reason":"...","evidence":"报告原句或\'未找到\'"}。'
JUDGE_SYS_EN = 'You are a rigorous reviewer of deep-research reports. You are given one binary (yes/no) question. Answer that question truthfully, using only what actually appears in the report under review. Important: answer the question literally ("Does the report …?" — yes if it holds, no if it does not). Do not reason backwards from any expected answer; scoring is done by the program and is none of your concern. Do not speculate or fill in gaps; the evidence must be a verbatim sentence quoted from the report. Output strict JSON: {"answer":"yes"|"no","reason":"...","evidence":"verbatim sentence from the report, or \'not found\'"}.'
JUDGE_SYS = JUDGE_SYS_ZH
JUDGE_SCAFFOLD = {
    "zh": {
        "sys": JUDGE_SYS_ZH,
        "query": "【用户query/聚焦点】",
        "report": "【被评报告可见文本】",
        "sources": "【报告来源清单】",
        "atom": "【待判定问题】",
        "note": "补充说明: ",
        "ask": "请如实回答该问题(是/否)并按JSON输出。",
    },
    "en": {
        "sys": JUDGE_SYS_EN,
        "query": "[USER QUERY / FOCUS]",
        "report": "[VISIBLE TEXT OF THE REPORT UNDER REVIEW]",
        "sources": "[SOURCES CITED BY THE REPORT]",
        "atom": "[QUESTION TO JUDGE]",
        "note": "Additional note: ",
        "ask": "Answer the question truthfully (yes/no) and output the JSON.",
    },
}
ANS_RE = re.compile('"answer"\\s*:\\s*"?\\s*(yes|no|是|否|true|false|1|0)', re.I)
REASON_RE = re.compile('"reason"\\s*:\\s*"(.*?)"\\s*(?:,\\s*"|\\}|$)', re.S)


def judge_text(atom, report_text, query, tools_used, model):
    L = JUDGE_SCAFFOLD[text_lang(atom.get("text") or "")]
    ctx = f"{L['query']}\n{query}\n\n{L['report']}\n{report_text}"
    if tools_used:
        ctx += f"\n\n{L['sources']}\n{tools_used[:4000]}"
    user = f"{ctx}\n\n{L['atom']}\n{atom['text']}\n{(L['note'] + atom['note'] if atom.get('note') else '')}\n\n{L['ask']}"
    reply = _chat(
        [{"role": "system", "content": L["sys"]}, {"role": "user", "content": user}],
        model=model,
        max_tokens=4000,
    )
    j = _parse_json(reply) or {}
    return _normalize(j, atom, reply)


JUDGE_VIS_SYS_ZH = '你是严谨的深度研究报告视觉评审。随附图片是这份报告渲染后的整页截图。针对给定的一道是非题(yes/no)，**只依据截图里实际能看到的视觉呈现**（配色、对比度、字号行距、版面留白与视觉重量）如实回答；文字内容对错不在本题范围。不要根据『期望答案』反推；是否得分由程序判定，与你无关。严格输出JSON：{"answer":"yes"|"no","reason":"...","evidence":"对截图里相关视觉细节的具体描述"}。'
JUDGE_VIS_SYS_EN = 'You are a rigorous visual reviewer of a deep-research report. The attached image is a full-page screenshot of the rendered report. For the single binary (yes/no) question, answer truthfully based ONLY on the visual presentation you can actually see in the screenshot (color scheme, contrast, font size and line spacing, layout whitespace and visual weight); factual correctness of the text is out of scope. Do not reason backwards from any expected answer; scoring is done by the program. Output strict JSON: {"answer":"yes"|"no","reason":"...","evidence":"concrete description of the relevant visual detail seen in the screenshot"}.'


def _report_shot(report_dir):
    bases = [_CACHE_DIR]
    if _PROBE_PATH:
        bases.insert(0, os.path.dirname(os.path.abspath(_PROBE_PATH)))
    for base in bases:
        for name in ("full.png", "desktop.png"):
            path = os.path.join(base, "probe_shots", name)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
    return None


def ensure_report_shot(report_dir):
    with _SHOT_LOCK:
        path = _report_shot(report_dir)
        if path:
            return path
        index = os.path.join(report_dir, "index.html")
        if not os.path.isfile(index) or os.path.getsize(index) == 0:
            return None
        output = os.path.join(_CACHE_DIR, "probe_shots", "full.png")
        os.makedirs(os.path.dirname(output), exist_ok=True)
        try:
            subprocess.run(
                [
                    sys.executable,
                    os.path.join(HERE, "screenshot.py"),
                    "file://" + os.path.abspath(index),
                    output,
                    "--full-page",
                    "--width",
                    "1440",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=240,
            )
        except Exception:
            return None
        return (
            output if os.path.isfile(output) and os.path.getsize(output) > 0 else None
        )


def _vlm_images(image_path):

    def _b64jpeg(im, quality):
        import io

        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=quality)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    try:
        from PIL import Image

        uris = []
        im = Image.open(image_path)
        w, h = im.size
        scale = min(720 / w, 8000 / h, 1.0)
        thumb = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        uris.append(_b64jpeg(thumb, 80))
        desk = os.path.join(os.path.dirname(image_path), "desktop.png")
        if os.path.isfile(desk):
            di = Image.open(desk)
            if di.size[0] > 1280:
                s = 1280 / di.size[0]
                di = di.resize((1280, int(di.size[1] * s)))
            uris.append(_b64jpeg(di, 85))
        return uris
    except Exception:
        with open(image_path, "rb") as f:
            return ["data:image/png;base64," + base64.b64encode(f.read()).decode()]


def judge_vision(atom, image_path, query, model):
    is_zh = text_lang(atom.get("text") or "") == "zh"
    sys_p = JUDGE_VIS_SYS_ZH if is_zh else JUDGE_VIS_SYS_EN
    uris = _vlm_images(image_path)
    if is_zh:
        prompt = f"【用户query/聚焦点】\n{query}\n\n【待判定问题】\n{atom['text']}\n{('补充说明: ' + atom['note'] if atom.get('note') else '')}\n\n随附{('两张图：第一张是整页缩略（看版面/留白/配色），第二张是首屏原分辨率（看对比/字号/可读性）' if len(uris) > 1 else '整页截图')}。请据此如实回答该问题(是/否)并按JSON输出。"
    else:
        prompt = f"[USER QUERY / FOCUS]\n{query}\n\n[QUESTION TO JUDGE]\n{atom['text']}\n{('Additional note: ' + atom['note'] if atom.get('note') else '')}\n\nAttached {('are two images: a full-page thumbnail (layout/whitespace/color) and a full-resolution first screen (contrast/font/legibility)' if len(uris) > 1 else 'is the full-page screenshot')}. Answer the question truthfully (yes/no) and output the JSON."
    content = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in uris]
    reply = _chat(
        [{"role": "system", "content": sys_p}, {"role": "user", "content": content}],
        model=_EVAL_CTX.get("vision_model") or model,
        max_tokens=2000,
    )
    j = _parse_json(reply) or {}
    return _normalize(j, atom, reply)


def _normalize(j, atom, raw):
    j = dict(j or {})
    ans_raw = j.get("answer")
    if ans_raw in (None, ""):
        m = ANS_RE.search(raw or "")
        if m:
            ans_raw = m.group(1)
            if not j.get("reason"):
                rm = REASON_RE.search(raw or "")
                if rm:
                    j["reason"] = rm.group(1)
    ans = str(ans_raw if ans_raw is not None else "").lower().strip()
    ans = (
        "yes"
        if ans.startswith("y") or ans in ("是", "true", "1")
        else "no" if ans.startswith("n") or ans in ("否", "false", "0") else None
    )
    exp = atom.get("expected", "yes")
    score = None if ans is None else 1 if ans == exp else 0
    return {
        "answer": ans,
        "score": score,
        "reason": (j.get("reason") or "")[:500],
        "evidence": str(j.get("evidence", ""))[:400],
    }


def _invalidate_ids(spec):
    if not spec:
        return []
    if os.path.exists(spec):
        blob = json.load(open(spec, encoding="utf-8"))
        items = (
            blob
            if isinstance(blob, list)
            else blob.get("atoms") or blob.get("ids") or []
        )
        return [x if isinstance(x, str) else x.get("id") for x in items if x]
    return [s.strip() for s in spec.split(",") if s.strip()]


def _claim_prev(prev_atoms, all_atoms, invalidate, src):
    by_cid, by_id, by_text = ({}, {}, {})
    for a in prev_atoms:
        if a.get("cid"):
            by_cid.setdefault(a["cid"], a)
        by_id.setdefault(a["id"], a)
        by_text.setdefault((a.get("check", "J"), a.get("text") or ""), a)

    def same_prompt(c, a):
        if (c.get("text") or "") != (a.get("text") or ""):
            return False
        if "note" not in c:
            return True
        return (c.get("note") or "") == (a.get("note") or "")

    inval = set(invalidate)
    cache, stale, renumbered = ({}, [], [])
    by_which = {"cid": 0, "id": 0, "text": 0}
    no_note_key, claimed, left = (0, set(), [])

    def take(a, hit, how):
        aid = a["id"]
        if a.get("check", "J") == "J" and "note" not in hit and a.get("note"):
            nonlocal no_note_key
            no_note_key += 1
        if aid in inval:
            stale.append(aid)
            return
        claimed.add(id(hit))
        same_id = hit.get("id") == aid
        cache[aid] = hit if same_id else dict(hit, id=aid)
        by_which[how] += 1
        if not same_id:
            renumbered.append(f"{hit['id']}→{aid}")

    for a in all_atoms:
        aid, is_j = (a["id"], a.get("check", "J") == "J")
        c_cid = by_cid.get(a.get("cid") or "\x00")
        c_id = by_id.get(aid)
        if c_cid is not None:
            if is_j and (not same_prompt(c_cid, a)):
                stale.append(aid)
            else:
                take(a, c_cid, "cid")
        elif c_id is not None and (not is_j or same_prompt(c_id, a)):
            take(a, c_id, "id")
        else:
            left.append((a, c_id))
    for a, c_id in left:
        t = by_text.get((a.get("check", "J"), a.get("text") or ""))
        if t is not None and t is not c_id and (id(t) not in claimed):
            take(a, t, "text")
        elif c_id is not None:
            stale.append(a["id"])
    if no_note_key:
        print(
            f"[rescore] {src} lacks saved notes for {no_note_key} atoms. If those notes changed, use --invalidate to prevent reuse of outdated judgments.",
            file=sys.stderr,
        )
    print(
        f"[rescore] Matched {len(cache)}/{len(all_atoms)} judgments: cid={by_which['cid']}, id={by_which['id']}, text={by_which['text']}.",
        file=sys.stderr,
    )
    if renumbered:
        print(
            f"[rescore] Reused {len(renumbered)} renumbered atoms with unchanged text: {renumbered[:8]}",
            file=sys.stderr,
        )
    if stale:
        print(
            f"[rescore] Invalidated {len(stale)} changed atoms: {stale[:8]}",
            file=sys.stderr,
        )
    return (cache, stale, renumbered)


def _vote(atom, rep, query, model, n):
    ballots = [
        judge_text(atom, rep["flatText"], query, rep.get("toolsUsed", ""), model)
        for _ in range(n)
    ]
    got = [b["score"] for b in ballots if b["score"] is not None]
    n1, n0 = (got.count(1), got.count(0))
    win = 1 if n1 > n0 else 0 if n0 > n1 else None
    lead = next((b for b in ballots if b["score"] == win), ballots[0])
    return {
        **lead,
        "score": win,
        "answer": lead["answer"] if win is not None else None,
        "reason": f"[{n} 票取多数 {n1}:{n0}{('，平票判不出来' if win is None else '')}] {lead['reason']}"[
            :500
        ],
        "n_votes": n,
        "votes": [b["score"] for b in ballots],
        "unanimous": bool(got) and len(set(got)) == 1 and (len(got) == n),
    }


def eval_atom(atom, data, files, rep, report_dir, query, model, no_llm, votes=1):
    ck = atom.get("check", "J")
    res = {
        "id": atom["id"],
        "cid": atom.get("cid") or "",
        "check": ck,
        "expected": atom.get("expected", "yes"),
        "text": atom["text"],
        "note": atom.get("note") or "",
    }
    try:
        if ck == "M":
            op = atom["machine"]["op"]
            args = dict(atom["machine"].get("args", {}))
            args["_report_dir"] = report_dir
            passed, detail = MACHINE_OPS[op](data, files, rep, args)
            res.update(
                {
                    "answer": (
                        ("yes" if passed else "no") if passed is not None else None
                    ),
                    "score": (1 if passed else 0) if passed is not None else None,
                    "reason": detail,
                    "evidence": "",
                }
            )
        elif no_llm:
            res.update(
                {
                    "answer": None,
                    "score": None,
                    "reason": "LLM disabled",
                    "evidence": "",
                }
            )
        elif _SKIP_VISION and atom.get("template") == "aesthetic":
            res.update(
                {
                    "answer": None,
                    "score": None,
                    "reason": "Visual judging disabled",
                    "evidence": "",
                }
            )
        elif atom.get("template") == "aesthetic":
            img = ensure_report_shot(report_dir)
            if not img:
                res.update(
                    {
                        "answer": None,
                        "score": None,
                        "reason": "P-VIS 视觉判据需要整页截图，但渲染/截图失败（缺 Playwright 或 index.html）——记 N/A 不计分",
                        "evidence": "",
                    }
                )
            else:
                res.update(judge_vision(atom, img, query, model))
        elif votes > 1:
            res.update(_vote(atom, rep, query, model, votes))
        else:
            res.update(
                judge_text(
                    atom, rep["flatText"], query, rep.get("toolsUsed", ""), model
                )
            )
    except Exception as e:
        res.update(
            {
                "answer": None,
                "score": None,
                "reason": f"ERROR: {_redact_error(e)}",
                "evidence": "",
            }
        )
    return res


def direction_pass(direction, atom_results):
    """Return a gate decision (None for core directions) and the raw pass rate."""
    scored = [r for r in atom_results if r["score"] is not None]
    if not scored:
        return (None, 0.0)
    total = len(scored)
    passed = sum((r["score"] for r in scored))
    rate = passed / total
    if direction.get("tier") == "core":
        return (None, rate)
    logic = direction.get("logic", "threshold")
    threshold = direction.get("threshold")
    if logic == "all":
        return (passed == total, rate)
    elif logic == "any":
        return (passed >= 1, rate)
    elif logic == "count":
        n = threshold if threshold else 1
        return (passed >= n, rate)
    else:
        t = threshold if threshold else 0.5
        return (rate >= t, rate)


from .spec import (
    GATE_MODE,
    GATE_VOTES,
    COVERAGE_SATURATION,
    COVERAGE_TEMPLATES,
    NA_CITATION_TEMPLATES,
    atom_fids,
    is_coverage_atom,
    is_na_cover_atom,
    text_lang,
    dir_weight,
    recency_window,
)

NA_COVER_TEMPLATES = COVERAGE_TEMPLATES
COUNT_CAP_DIRS = {"C-FC", "C-CA", "C-CMP"}
COUNT_CAP = 15


def _scoring_weight(ref, n_scored):
    w = min(n_scored, COUNT_CAP) if ref in COUNT_CAP_DIRS else n_scored
    return w * dir_weight(ref)


def _dim_tally(all_atoms, atom_results, dir_of, dim):
    idx = {a["id"]: i for i, a in enumerate(all_atoms)}
    n = p = 0
    for a in all_atoms:
        d = dir_of.get(a["id"]) or {}
        if d.get("dimension") != dim or d.get("tier") == "bonus":
            continue
        s = (atom_results[idx[a["id"]]] or {}).get("score")
        if s is None:
            continue
        n += 1
        p += 1 if s else 0
    return (n, p)


def _content_dir_gains(all_atoms, atom_results, dir_of):
    idx = {a["id"]: i for i, a in enumerate(all_atoms)}
    cov = defaultdict(lambda: [0, 0])
    tot = defaultdict(int)
    ref = {}
    for a in all_atoms:
        d = dir_of.get(a["id"]) or {}
        if d.get("dimension") != "content" or d.get("tier") == "bonus":
            continue
        s = (atom_results[idx[a["id"]]] or {}).get("score")
        if s is None:
            continue
        did = d.get("id") or d.get("ref") or id(d)
        ref[did] = d.get("ref")
        tot[did] += 1
        if is_coverage_atom(a, d.get("dimension"), d.get("ref")):
            cov[did][0] += 1
            cov[did][1] += 1 if s else 0
    out = {}
    for did, n_dc in tot.items():
        nc, pc = cov.get(did, [0, 0])
        if not nc or not COVERAGE_SATURATION:
            g = 1.0
        elif pc / nc < COVERAGE_SATURATION:
            g = 1.0 / COVERAGE_SATURATION
        else:
            g = 0.0
        out[did] = {"gain": g, "n": n_dc, "ref": ref.get(did)}
    return out


def propagate_na(all_atoms, atom_results, dir_of, dim_weights):
    idx = {a["id"]: i for i, a in enumerate(all_atoms)}
    for r in atom_results:
        if r and r.get("na_propagated"):
            r["score"] = 0
            r.pop("na_propagated", None)
            r["reason"] = str(r.get("reason") or "").split("原判理由：", 1)[-1]
    cover = defaultdict(list)
    for a in all_atoms:
        if not is_na_cover_atom(a, (dir_of.get(a["id"]) or {}).get("dimension")):
            continue
        for f in atom_fids(a):
            cover[f].append(a["id"])
    undo = []
    for a in all_atoms:
        if (dir_of.get(a["id"]) or {}).get("dimension") != "citation":
            continue
        if (a.get("template") or "") not in NA_CITATION_TEMPLATES:
            continue
        if (a.get("expected") or "yes") != "yes":
            continue
        fids = atom_fids(a)
        if not fids or not any((f in cover for f in fids)):
            continue
        r = atom_results[idx[a["id"]]]
        if not r or r.get("score") != 0:
            continue
        peer = [
            atom_results[idx[i]] for f in fids for i in cover.get(f, []) if i in idx
        ]
        got = [p["score"] for p in peer if p and p.get("score") is not None]
        if not got or max(got) != 0:
            continue
        undo.append((a["id"], r, r.get("reason"), fids))
        r["score"] = None
        r["na_propagated"] = True
        r["reason"] = (
            "N/A｜内容维度判定「报告没有说出这条事实」（覆盖题 "
            + ",".join(sorted({i for f in fids for i in cover.get(f, [])}))
            + " 全判 0），没提到已在内容维度扣过分，引用维度不再二次扣分。"
            + "原判理由："
            + str(r.get("reason") or "")
        )[:500]
    wc = dim_weights.get("content") or 0.0
    wr = dim_weights.get("citation") or 0.0
    c_gains = _content_dir_gains(all_atoms, atom_results, dir_of)
    Wc = sum((_scoring_weight(v["ref"], v["n"]) for v in c_gains.values())) or 1.0
    fid_content_dirs = defaultdict(set)
    for f, cover_ids in cover.items():
        for i in cover_ids:
            did = (dir_of.get(i) or {}).get("id")
            if did in c_gains:
                fid_content_dirs[f].add(did)
    by_id = {aid: (r, old, fs) for aid, r, old, fs in undo}
    live, retracted = (set(by_id), [])
    nc, _ = _dim_tally(all_atoms, atom_results, dir_of, "content")

    def _citation_dir_tally():
        n = defaultdict(int)
        p = defaultdict(int)
        k = defaultdict(int)
        ref = {}
        for a in all_atoms:
            d = dir_of.get(a["id"]) or {}
            if d.get("dimension") != "citation" or d.get("tier") == "bonus":
                continue
            did = d.get("id")
            ref[did] = d.get("ref")
            if a["id"] in live:
                n[did] += 1
                k[did] += 1
            else:
                rr = atom_results[idx[a["id"]]]
                if rr and rr.get("score") is not None:
                    n[did] += 1
                    p[did] += 1 if rr["score"] else 0
        return (n, p, k, ref)

    def _deltas():
        involved = set()
        for aid in live:
            for f in by_id[aid][2]:
                involved |= fid_content_dirs.get(f, set())
        dc_terms = []
        for did in involved:
            g = c_gains[did]
            n_dc = g["n"] or 1
            wdc = _scoring_weight(g["ref"], g["n"]) / Wc
            dc_terms.append(wc * wdc * g["gain"] / n_dc)
        d_content = min(dc_terms) if dc_terms else float("inf")
        n, p, k, ref = _citation_dir_tally()
        Wr = sum((_scoring_weight(ref[did], n[did]) for did in n if n[did])) or 1.0
        d_free = 0.0
        per_dir = {}
        for did in n:
            if not k[did] or n[did] <= k[did]:
                continue
            r_j = p[did] / n[did]
            wj = _scoring_weight(ref[did], n[did]) / Wr
            term = wr * wj * r_j * k[did] / (n[did] - k[did])
            d_free += term
            per_dir[did] = {
                "ref": ref[did],
                "n": n[did],
                "p": p[did],
                "k": k[did],
                "r": round(r_j, 4),
                "w": round(wj, 4),
                "delta": round(term, 6),
            }
        return (d_content, d_free, per_dir, sorted(involved))

    while True:
        d_content, d_free, per_dir, content_dirs = _deltas()
        if d_content > d_free or not live:
            break
        per_fid = defaultdict(int)
        for aid in live:
            for f in by_id[aid][2]:
                per_fid[f] += 1
        worst = max(per_fid.items(), key=lambda kv: (kv[1], kv[0]))[0]
        aid = max((x for x in live if worst in by_id[x][2]))
        r, old, _ = by_id[aid]
        r["score"] = 0
        r.pop("na_propagated", None)
        r["reason"] = old
        live.discard(aid)
        retracted.append(aid)
    hit = sorted(live)
    nr, pr = _dim_tally(all_atoms, atom_results, dir_of, "citation")
    rate = pr / nr if nr else None
    per_fid = defaultdict(int)
    for aid in live:
        for f in by_id[aid][2]:
            per_fid[f] += 1
    k_max = max(per_fid.values()) if per_fid else 0
    binding_gain = None
    if content_dirs:
        binding = min(
            content_dirs,
            key=lambda did: _scoring_weight(c_gains[did]["ref"], c_gains[did]["n"])
            / Wc
            * c_gains[did]["gain"]
            / (c_gains[did]["n"] or 1),
        )
        binding_gain = c_gains[binding]["gain"]
    limit = (
        wc / wr * (binding_gain or 0.0) * (nr - k_max) / (k_max * nc)
        if wr and nc and k_max and (nr > k_max)
        else None
    )
    guard = {
        "applied": bool(hit),
        "n_candidate": len(by_id),
        "n_applied": len(hit),
        "n_retracted": len(retracted),
        "retracted": sorted(retracted)[:50],
        "delta_content": None if d_content == float("inf") else round(d_content, 6),
        "delta_free": round(d_free, 6),
        "per_dir": per_dir,
        "content_dir": content_dirs,
        "nc": nc,
        "nr": nr,
        "k_max": k_max,
        "content_gain": round(binding_gain, 3) if binding_gain is not None else None,
        "citation_pass_rate": rate,
        "limit": limit,
        "rule": "Δ内容 = W内·ŵ_Dc·gain/n_Dc  >  Δ引用 = W引·Σ_j ŵ_j·r_j·k_j/(n_j−k_j)",
    }
    if retracted:
        dc_show = "∞" if d_content == float("inf") else round(d_content, 4)
        guard["why"] = (
            f"{len(by_id)} 道候选里退回 {len(retracted)} 道照判 0：再多传导，引用维度白拿的 Δ引用={round(d_free, 4)} 就会顶过内容维度多说一条事实的 Δ内容={dc_show}，让「没提到」比「提到但没给引用」得分更高——排序翻转比重复扣分更坏。病根是引用维度里不挂具体事实的题（来源清单整体的数量/时效/多样性）太少，报告没覆盖到的事实一多，这个维度的分母就塌，要在卷面侧补这类题。"
        )
    return (hit, guard)


def _coverage_split(direction, atom_results, sat):
    if sat >= 1.0:
        return (None, None)
    spec_by_id = {a["id"]: a for a in direction.get("atoms") or []}
    scored = [r for r in atom_results if r["score"] is not None]
    if not scored:
        return (None, None)
    cov = [
        r
        for r in scored
        if is_coverage_atom(
            spec_by_id.get(r["id"]) or {},
            direction.get("dimension"),
            direction.get("ref"),
        )
    ]
    if not cov:
        return (None, None)
    n_cov, n_all = (len(cov), len(scored))
    p_cov = sum((r["score"] for r in cov))
    p_all = sum((r["score"] for r in scored))
    r_cov = p_cov / n_cov
    eff_cov = min(1.0, r_cov / sat)
    n_rest = n_all - n_cov
    r_rest = (p_all - p_cov) / n_rest if n_rest else 0.0
    eff = (eff_cov * n_cov + r_rest * n_rest) / n_all
    return (
        round(eff, 4),
        {
            "saturation": round(sat, 4),
            "n_coverage_atoms": n_cov,
            "n_other_atoms": n_rest,
            "coverage_rate": round(r_cov, 4),
            "coverage_credited": round(eff_cov, 4),
            "other_rate": round(r_rest, 4),
        },
    )


def aggregate(
    directions_results,
    dim_weights,
    gate_mode=GATE_MODE,
    coverage_sat=COVERAGE_SATURATION,
):
    dir_scores = []
    gate_failures = []
    sat = float(coverage_sat or COVERAGE_SATURATION)
    sat_gate_dirs = []
    for dr in directions_results:
        d = dr["direction"]
        d_pass, d_rate = direction_pass(d, dr["atoms"])
        gate_failed = d["tier"] == "gate" and d_pass is False
        entry = {
            "id": d["id"],
            "ref": d.get("ref"),
            "name": d["name"],
            "dimension": d["dimension"],
            "tier": d["tier"],
            "n_atoms": len(dr["atoms"]),
            "n_scored": len([a for a in dr["atoms"] if a["score"] is not None]),
            "n_passed": sum(
                (a["score"] for a in dr["atoms"] if a["score"] is not None)
            ),
            "pass_rate": round(d_rate, 4),
            "gate_failed": gate_failed,
            "effective_rate": 0.0 if gate_failed else round(d_rate, 4),
        }
        if d["tier"] == "gate":
            entry.update(
                logic=d.get("logic", "threshold"),
                threshold=d.get("threshold"),
                logic_met=d_pass,
            )
        if not gate_failed:
            eff, cov_info = _coverage_split(d, dr["atoms"], sat)
            if cov_info:
                entry["effective_rate"] = eff
                entry["coverage"] = cov_info
                if d["tier"] == "gate":
                    sat_gate_dirs.append(entry["id"])
        dir_scores.append(entry)
        if gate_failed:
            failed_atoms = [a["id"] for a in dr["atoms"] if a["score"] == 0]
            gate_failures.append(
                {
                    "direction": entry["id"],
                    "name": entry["name"],
                    "dimension": entry["dimension"],
                    "logic": entry["logic"],
                    "threshold": entry["threshold"],
                    "pass_rate": entry["pass_rate"],
                    "failed_atoms": failed_atoms,
                }
            )
    zeroed_dims = (
        {d["dimension"] for d in dir_scores if d["gate_failed"]}
        if gate_mode == "dimension"
        else set()
    )
    dim_scores, dim_raw, dim_dir_weights = ({}, {}, {})
    for dim in dim_weights:
        ds = [
            d
            for d in dir_scores
            if d["dimension"] == dim
            and d["tier"] in ("core", "gate")
            and (d["n_scored"] > 0)
        ]
        W = sum((_scoring_weight(d["ref"], d["n_scored"]) for d in ds))
        if not ds or not W:
            dim_raw[dim] = dim_scores[dim] = None
            dim_dir_weights[dim] = {}
            continue
        dim_raw[dim] = round(
            sum(
                (
                    d["effective_rate"] * _scoring_weight(d["ref"], d["n_scored"])
                    for d in ds
                )
            )
            / W,
            4,
        )
        dim_scores[dim] = 0.0 if dim in zeroed_dims else dim_raw[dim]
        dim_dir_weights[dim] = {
            d["id"]: round(_scoring_weight(d["ref"], d["n_scored"]) / W, 4) for d in ds
        }
    live = [d for d in dim_weights if dim_scores[d] is not None]
    wsum = sum((dim_weights[d] for d in live)) or 1.0
    base = round(sum((dim_weights[d] * dim_scores[d] for d in live)) / wsum * 100, 2)
    total_score = round(max(0.0, min(100.0, base)), 2)
    return {
        "dimension_scores": dim_scores,
        "dimension_raw_scores": dim_raw,
        "dimension_weights": dim_weights,
        "dimension_direction_weights": dim_dir_weights,
        "base_score": base,
        "raw_score": total_score,
        "gate_pass": len(gate_failures) == 0,
        "gate_mode": gate_mode,
        "coverage_saturation": round(sat, 4),
        "coverage_saturation_gate_directions": sat_gate_dirs,
        "gate_zeroed_dimensions": sorted(zeroed_dims),
        "total_score": total_score,
        "gate_failures": gate_failures,
        "directions": dir_scores,
    }


def main():
    global _CACHE_DIR, _PROBE_PATH, _SKIP_VISION
    ap = argparse.ArgumentParser(
        description="Evaluate one report against an OmniDR rubric."
    )
    ap.add_argument(
        "--report",
        default="",
        help="Input report directory containing index.html and js/data.js.",
    )
    ap.add_argument("--rubrics", required=True, help="Rubric JSON file.")
    ap.add_argument(
        "--query", default="", help="User query text or a UTF-8 file containing it."
    )
    ap.add_argument("--out", default="scores.json", help="Output score JSON.")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Judge model name.")
    ap.add_argument("--vision-model", help="Vision judge; defaults to --model.")
    ap.add_argument(
        "--evaluation-date",
        help="Date for recency checks (YYYY-MM-DD); defaults to today.",
    )
    ap.add_argument("--workers", type=int, default=8, help="Concurrent atom judgments.")
    ap.add_argument("--no-llm", action="store_true", help="run machine checks only")
    ap.add_argument(
        "--skip-vision",
        action="store_true",
        help="Skip aesthetic judgments without rendering screenshots.",
    )
    ap.add_argument(
        "--gate-mode",
        choices=["dimension", "direction"],
        default="",
        help="override the rubric's gate_mode",
    )
    ap.add_argument(
        "--gate-votes",
        type=int,
        default=GATE_VOTES,
        help="Number of majority votes for J atoms in gate directions (default: 3).",
    )
    ap.add_argument(
        "--rescore",
        default="",
        help="Reaggregate saved atom judgments against the supplied rubric.",
    )
    ap.add_argument(
        "--rescore-partial",
        action="store_true",
        help="Evaluate atoms missing from the saved judgment; missing J atoms call the model.",
    )
    ap.add_argument(
        "--invalidate",
        default="",
        help="Invalidate comma-separated atom IDs, or IDs loaded from a JSON file.",
    )
    ap.add_argument(
        "--cache-dir",
        help="Directory for generated screenshots and probe files; defaults beside --out.",
    )
    ap.add_argument(
        "--probe",
        help="Explicit browser probe JSON; defaults to <cache-dir>/render_probe.json.",
    )
    args = ap.parse_args()
    _SKIP_VISION = args.skip_vision
    _CACHE_DIR = os.path.abspath(
        args.cache_dir
        or os.path.join(os.path.dirname(os.path.abspath(args.out)), "cache")
    )
    _PROBE_PATH = os.path.abspath(args.probe) if args.probe else None
    if not args.report and (not args.rescore):
        ap.error(
            "--report is required (or use --rescore to re-aggregate an existing scores.json)"
        )
    query = args.query
    if query and os.path.exists(query):
        query = open(query, encoding="utf-8").read()
    rubrics = json.load(open(args.rubrics, encoding="utf-8"))
    dim_weights = rubrics.get(
        "dimensions", {"content": 0.5, "citation": 0.3, "presentation": 0.2}
    )
    scoring = rubrics.get("scoring", {})
    gate_mode = args.gate_mode or scoring.get("gate_mode", GATE_MODE)
    _EVAL_CTX["video_published"] = rubrics.get("video_published")
    _EVAL_CTX["today"] = args.evaluation_date or None
    _EVAL_CTX["vision_model"] = args.vision_model or args.model
    coverage_sat = float(scoring.get("coverage_saturation") or COVERAGE_SATURATION)
    directions = rubrics["directions"]
    all_atoms = []
    atom_to_dir = {}
    votes_of = {}
    for d in directions:
        nv = args.gate_votes if d.get("tier") == "gate" else 1
        for atom in d["atoms"]:
            all_atoms.append(atom)
            atom_to_dir[atom["id"]] = d["id"]
            votes_of[atom["id"]] = nv if atom.get("check", "J") == "J" else 1
    n_total = len(all_atoms)
    if args.rescore:
        prev = json.load(open(args.rescore, encoding="utf-8"))
        prev_atoms = [a for d in prev["directions"] for a in d["atoms"]]
        cache, _, _ = _claim_prev(
            prev_atoms, all_atoms, _invalidate_ids(args.invalidate), args.rescore
        )
        missing = [a["id"] for a in all_atoms if a["id"] not in cache]
        if missing and (not args.rescore_partial):
            sys.exit(
                f"[rescore] {len(missing)} atoms absent from {args.rescore}: {missing[:8]}…\n          the rubric changed — do a full run instead, or pass --rescore-partial if the new atoms are machine checks."
            )
        force = [
            a["id"]
            for a in all_atoms
            if a.get("check", "J") != "J" and a["id"] in cache
        ]
        if force and (not args.report):
            force = []
        todo = set(missing) | set(force)
        report_path, model = (prev.get("report", ""), prev.get("model", args.model))
        if not todo:
            atom_results = [cache[a["id"]] for a in all_atoms]
        else:
            rep = load_report(args.report)
            data, files = (rep["data"], rep["files"])
            report_path = args.report
            atom_results = [None] * n_total

            def run_missing(i):
                a = all_atoms[i]
                atom_results[i] = (
                    eval_atom(
                        a,
                        data,
                        files,
                        rep,
                        args.report,
                        query,
                        args.model,
                        args.no_llm,
                        votes=votes_of.get(a["id"], 1),
                    )
                    if a["id"] in todo
                    else cache[a["id"]]
                )

            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                list(ex.map(run_missing, range(n_total)))
    else:
        rep = load_report(args.report)
        data, files = (rep["data"], rep["files"])
        report_path, model = (args.report, args.model)
        atom_results = [None] * n_total

        def run(i):
            atom_results[i] = eval_atom(
                all_atoms[i],
                data,
                files,
                rep,
                args.report,
                query,
                args.model,
                args.no_llm,
                votes=votes_of.get(all_atoms[i]["id"], 1),
            )

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(run, range(n_total)))
    _dir_by_id = {d["id"]: d for d in directions}
    dir_of = {a["id"]: _dir_by_id.get(atom_to_dir[a["id"]]) or {} for a in all_atoms}
    na_hit, na_guard = propagate_na(all_atoms, atom_results, dir_of, dim_weights)
    dir_atom_map = defaultdict(list)
    for i, atom in enumerate(all_atoms):
        dir_atom_map[atom_to_dir[atom["id"]]].append(atom_results[i])
    directions_results = []
    for d in directions:
        directions_results.append({"direction": d, "atoms": dir_atom_map[d["id"]]})
    agg = aggregate(
        directions_results, dim_weights, gate_mode, coverage_sat=coverage_sat
    )
    scored = [r for r in atom_results if r and r.get("score") is not None]
    errored = [
        r
        for r in atom_results
        if r
        and r.get("score") is None
        and str(r.get("reason") or "").startswith("ERROR:")
    ]
    err_reasons = Counter((str(r["reason"])[:80] for r in errored))
    out = {
        "report": report_path,
        "model": model,
        "n_atoms": n_total,
        "n_scored": len(scored),
        "n_skipped": n_total - len(scored),
        "n_errored": len(errored),
        "n_na_propagated": len(na_hit),
        "na_propagated": na_hit[:200],
        "na_guard": na_guard,
        "error_reasons": dict(err_reasons.most_common(10)),
        "aggregate": agg,
        "directions": [
            {
                "id": d["id"],
                "name": d["name"],
                "dimension": d["dimension"],
                "tier": d["tier"],
                "atoms": dir_atom_map[d["id"]],
            }
            for d in directions
        ],
    }
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(
        f"Score: {agg['total_score']:.2f}/100; gate={('PASS' if agg['gate_pass'] else 'FAIL')}; atoms={n_total}; scored={len(scored)}; skipped={n_total - len(scored)}; errors={len(errored)}; output={args.out}"
    )
    if errored:
        print(
            "Evaluation is incomplete: failed judgments are excluded from score denominators.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
