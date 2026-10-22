"""Scoring constants and pure helpers for the released evaluation protocol."""

from __future__ import annotations
import datetime as _dt
import re

DIR_WEIGHTS: dict = {}


def dir_weight(ref) -> float:
    w = DIR_WEIGHTS.get(ref)
    try:
        w = float(w)
    except (TypeError, ValueError):
        return 1.0
    return w if w > 0 else 1.0


def _parse_date(s):
    s = str(s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m", "%Y"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match("(\\d{4})\\D+(\\d{1,2})\\D+(\\d{1,2})", s)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def recency_window(video_published, today=None):
    pub = _parse_date(video_published)
    if pub is None:
        return None
    if today is None:
        today = _dt.date.today()
    elif not isinstance(today, _dt.date):
        today = _parse_date(today)
        if today is None:
            return None
    age_days = (today - pub).days
    if age_days > 3 * 365:
        return ("近三年", today - _dt.timedelta(days=3 * 365))
    if age_days >= 182:
        return ("近半年", today - _dt.timedelta(days=182))
    return None


_HAN_RE = re.compile("[一-鿿]")


def text_lang(text: str, zh_ratio: float = 0.1) -> str:
    s = re.sub("\\s", "", text or "")
    if not s:
        return "zh"
    return "zh" if len(_HAN_RE.findall(s)) / len(s) >= zh_ratio else "en"


COVERAGE_SATURATION = 0.18
COVERAGE_TEMPLATES = ("fact_entity", "fact_quote", "fact_value", "timeliness")
COVERAGE_DIRECTIONS = ("C-FC", "C-CA", "C-CMP")
NA_CITATION_TEMPLATES = ("support", "crossverify", "relevance", "authority")


def is_coverage_atom(atom: dict, dimension: str, ref: str | None = None) -> bool:
    if dimension != "content":
        return False
    if (atom.get("expected") or "yes") != "yes" or atom.get("check") == "M":
        return False
    return ref in COVERAGE_DIRECTIONS


def is_na_cover_atom(atom: dict, dimension: str) -> bool:
    if dimension != "content":
        return False
    if (atom.get("expected") or "yes") != "yes":
        return False
    return (atom.get("template") or "") in COVERAGE_TEMPLATES


GATE_MODE = "direction"
GATE_VOTES = 3


def atom_fids(atom: dict):
    fids = list(atom.get("fids") or [])
    if atom.get("fid") and atom["fid"] not in fids:
        fids.insert(0, atom["fid"])
    return fids
