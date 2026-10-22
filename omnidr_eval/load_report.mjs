#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const reportDir = process.argv[2];
if (!reportDir) { console.error("usage: node load_report.mjs <report_dir>"); process.exit(2); }

function listFiles(dir) {
  const out = [];
  const walk = (d, rel) => {
    let ents = [];
    try { ents = fs.readdirSync(d, { withFileTypes: true }); } catch { return; }
    for (const e of ents) {
      const rp = rel ? path.join(rel, e.name) : e.name;
      if (e.isDirectory()) walk(path.join(d, e.name), rp);
      else {
        let size = 0; try { size = fs.statSync(path.join(d, e.name)).size; } catch {}
        out.push({ path: rp, size });
      }
    }
  };
  walk(dir, "");
  return out;
}

function collectText(data) {
  const parts = [];
  const citeIds = new Set();
  const scanCites = (s) => { if (typeof s === "string") for (const m of s.matchAll(/\[S(\d+)\]/g)) citeIds.add("S" + m[1]); };
  const pushText = (label, s) => { if (typeof s === "string" && s.trim()) { parts.push(`[${label}] ${s}`); scanCites(s); } };

  if (data.meta) parts.push(`[meta] theme=${data.meta.theme} category=${data.meta.category} lang=${data.meta.lang}`);
  pushText("summary", data.summary);
  if (data.video) pushText("video.title", data.video.title);

  for (const seg of data.segments || []) {
    pushText(`segment.${seg.id}.title`, seg.title);
    for (const b of seg.caption || []) {
      if (b.type === "text") pushText(`segment.${seg.id}.text`, b.text);
      else if (b.type === "image") pushText(`segment.${seg.id}.imgcap`, b.cap || "");
      else if (b.type === "flow") pushText(`segment.${seg.id}.flow`, `${b.title || ""}: ${(b.steps || []).join(" -> ")}`);
      else if (b.type === "mindmap") pushText(`segment.${seg.id}.mindmap`, JSON.stringify(b));
      else if (b.type === "svg") parts.push(`[segment.${seg.id}.svg] <svg diagram>`);
    }
    for (const ins of seg.insights || []) {
      pushText(`segment.${seg.id}.insight.${ins.kind}`, [ins.title, ins.body, ins.videoClaim, ins.currentFact].filter(Boolean).join(" | "));
    }
  }
  for (const ins of data.insights || []) {
    let t = [ins.title, ins.body, ins.videoClaim, ins.currentFact].filter(Boolean).join(" | ");
    if (ins.table) {
      const cellsOf = (r) => Array.isArray(r) ? r : (r && typeof r === "object" ? Object.values(r) : [r]);
      t += " | TABLE cols=" + (ins.table.cols || []).join(",") + " rows=[" +
        (ins.table.rows || []).map((r) => cellsOf(r).join("/")).join(" ; ") + "]";
    }
    pushText(`insight.${ins.kind}`, t);
  }
  for (const r of data.research || []) {
    pushText(`research.${r.id}.${r.type}`, `${r.title} — ${r.summary} <${r.url}>`);
  }
  return { flatText: parts.join("\n"), citeIds: [...citeIds] };
}

const result = { ok: false, data: null, files: [], toolsUsed: "", flatText: "", citeIds: [], researchIds: [], error: null };
try {
  result.files = listFiles(reportDir);
  const tu = path.join(reportDir, "TOOLS_USED.md");
  if (fs.existsSync(tu)) result.toolsUsed = fs.readFileSync(tu, "utf8");

  const dataPath = path.join(reportDir, "js", "data.js");
  const code = fs.readFileSync(dataPath, "utf8");
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
  vm.runInContext(code, sandbox, { timeout: 5000 });
  const serialized = vm.runInContext(
    "JSON.stringify(window.DATA || globalThis.DATA)", sandbox, { timeout: 5000 }
  );
  if (typeof serialized !== "string") throw new Error("no serializable window.DATA assigned by data.js");
  const data = JSON.parse(serialized);
  if (!data) throw new Error("no window.DATA assigned by data.js");
  result.data = data;
  result.researchIds = (data.research || []).map((r) => r.id);
  const { flatText, citeIds } = collectText(data);
  result.flatText = flatText;
  result.citeIds = citeIds;
  result.ok = true;
} catch (e) {
  result.error = String(e && e.stack || e);
}
process.stdout.write(JSON.stringify(result));
