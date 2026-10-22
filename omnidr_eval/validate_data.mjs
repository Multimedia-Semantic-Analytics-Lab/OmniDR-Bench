#!/usr/bin/env node

import fs from "node:fs";
import vm from "node:vm";
import nodePath from "node:path";

const ANCHOR_CATS = new Set([
  "背景知识", "更多支撑", "类似产品·事件对比", "网络观点评论", "相似资料", "时效纠偏",
  "background", "more support", "comparison", "commentary", "related material", "timeliness",
]);
const RESEARCH_TYPES = new Set(["web", "image", "video", "report"]);
const BLOCK_TYPES = new Set(["text", "image", "flow", "mindmap", "svg"]);

const errors = [];
const warns = [];
const E = (m) => errors.push(m);
const W = (m) => warns.push(m);

function isNum(x) { return typeof x === "number" && isFinite(x); }
function isStr(x) { return typeof x === "string" && x.length > 0; }
function isArr(x) { return Array.isArray(x); }

const path = process.argv[2];
if (!path) { console.error("usage: node validate_data.mjs <js/data.js>"); process.exit(2); }
let code;
try { code = fs.readFileSync(path, "utf8"); }
catch (e) { console.error(`cannot read ${path}: ${e.message}`); process.exit(2); }

const dataDir = nodePath.dirname(nodePath.resolve(path));
const reportRoot = nodePath.basename(dataDir) === "js" ? nodePath.dirname(dataDir) : dataDir;

