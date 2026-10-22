#!/usr/bin/env python3
"""Evaluate report folders against the released OmniDR-Bench tasks."""
from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent
DIMENSIONS = ("content", "citation", "presentation")


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def load_dataset(data_dir, selected=None):
    path = data_dir / "OmniDR-Bench.jsonl"
    records = {}
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSON at {path}:{line_number}: {error.msg}"
                ) from error
            if not isinstance(record, dict) or set(record) != {
                "id",
                "video_url",
                "query",
                "rubric",
            }:
                raise ValueError(
                    f"Expected id, video_url, query, and rubric at {path}:{line_number}."
                )
            iid = record["id"]
            if not isinstance(iid, str) or not re.fullmatch(
                r"OmniDR-Bench-v0-\d{4}", iid
            ):
                raise ValueError(f"Invalid instance ID at {path}:{line_number}.")
            if iid in records:
                raise ValueError(
                    f"Duplicate instance ID {iid} at {path}:{line_number}."
                )
            records[iid] = record
    if not records:
        raise ValueError(f"No tasks in {path}.")
    tasks = {}
    for iid, record in records.items():
        query = record["query"]
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"Empty or invalid query for {iid}.")
        url = record["video_url"]
        if not isinstance(url, str):
            raise ValueError(f"Invalid video_url for {iid}.")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname
            not in {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
                "youtu.be",
                "bilibili.com",
                "www.bilibili.com",
            }
            or parsed.username
            or parsed.password
            or not parsed.path.strip("/")
        ):
            raise ValueError(
                f"video_url must be a public YouTube or Bilibili HTTPS page for {iid}."
            )
        rubric = record["rubric"]
        if not isinstance(rubric, dict):
            raise ValueError(f"Rubric must be an object for {iid}.")
        directions = rubric.get("directions")
        if not isinstance(directions, list) or not directions:
            raise ValueError(f"No rubric directions for {iid}.")
        if any(
            not isinstance(d, dict)
            or not isinstance(d.get("id"), str)
            or not isinstance(d.get("atoms"), list)
            for d in directions
        ):
            raise ValueError(f"Invalid direction definition for {iid}.")
        if len({d["id"] for d in directions}) != len(directions):
            raise ValueError(f"Duplicate direction IDs for {iid}.")
        atoms = [a for d in directions for a in d.get("atoms", [])]
        if any(
            not isinstance(a, dict)
            or not isinstance(a.get("id"), str)
            or not isinstance(a.get("text"), str)
            for a in atoms
        ):
            raise ValueError(f"Invalid atom definition for {iid}.")
        if not atoms or len({a["id"] for a in atoms}) != len(atoms):
            raise ValueError(f"Missing or duplicate atom IDs for {iid}.")
        tasks[iid] = {"query": query, "video_url": url, "rubric": rubric}
    ids = sorted(set(selected)) if selected else sorted(tasks)
    unknown = set(ids) - tasks.keys()
    if unknown:
        raise ValueError("Unknown instance IDs: " + ", ".join(sorted(unknown)))
    return {iid: tasks[iid] for iid in ids}, set(tasks)


def find_report(model_dir, iid):
    candidates = []
    folder = model_dir / iid
    for candidate in (folder, folder / "report"):
        if (candidate / "index.html").is_file():
            candidates.append(candidate)
    if len(candidates) > 1:
        raise ValueError(
            f"Ambiguous reports for {model_dir.name}/{iid}: keep one report folder."
        )
    if candidates:
        return candidates[0].resolve()
    if (model_dir / iid).exists():
        return (
            None,
            "invalid_report",
            "The instance folder does not contain index.html.",
        )
    return None, "missing_report", "No report was submitted for this task."