const localAssets = new Map(); // rel-path -> [where,...]
function isRemoteAsset(p) { return /^(https?:)?\/\//i.test(p) || /^(oss|data|blob):/i.test(p); }
function noteAsset(p, where) {
  if (!isStr(p) || isRemoteAsset(p)) return;   // only local, in-report paths are checked
  if (!localAssets.has(p)) localAssets.set(p, []);
  localAssets.get(p).push(where);
}

const sandbox = vm.createContext(Object.create(null), {
  codeGeneration: { strings: false, wasm: false },
  microtaskMode: "afterEvaluate",
});
vm.runInContext(`
  globalThis.window = {};
  globalThis.DATA = undefined;
  globalThis.console = Object.freeze({
    log() {}, info() {}, warn() {}, error() {}, debug() {}, trace() {},
    assert() {}, dir() {}, dirxml() {}, table() {}, clear() {},
    time() {}, timeEnd() {}, timeLog() {}, count() {}, countReset() {},
    group() {}, groupCollapsed() {}, groupEnd() {},
  });
`, sandbox, { timeout: 5000 });
let DATA;
try {
  vm.runInContext(code, sandbox, { filename: path, timeout: 5000 });
  const serialized = vm.runInContext("JSON.stringify(window.DATA)", sandbox, { timeout: 5000 });
  DATA = typeof serialized === "string" ? JSON.parse(serialized) : undefined;
} catch (e) {
  console.error(`✗ ${path} is not valid JS (fix syntax first): ${e.message}`);
  process.exit(1);
}

if (!DATA || typeof DATA !== "object") {
  console.error("✗ window.DATA is missing or not an object.");
  process.exit(1);
}

const research = isArr(DATA.research) ? DATA.research : [];
const SRC_IDS = new Set();
const SRC_MEDIA = new Map();   // S# -> local copy path ("" when the page was never saved)
research.forEach((r) => {
  if (!r || !isStr(r.id)) return;
  SRC_IDS.add(r.id);
  SRC_MEDIA.set(r.id, isStr(r.media) ? r.media : "");
});

const allInsights = [];       // {ins, at} — the evidence pass needs research[] parsed first
const referenced = new Map(); // id -> where (for error messages)
function refCite(id, where) { if (!referenced.has(id)) referenced.set(id, where); }
function scanText(text, where) {
  if (text == null) return;
  const re = /\[(S\d+)\]/g; let m;
  while ((m = re.exec(String(text))) !== null) refCite(m[1], where);
}
function scanSources(arr, where) {
  (isArr(arr) ? arr : []).forEach((id) => { if (isStr(id)) refCite(id, where); });
}

if (DATA.meta) {
  const t = DATA.meta.theme;
  if (t != null && !/^t(0[1-9]|1[0-9]|20)$/.test(String(t)))
    E(`meta.theme "${t}" is not t01..t20.`);
  const bg = DATA.meta.background;
  if (bg != null) {
    if (typeof bg !== "string" || !bg.length) E("meta.background must be a non-empty string path (or omit it).");
    else {
      if (/^(https?:)?\/\//i.test(bg)) W(`meta.background "${bg}" is a remote URL — download it into img/bg/ so file:// stays offline.`);
      else { if (!/^img\//.test(bg)) W(`meta.background "${bg}" should live under img/ (e.g. img/bg/hero.jpg) so it travels with the report.`); noteAsset(bg, "meta.background"); }
    }
  }
}
const video = DATA.video || {};
if (!isStr(video.title)) W("video.title is empty.");
if (!isNum(video.duration)) E("video.duration must be a number (seconds).");
else if (video.duration <= 0) W("video.duration should be positive and match the source video.");
noteAsset(video.src, "video.src");
noteAsset(video.poster, "video.poster");
if (!isStr(video.poster))
  W("video.poster is empty; provide a local preview image.");

scanText(DATA.summary, "summary");

const frameUses = new Map(); // src -> [where,...]  (right-column figures only)
function noteFrame(src, where) {
  if (!isStr(src)) return;
  if (!frameUses.has(src)) frameUses.set(src, []);
  frameUses.get(src).push(where);
}
function tsOf(src) { // best-effort seconds from names like seg3-02m35s.jpg
  const m = /(\d+)m(\d+)s/.exec(String(src || ""));
  return m ? parseInt(m[1], 10) * 60 + parseInt(m[2], 10) : null;
}

const segs = isArr(DATA.segments) ? DATA.segments : [];
if (!segs.length) E("segments[] is empty — the report has no backbone.");
const segIds = new Set();
segs.forEach((s, i) => {
  const at = `segments[${i}]`;
  if (!s || typeof s !== "object") { E(`${at} is not an object.`); return; }
  if (!isStr(s.id)) E(`${at}.id missing.`);
  else { if (segIds.has(s.id)) E(`${at}.id "${s.id}" is duplicated.`); segIds.add(s.id); }
  if (!isNum(s.start)) E(`${at}.start must be a number.`);
  if (!isNum(s.end)) E(`${at}.end must be a number.`);
  if (isNum(s.start) && isNum(s.end) && !(s.start < s.end))
    E(`${at}: need start < end (got ${s.start} … ${s.end}).`);
  if (!isStr(s.title)) W(`${at}.title is empty.`);
  if (s.keyframe != null) {
    if (!/^img\//.test(String(s.keyframe))) W(`${at}.keyframe "${s.keyframe}" should live under img/.`);
    noteFrame(s.keyframe, `${at}.keyframe`);
    noteAsset(s.keyframe, `${at}.keyframe`);
  }
  const caption = isArr(s.caption) ? s.caption : [];
  caption.forEach((b, j) => {
    const bat = `${at}.caption[${j}]`;
    if (!b || typeof b !== "object") { E(`${bat} is not an object.`); return; }
    if (!BLOCK_TYPES.has(b.type)) { E(`${bat}.type "${b.type}" invalid (text|image|flow|mindmap|svg).`); return; }
    if (b.type === "text") { if (!isStr(b.text)) E(`${bat} text block needs .text.`); else scanText(b.text, bat); }
    if (b.type === "image") { if (!isStr(b.src)) E(`${bat} image block needs .src.`); else { noteFrame(b.src, bat); noteAsset(b.src, bat); } if (b.cap) scanText(b.cap, bat); }
    if (b.type === "flow" && !(isArr(b.steps) && b.steps.length)) E(`${bat} flow block needs steps[].`);
    if (b.type === "mindmap" && !b.root && !(isArr(b.branches) && b.branches.length)) E(`${bat} mindmap needs root or branches[].`);
    if (b.type === "svg" && !isStr(b.svg)) E(`${bat} svg block needs .svg markup.`);
  });
  scanSources(s.sources, `${at}.sources`);
  (isArr(s.insights) ? s.insights : []).forEach((ins, k) => scanInsight(ins, `${at}.insights[${k}]`));
});

const anchors = isArr(DATA.anchors) ? DATA.anchors : [];
const ANCHOR_IDS = new Set();
if (!anchors.length)
  E("anchors[] is missing or empty — record the anchors you researched (§3: id/label/category/why/segmentIds). " +
    "They are the anchor↔evidence trace, not page content; research[].anchorId points at them.");
anchors.forEach((a, i) => {
  const at = `anchors[${i}]`;
  if (!a || typeof a !== "object") { E(`${at} is not an object.`); return; }
  if (!isStr(a.id)) E(`${at}.id missing.`);
  else { if (ANCHOR_IDS.has(a.id)) E(`${at}.id "${a.id}" is duplicated.`); ANCHOR_IDS.add(a.id); }
  if (!isStr(a.label)) W(`${at}.label is empty.`);
  if (a.category != null && !ANCHOR_CATS.has(a.category))
    E(`${at}.category "${a.category}" not in the allowed set.`);
  if (a.frame) { noteFrame(a.frame, `${at}.frame`); noteAsset(a.frame, `${at}.frame`); }   // anchor frames aren't right-column, tracked for dup ts only
});

function scanInsight(ins, at) {
  if (!ins || typeof ins !== "object") { E(`${at} is not an object.`); return; }
  allInsights.push({ ins, at });
  scanText(ins.body, `${at}.body`);
  scanText(ins.videoClaim, `${at}.videoClaim`);
  scanText(ins.currentFact, `${at}.currentFact`);
  if (ins.table && isArr(ins.table.rows))
    ins.table.rows.forEach((row) => (isArr(row) ? row : [row]).forEach((c) => scanText(c, `${at}.table`)));
  scanSources(ins.sources, `${at}.sources`);
}
(isArr(DATA.insights) ? DATA.insights : []).forEach((ins, i) => scanInsight(ins, `insights[${i}]`));

const MANGLED_DATE_URL = /^https?:\/\/[^/]+\/(?:[^/]+\/)*?\d{4}-\d{2}-\d{2}\//i;
let anchorRefs = 0;
research.forEach((r, i) => {
  const at = `research[${i}]`;
  if (!r || typeof r !== "object") { E(`${at} is not an object.`); return; }
  if (!isStr(r.id) || !/^S\d+$/.test(r.id)) E(`${at}.id "${r && r.id}" must match S\\d+.`);
  if (!RESEARCH_TYPES.has(r.type)) E(`${at}.type "${r.type}" invalid (web|image|video|report).`);
  if (r.type === "web" && !isStr(r.url)) E(`${at} (web) needs a url.`);
  if (!isStr(r.summary)) W(`${at}.summary is empty — add the distilled finding.`);
  if (isStr(r.url) && MANGLED_DATE_URL.test(r.url))
    W(`${at}.url "${r.url}" has a /YYYY-MM-DD/ path — news permalinks use /YYYY/MM/DD/. ` +
      `Open it before citing; this shape is a 404 in practice.`);
  if (r.anchorId != null) {
    anchorRefs++;
    if (!ANCHOR_IDS.has(r.anchorId))
      E(`${at}.anchorId "${r.anchorId}" does not exist in anchors[] — ` +
        `add the anchor or drop the back-reference (dangling anchorIds break the anchor→evidence trace).`);
  }
  noteAsset(r.thumb, `${at}.thumb`);
  noteAsset(r.media, `${at}.media`);
});
if (anchors.length && research.length && anchorRefs === 0)
  W(`${anchors.length} anchor(s) but no research entry carries an anchorId — ` +
    `the anchor→evidence link is one-way. Tag each source with the anchor it answers.`);

const RISK_KINDS = new Set(["timeliness", "compare"]);
const EV_HARD = process.env.VALIDATE_EVIDENCE === "hard";
const EV = (m) => (EV_HARD ? E(m) : W(m));

function visibleText(s) {
  return s
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ").replace(/&amp;/gi, "&").replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'").replace(/&lt;/gi, "<").replace(/&gt;/gi, ">")
    .replace(/[‘’]/g, "'").replace(/[“”]/g, '"')
    .replace(/[‐-―]/g, "-").replace(/ /g, " ")
    .replace(/\s+/g, " ")
    .toLowerCase();
}
function normQuote(s) {
  return String(s)
    .replace(/[‘’]/g, "'").replace(/[“”]/g, '"')
    .replace(/[‐-―]/g, "-").replace(/ /g, " ")
    .replace(/\s+/g, " ").trim().toLowerCase();
}

let riskInsights = 0, riskWithEvidence = 0, quotesChecked = 0;
const noEvidence = [];
for (const { ins, at } of allInsights) {
  if (!RISK_KINDS.has(ins.kind)) continue;
  riskInsights++;
  const src = (isArr(ins.sources) ? ins.sources : []).filter(isStr);

  if (src.length < 2)
    E(`${at} (kind "${ins.kind}") cites ${src.length} source(s) — corrections and ` +
      `comparisons need ≥2 INDEPENDENT sources (data contract). One source asserting ` +
      `something the video does not say is unverifiable by the reader.`);

  const ev = isArr(ins.evidence) ? ins.evidence : [];
  if (!ev.length) { noEvidence.push(at); continue; }
  riskWithEvidence++;

  ev.forEach((e, j) => {
    const eat = `${at}.evidence[${j}]`;
    if (!e || typeof e !== "object") { EV(`${eat} is not an object.`); return; }
    if (!isStr(e.srcId)) EV(`${eat}.srcId missing.`);
    else if (!SRC_IDS.has(e.srcId)) EV(`${eat}.srcId "${e.srcId}" has no research entry.`);
    else if (!(isArr(ins.sources) && ins.sources.includes(e.srcId)))
      EV(`${eat}.srcId "${e.srcId}" is not in ${at}.sources — evidence must back a cited source.`);

    if (!isStr(e.quote)) { EV(`${eat}.quote missing — paste the sentence that carries the claim.`); return; }
    const q = normQuote(e.quote);
    if (q.length < 20) { EV(`${eat}.quote is ${q.length} chars — too short to be evidence (≥20).`); return; }

    if (!isStr(e.local)) {
      EV(`${eat}.local missing — save the cited page into the report ` +
         `(_assets/docs/${e.srcId || "S#"}.html) and point .local at it. ` +
         `A source read only into the context window leaves nothing to re-check.`);
      return;
    }
    const abs = nodePath.resolve(reportRoot, e.local);
    if (!fs.existsSync(abs)) { EV(`${eat}.local "${e.local}" does not exist (expected ${abs}).`); return; }
    let buf;
    try { buf = fs.readFileSync(abs); }
    catch (err) { EV(`${eat}.local "${e.local}" cannot be read: ${err.message}`); return; }
    if (buf.slice(0, 5).toString("latin1") === "%PDF-" || buf.slice(0, 4096).includes(0)) {
      W(`${eat}.local "${e.local}" is binary — the quote cannot be verified inside it. ` +
        `Save a UTF-8 text extract of the document and point .local at that file.`);
      return;
    }
    if (visibleText(buf.toString("utf8")).includes(q)) quotesChecked++;
    else
      EV(`${eat}.quote is NOT present in "${e.local}" — the saved page does not contain ` +
         `the sentence this claim rests on. Re-read the source: either quote it correctly ` +
         `or drop the claim. (This is the exact shape of a fabricated correction.)`);
  });
}
if (noEvidence.length)
  W(`${noEvidence.length}/${riskInsights} correcting/comparing insight(s) carry no evidence[] — ` +
    `unverifiable downstream: ${noEvidence.slice(0, 6).join(", ")}` +
    (noEvidence.length > 6 ? `, +${noEvidence.length - 6} more` : "") +
    `. Add evidence:[{srcId,quote,local}] with the cited page saved into the report.`);
for (const { ins, at } of allInsights) {
  if (!RISK_KINDS.has(ins.kind)) continue;
  const src = (isArr(ins.sources) ? ins.sources : []).filter(isStr).filter((s) => SRC_IDS.has(s));
  if (src.length && !src.some((s) => SRC_MEDIA.get(s)))
    W(`${at} rests on ${src.length} source(s) and NOT ONE was saved into the report ` +
      `(${src.join(", ")} all have empty .media) — nothing here can be re-checked after the run.`);
}

for (const [id, where] of referenced) {
  if (!SRC_IDS.has(id)) E(`broken citation [${id}] at ${where} — no research entry has id "${id}".`);
}

for (const [src, uses] of frameUses) {
  if (uses.length > 1) W(`frame "${src}" is shown ${uses.length}× (${uses.join(", ")}) — the renderer now de-dups it, but consider distinct frames.`);
}
const framed = [...frameUses.keys()].map((s) => ({ s, t: tsOf(s) })).filter((x) => x.t != null).sort((a, b) => a.t - b.t);
for (let i = 1; i < framed.length; i++) {
  if (framed[i].s !== framed[i - 1].s && Math.abs(framed[i].t - framed[i - 1].t) < 2)
    W(`near-duplicate frames <2s apart: "${framed[i - 1].s}" & "${framed[i].s}" — pick one.`);
}

for (const [rel, uses] of localAssets) {
  const abs = nodePath.resolve(reportRoot, rel);
  if (!fs.existsSync(abs))
    E(`missing local asset "${rel}" (used at ${uses.join(", ")}) — expected at ${abs}. ` +
      `Persist the file into the report or fix the path (remote URLs must be downloaded in).`);
}

warns.forEach((w) => console.warn("  ⚠ " + w));
if (errors.length) {
  console.error(`\n✗ ${errors.length} error(s) in ${path}:`);
  errors.forEach((e) => console.error("  ✗ " + e));
  process.exit(1);
}
console.log(`✓ ${path} valid — ${segs.length} segments, ${research.length} sources, ${referenced.size} citations resolved, ` +
  `${anchors.length} anchors (${anchorRefs} back-refs), ${localAssets.size} local asset(s) found, ` +
  `${riskWithEvidence}/${riskInsights} correcting insight(s) evidenced (${quotesChecked} quote(s) verified)` +
  (warns.length ? `, ${warns.length} warning(s)` : ""));
process.exit(0);