def inspect_report(report):
    for relative in ("index.html", "js/data.js"):
        path = report / relative
        if not path.is_file() or path.stat().st_size == 0:
            return f"Missing or empty {relative}."
    process = subprocess.run(
        ["node", str(ROOT / "omnidr_eval" / "load_report.mjs"), str(report)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        parsed = json.loads(process.stdout)
    except ValueError:
        return "The report loader could not parse js/data.js."
    if process.returncode or not parsed.get("ok"):
        return (
            "Cannot load report data: "
            + str(parsed.get("error", "Node failed")).splitlines()[0]
        )
    data = parsed.get("data")
    if not isinstance(data, dict):
        return "window.DATA must be an object."
    for key in ("segments", "research", "insights"):
        if key in data and not isinstance(data[key], list):
            return f"DATA.{key} must be an array."
    if (
        not data.get("segments")
        and not data.get("summary")
        and not data.get("insights")
    ):
        return "The report contains no segments, summary, or insights."
    return None


def hash_file(path, digest):
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)


def fingerprint(report, task, settings):
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode())
    digest.update(json.dumps(task, sort_keys=True, ensure_ascii=False).encode())
    for source in sorted((ROOT / "omnidr_eval").iterdir()):
        if source.suffix in (".py", ".mjs"):
            digest.update(source.name.encode())
            hash_file(source, digest)

    def visit(directory, ancestors):
        resolved = directory.resolve()
        if resolved in ancestors:
            raise ValueError(f"Cyclic directory link in report: {directory}")
        for path in sorted(directory.iterdir()):
            digest.update(path.relative_to(report).as_posix().encode() + b"\0")
            if path.is_dir():
                visit(path, ancestors | {resolved})
            elif path.is_file():
                digest.update(str(path.stat().st_size).encode() + b"\0")
                hash_file(path, digest)
            else:
                raise ValueError(f"Broken link or unsupported file in report: {path}")

    visit(report, set())
    return digest.hexdigest()


def empty_result(model, iid, status, reason):
    return {
        "model": model,
        "instance_id": iid,
        "status": status,
        "reason": reason,
        "judged": False,
        "total_score": 0.0,
        "base_score": 0.0,
        "dimension_scores": {d: 0.0 for d in DIMENSIONS},
        "gate_pass": False,
        "n_atoms": 0,
        "n_scored": 0,
        "n_skipped": 0,
        "n_errored": 0,
        "n_unresolved": 0,
    }


def unresolved_ids(result):
    return [
        atom["id"]
        for direction in result.get("directions", [])
        for atom in direction.get("atoms", [])
        if atom.get("check", "J") == "J"
        and atom.get("score") is None
        and not atom.get("na_propagated")
    ]


def score_record(model, iid, result, partial, probe_ok=True):
    aggregate = result["aggregate"]
    total = aggregate.get("total_score")
    if not isinstance(total, (int, float)) or not math.isfinite(total):
        raise ValueError("The evaluator did not return a finite total score.")
    errors = int(result.get("n_errored", 0))
    unresolved = len(unresolved_ids(result))
    status = (
        "partial"
        if partial
        else ("scored" if errors == 0 and not unresolved and probe_ok else "incomplete")
    )
    return {
        "model": model,
        "instance_id": iid,
        "status": status,
        "judged": True,
        "total_score": total,
        "base_score": aggregate.get("base_score", total),
        "dimension_scores": aggregate.get("dimension_scores", {}),
        "gate_pass": bool(aggregate.get("gate_pass")),
        "n_unresolved": unresolved,
        **{
            key: int(result.get(key, 0))
            for key in ("n_atoms", "n_scored", "n_skipped", "n_errored")
        },
    }


def run_process(command, log, timeout):
    with log.open("a", encoding="utf-8") as stream:
        process = subprocess.run(
            command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout
        )
    return process.returncode


def evaluate_one(model, iid, report, task, args, settings):
    output = args.output_dir / model / (iid + ".json")
    cache = args.output_dir / ".cache" / model / iid
    metadata = cache / "run.json"
    log = cache / "evaluation.log"
    started = time.monotonic()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        identity = fingerprint(report, task, settings)
        cached = None
        old_meta = read_json(metadata) if metadata.exists() else {}
        if (
            not args.force
            and output.exists()
            and old_meta.get("fingerprint") == identity
        ):
            cached = read_json(output)
            if (
                not cached.get("n_errored")
                and old_meta.get("probe_ok")
                and (args.no_llm or not unresolved_ids(cached))
            ):
                row = score_record(model, iid, cached, args.no_llm or args.skip_render)
                row.update(cached=True, seconds=0.0)
                return row

        probe_path = cache / "render_probe.json"
        probe_ok = False
        command = [
            sys.executable,
            "-m",
            "omnidr_eval.render_probe",
            "--report",
            str(report),
            "--out",
            str(probe_path),
            "--label",
            iid,
        ]
        if not args.skip_render:
            if cached is not None and old_meta.get("probe_ok") and probe_path.exists():
                probe_ok = bool(read_json(probe_path).get("ok"))
            else:
                code = run_process(command, log, args.timeout)
                probe_ok = (
                    code == 0
                    and probe_path.exists()
                    and bool(read_json(probe_path).get("ok"))
                )
            if not probe_ok:
                return empty_result(
                    model, iid, "evaluation_failed", f"Browser probe failed; see {log}."
                )
        else:
            probe_path = cache / "render_probe.disabled.json"
            write_json(
                probe_path,
                {"ok": False, "error": "Browser checks were explicitly disabled."},
            )

        query_path = cache / "query.txt"
        query_path.write_text(task["query"] + "\n", encoding="utf-8")
        rubric_path = cache / "rubric.json"
        write_json(
            rubric_path, {**task["rubric"], "task_id": iid, "query": task["query"]}
        )
        pending = cache / "scores.pending.json"
        command = [
            sys.executable,
            "-m",
            "omnidr_eval.engine",
            "--report",
            str(report),
            "--rubrics",
            str(rubric_path),
            "--query",
            str(query_path),
            "--out",
            str(pending),
            "--model",
            args.judge_model,
            "--vision-model",
            settings["vision_model"],
            "--evaluation-date",
            settings["evaluation_date"],
            "--workers",
            str(args.workers),
            "--gate-votes",
            str(args.gate_votes),
            "--cache-dir",
            str(cache),
            "--probe",
            str(probe_path),
        ]
        if args.no_llm:
            command.append("--no-llm")
        if args.skip_render:
            command.append("--skip-vision")
        if cached is not None:
            failed = unresolved_ids(cached)
            command += ["--rescore", str(output), "--rescore-partial"]
            if failed:
                command += ["--invalidate", ",".join(failed)]
        code = run_process(command, log, args.timeout)
        if code or not pending.exists():
            return empty_result(
                model, iid, "evaluation_failed", f"Scoring failed; see {log}."
            )
        result = read_json(pending)
        row = score_record(
            model, iid, result, args.no_llm or args.skip_render, probe_ok
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        pending.replace(output)
        write_json(
            metadata,
            {
                "fingerprint": identity,
                "probe_ok": probe_ok,
                "settings": settings,
                "report": str(report),
            },
        )
        row.update(cached=False, seconds=round(time.monotonic() - started, 2))
        return row
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        return empty_result(model, iid, "evaluation_failed", str(error))


def stats(values):
    if not values:
        return {key: None for key in ("mean", "median", "min", "max", "stdev")}
    return {
        "mean": round(statistics.mean(values), 4),
        "median": round(statistics.median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "stdev": round(statistics.pstdev(values), 4),
    }


def summarize(rows, partial=False):
    judged = [row for row in rows if row["judged"]]
    dimensions = lambda subset: {
        d: stats([(r["dimension_scores"].get(d) or 0) * 100 for r in subset])
        for d in DIMENSIONS
    }
    return {
        "n_total": len(rows),
        "n_judged": len(judged),
        "n_unjudged": len(rows) - len(judged),
        "success_rate": len(judged) / len(rows) if rows else 0.0,
        "evaluation_complete": not partial
        and not any(r["status"] in ("incomplete", "evaluation_failed") for r in rows),
        "n_errored": sum(r["n_errored"] for r in rows),
        "n_unresolved": sum(r["n_unresolved"] for r in rows),
        "n_skipped": sum(r["n_skipped"] for r in rows),
        "total_score": stats([r["total_score"] for r in rows]),
        "dimension_scores": dimensions(rows),
        "judged_only": {
            "total_score": stats([r["total_score"] for r in judged]),
            "dimension_scores": dimensions(judged),
        },
        "gate_pass_rate": (
            sum(r["gate_pass"] for r in judged) / len(judged) if judged else 0.0
        ),
    }


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reports-dir", type=Path, default=Path("reports"))
    ap.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="User-managed directory containing OmniDR-Bench.jsonl from Hugging Face.",
    )
    ap.add_argument("--output-dir", type=Path, default=Path("results"))
    ap.add_argument(
        "--models",
        nargs="+",
        help="Report folder names; defaults to every model folder.",
    )
    ap.add_argument(
        "--instances",
        nargs="+",
        help="Evaluate a subset; defaults to every released task.",
    )
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    ap.add_argument("--judge-model", help="OpenAI-compatible text judge model ID.")
    ap.add_argument(
        "--vision-model", help="Vision judge model ID; defaults to the text judge."
    )
    ap.add_argument(
        "--evaluation-date",
        help="Date for recency checks (YYYY-MM-DD); defaults to today.",
    )
    ap.add_argument(
        "--workers",
        type=positive_int,
        default=4,
        help="Concurrent judgments per report.",
    )
    ap.add_argument("--jobs", type=positive_int, default=1, help="Concurrent reports.")
    ap.add_argument("--gate-votes", type=positive_int, default=3)
    ap.add_argument(
        "--timeout",
        type=positive_int,
        default=7200,
        help="Timeout in seconds per subprocess.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Ignore cached judgments and evaluate again.",
    )
    ap.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate inputs without API or browser calls.",
    )
    ap.add_argument(
        "--no-llm",
        action="store_true",
        help="Run only machine checks; scores are partial.",
    )
    ap.add_argument(
        "--skip-render",
        action="store_true",
        help="Skip browser checks; scores are partial.",
    )
    return ap


def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
        args.judge_model = (
            args.judge_model or os.environ.get("OMNIDR_JUDGE_MODEL") or "gpt-5.6-sol"
        )
        vision = args.vision_model or args.judge_model
        os.environ["OMNIDR_JUDGE_MODEL"] = args.judge_model
        evaluation_date = args.evaluation_date or date.today().isoformat()
        date.fromisoformat(evaluation_date)
        args.reports_dir = args.reports_dir.expanduser().resolve()
        args.data_dir = args.data_dir.expanduser().resolve()
        output_path = args.output_dir.expanduser().absolute()
        if any(part.is_symlink() for part in (output_path, *output_path.parents)):
            raise ValueError(
                "Output directory and its parents must not be symbolic links."
            )
        args.output_dir = output_path.resolve()
        if args.output_dir.exists():
            for directory, dirs, files in os.walk(args.output_dir):
                if any((Path(directory) / name).is_symlink() for name in dirs + files):
                    raise ValueError(
                        "The output directory must not contain symbolic links."
                    )
        if not shutil.which("node"):
            raise ValueError(
                "Node.js 18+ is required. Install Node.js before running evaluation."
            )
        if not args.reports_dir.is_dir():
            raise ValueError(f"Report directory not found: {args.reports_dir}")
        tasks, all_ids = load_dataset(args.data_dir, args.instances)
        models = args.models or sorted(
            p.name
            for p in args.reports_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
        if not models or len(models) != len(set(models)):
            raise ValueError("Provide at least one distinct model folder.")
        for model in models:
            if (
                Path(model).name != model
                or model in (".", "..")
                or not (args.reports_dir / model).is_dir()
            ):
                raise ValueError(f"Invalid or missing model folder: {model}")
            unknown = [
                p.name
                for p in (args.reports_dir / model).iterdir()
                if p.is_dir() and not p.name.startswith(".") and p.name not in all_ids
            ]
            if unknown:
                raise ValueError(
                    f"Unknown instance folders in {model}: {', '.join(sorted(unknown))}"
                )
        for source in (args.reports_dir, args.data_dir):
            if (
                args.output_dir == source
                or source in args.output_dir.parents
                or args.output_dir in source.parents
            ):
                raise ValueError(
                    "Output directory must be separate from reports and benchmark data."
                )

        rows, jobs = [], []
        for model in models:
            for iid, task in tasks.items():
                found = find_report(args.reports_dir / model, iid)
                if isinstance(found, tuple):
                    _, status, reason = found
                    rows.append(empty_result(model, iid, status, reason))
                    continue
                if (
                    args.output_dir == found
                    or found in args.output_dir.parents
                    or args.output_dir in found.parents
                ):
                    raise ValueError(
                        f"Output directory overlaps the report at {found}."
                    )
                reason = inspect_report(found)
                if reason:
                    rows.append(empty_result(model, iid, "invalid_report", reason))
                else:
                    jobs.append((model, iid, found, task))
        calls = sum(
            (args.gate_votes if d.get("tier") == "gate" else 1)
            for _, _, _, task in jobs
            for d in task["rubric"]["directions"]
            for atom in d["atoms"]
            if atom.get("check", "J") == "J"
        )
        if args.no_llm:
            calls = 0
        print(
            f"{len(models)} models x {len(tasks)} tasks: {len(jobs)} readable, {len(rows)} missing/invalid."
        )
        print(
            f"Up to {calls} initial judge calls, excluding retries; workers={args.workers}, jobs={args.jobs}."
        )
        for row in rows:
            print(
                f"[{row['status']}] {row['model']}/{row['instance_id']}: {row['reason']}"
            )
        if args.validate_only:
            return 1 if rows else 0
        if jobs and not args.no_llm:
            if not os.environ.get("OPENAI_API_KEY") or not os.environ.get(
                "OPENAI_BASE_URL"
            ):
                raise ValueError(
                    "Set OPENAI_BASE_URL and OPENAI_API_KEY in .env or the environment."
                )
            endpoint = urlparse(os.environ["OPENAI_BASE_URL"])
            if endpoint.scheme not in ("http", "https") or not endpoint.netloc:
                raise ValueError("OPENAI_BASE_URL must be an HTTP(S) API base URL.")
        if jobs and not args.skip_render:
            import playwright.sync_api  # noqa: F401
        settings = {
            "judge_model": args.judge_model,
            "vision_model": vision,
            "gate_votes": args.gate_votes,
            "no_llm": args.no_llm,
            "skip_render": args.skip_render,
            "evaluation_date": evaluation_date,
            "validate_evidence": os.environ.get("VALIDATE_EVIDENCE", ""),
            "endpoint_hash": hashlib.sha256(
                os.environ.get("OPENAI_BASE_URL", "").encode()
            ).hexdigest(),
        }
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [
                pool.submit(evaluate_one, model, iid, report, task, args, settings)
                for model, iid, report, task in jobs
            ]
            for future in as_completed(futures):
                row = future.result()
                rows.append(row)
                print(
                    f"[{row['status']}] {row['model']}/{row['instance_id']}: "
                    f"{row['total_score']:.2f}, judge errors={row['n_errored']}",
                    flush=True,
                )
        rows.sort(key=lambda row: (row["model"], row["instance_id"]))
        partial = args.no_llm or args.skip_render
        by_model = {
            model: summarize([r for r in rows if r["model"] == model], partial)
            for model in models
        }
        summary = {
            "mode": "partial" if partial else "full",
            "n_tasks": len(tasks),
            "instance_ids": sorted(tasks),
            "settings": settings,
            "by_model": by_model,
            "per_instance": rows,
        }
        write_json(args.output_dir / "summary.json", summary)
        fields = [
            "model",
            "instance_id",
            "status",
            "total_score",
            *DIMENSIONS,
            "n_scored",
            "n_skipped",
            "n_errored",
            "n_unresolved",
        ]
        with (args.output_dir / "scores.csv").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: (
                            round((row["dimension_scores"].get(key) or 0) * 100, 2)
                            if key in DIMENSIONS
                            else row.get(key)
                        )
                        for key in fields
                    }
                )
        for model, result in by_model.items():
            print(
                f"{model}: Avg={result['total_score']['mean']:.2f}, "
                f"reports={result['n_judged']}/{result['n_total']}, "
                f"complete={result['evaluation_complete']}, judge errors={result['n_errored']}"
            )
        print(f"Results: {args.output_dir / 'summary.json'}")
        return (
            1
            if any(
                r["status"] in ("incomplete", "evaluation_failed") or r["n_errored"]
                for r in rows
            )
            else 0
        )
    except (
        OSError,
        ValueError,
        KeyError,
        ImportError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
