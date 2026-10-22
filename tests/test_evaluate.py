"""Public evaluation contracts, without API calls or browser dependencies."""

from __future__ import annotations

import copy
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


RELEASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RELEASE))
MODULE_SPEC = importlib.util.spec_from_file_location(
    "release_evaluate", RELEASE / "evaluate.py"
)
evaluate = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(evaluate)
from omnidr_eval import engine  # noqa: E402


FIRST = "OmniDR-Bench-v0-0001"
SECOND = "OmniDR-Bench-v0-0002"
MODEL = "example-model"


def judgment(atoms=None, score=80.0):
    atoms = copy.deepcopy(
        atoms
        if atoms is not None
        else [{"id": "A1-01", "check": "J", "score": 1, "reason": "Supported."}]
    )
    return {
        "model": "example-judge",
        "n_atoms": len(atoms),
        "n_scored": sum(atom.get("score") is not None for atom in atoms),
        "n_skipped": sum(atom.get("score") is None for atom in atoms),
        "n_errored": sum(
            str(atom.get("reason", "")).startswith("ERROR:") for atom in atoms
        ),
        "n_na_propagated": sum(bool(atom.get("na_propagated")) for atom in atoms),
        "aggregate": {
            "total_score": score,
            "base_score": score,
            "dimension_scores": {name: score / 100 for name in evaluate.DIMENSIONS},
            "gate_pass": True,
        },
        "directions": [{"id": "A1", "atoms": atoms}],
    }


class EvaluationContractTests(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        temporary = self.context.enter_context(
            tempfile.TemporaryDirectory(
                prefix="evaluation-test-", dir=Path(__file__).parent
            )
        )
        self.base = Path(temporary)
        self.data = self.base / "data"
        self.reports = self.base / "reports"
        self.output = self.base / "results"
        self.records = {}
        self.commands = []
        self.results = []
        (self.reports / MODEL).mkdir(parents=True)
        self.add_task(FIRST)
        self.make_report(FIRST)

        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *args, **kwargs: None
        playwright = types.ModuleType("playwright")
        playwright.__path__ = []
        playwright.sync_api = types.ModuleType("playwright.sync_api")
        self.context.enter_context(
            patch.dict(
                sys.modules,
                {
                    "dotenv": dotenv,
                    "playwright": playwright,
                    "playwright.sync_api": playwright.sync_api,
                },
            )
        )
        self.context.enter_context(
            patch.dict(
                os.environ,
                {
                    "OPENAI_BASE_URL": "https://api.example.invalid/v1",
                    "OPENAI_API_KEY": "unit-test-placeholder",
                },
                clear=True,
            )
        )
        self.context.enter_context(
            patch.object(evaluate.shutil, "which", return_value="node")
        )
        self.context.enter_context(
            patch.object(evaluate, "inspect_report", return_value=None)
        )
        self.runner = self.context.enter_context(
            patch.object(evaluate, "run_process", side_effect=self.fake_process)
        )

    def add_task(self, iid, query="Explain the evidence in this video."):
        self.records[iid] = {
            "id": iid,
            "video_url": "https://www.youtube.com/watch?v=<VIDEO_ID>",
            "query": query,
            "rubric": {
                "dimensions": {"content": 0.5, "citation": 0.3, "presentation": 0.2},
                "directions": [
                    {
                        "id": "A1",
                        "ref": "C-IF",
                        "name": "Instruction following",
                        "dimension": "content",
                        "tier": "gate",
                        "logic": "all",
                        "atoms": [
                            {
                                "id": "A1-01",
                                "cid": "Q0001",
                                "check": "J",
                                "text": "Does the report answer the query?",
                                "expected": "yes",
                            }
                        ],
                    }
                ],
            },
        }
        self.save_dataset()

    def save_dataset(self):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "OmniDR-Bench.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in self.records.values()),
            encoding="utf-8",
        )

    def make_report(self, iid):
        report = self.reports / MODEL / iid
        (report / "js").mkdir(parents=True)
        (report / "index.html").write_text(
            "<html><body>Report</body></html>", encoding="utf-8"
        )
        (report / "js" / "data.js").write_text(
            'window.DATA = {summary: "An evidence-based explanation."};',
            encoding="utf-8",
        )
        return report

    def fake_process(self, command, log, timeout):
        self.commands.append(list(command))
        destination = Path(command[command.index("--out") + 1])
        if "omnidr_eval.render_probe" in command:
            evaluate.write_json(destination, {"ok": True, "probe_version": 2})
        elif "omnidr_eval.engine" in command:
            evaluate.write_json(
                destination, self.results.pop(0) if self.results else judgment()
            )
        else:
            self.fail(f"Unexpected subprocess: {command}")
        return 0

    def run_cli(self, *options, output=None):
        arguments = [
            "--reports-dir",
            str(self.reports),
            "--data-dir",
            str(self.data),
            "--output-dir",
            str(output or self.output),
            "--env-file",
            str(self.base / "unused.env"),
            "--evaluation-date",
            "2026-01-02",
        ]
        stream = io.StringIO()
        with redirect_stdout(stream), redirect_stderr(stream):
            code = evaluate.main(arguments + list(options))
        return code, stream.getvalue()

    def summary(self, output=None):
        return evaluate.read_json((output or self.output) / "summary.json")

    def test_load_dataset_selects_public_ids_and_rejects_empty_query(self):
        self.add_task(SECOND)
        tasks, ids = evaluate.load_dataset(self.data, [SECOND])
        self.assertEqual(list(tasks), [SECOND])
        self.assertEqual(ids, {FIRST, SECOND})
        self.assertEqual(tasks[SECOND]["rubric"], self.records[SECOND]["rubric"])
        self.records[SECOND]["query"] = " "
        self.save_dataset()
        with self.assertRaisesRegex(ValueError, "Empty or invalid query"):
            evaluate.load_dataset(self.data, [SECOND])

    def test_dataset_rejects_duplicates_malformed_lines_and_nonpublic_video_urls(self):
        path = self.data / "OmniDR-Bench.jsonl"
        original = path.read_text()
        for text, message in (
            (original + original, "Duplicate instance ID"),
            (original + "{broken}\n", "Invalid JSON"),
        ):
            with self.subTest(message=message):
                path.write_text(text)
                with self.assertRaisesRegex(ValueError, message):
                    evaluate.load_dataset(self.data)
        self.records[FIRST]["video_url"] = "file:///videos/example.mp4"
        self.save_dataset()
        with self.assertRaisesRegex(ValueError, "public YouTube or Bilibili"):
            evaluate.load_dataset(self.data)

    def test_per_task_rubric_reaches_the_engine(self):
        tasks, _ = evaluate.load_dataset(self.data)
        self.assertEqual(tasks[FIRST]["rubric"], self.records[FIRST]["rubric"])
        self.assertEqual(self.run_cli()[0], 0)
        command = next(c for c in self.commands if "omnidr_eval.engine" in c)
        rubric_path = Path(command[command.index("--rubrics") + 1])
        self.assertTrue(rubric_path.is_relative_to(self.output / ".cache"))
        self.assertEqual(
            evaluate.read_json(rubric_path),
            {
                **self.records[FIRST]["rubric"],
                "task_id": FIRST,
                "query": self.records[FIRST]["query"],
            },
        )

    def test_other_task_edits_do_not_invalidate_selected_task_cache(self):
        self.add_task(SECOND)
        self.assertEqual(self.run_cli("--instances", FIRST)[0], 0)
        self.add_task(SECOND, "A revised question for another task.")
        self.commands.clear()
        self.assertEqual(self.run_cli("--instances", FIRST)[0], 0)
        self.assertEqual(self.commands, [])

    def test_validate_only_needs_no_credentials_browser_or_output(self):
        os.environ.pop("OPENAI_API_KEY")
        os.environ.pop("OPENAI_BASE_URL")
        code, _ = self.run_cli("--validate-only", "--instances", FIRST)
        self.assertEqual(code, 0)
        self.runner.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_judge_defaults_and_optional_cli_settings_reach_the_engine(self):
        self.assertEqual(self.run_cli()[0], 0)
        command = next(c for c in self.commands if "omnidr_eval.engine" in c)
        self.assertEqual(
            command[command.index("--vision-model") + 1],
            command[command.index("--model") + 1],
        )
        self.commands.clear()
        self.assertEqual(
            self.run_cli(
                "--vision-model", "example-vision", "--evaluation-date", "2026-03-01"
            )[0],
            0,
        )
        command = next(c for c in self.commands if "omnidr_eval.engine" in c)
        self.assertEqual(command[command.index("--vision-model") + 1], "example-vision")
        self.assertEqual(command[command.index("--evaluation-date") + 1], "2026-03-01")
        self.assertNotIn("--rescore", command)

    def test_missing_report_is_zero_in_the_full_benchmark_denominator(self):
        self.add_task(SECOND)
        code, _ = self.run_cli()
        self.assertEqual(code, 0)
        summary = self.summary()
        overall = summary["by_model"][MODEL]
        self.assertEqual(overall["n_total"], 2)
        self.assertEqual(overall["n_judged"], 1)
        self.assertEqual(overall["total_score"]["mean"], 40.0)
        self.assertEqual(overall["judged_only"]["total_score"]["mean"], 80.0)
        missing = next(
            row for row in summary["per_instance"] if row["instance_id"] == SECOND
        )
        self.assertEqual(
            (missing["status"], missing["total_score"]), ("missing_report", 0.0)
        )

    def test_partial_modes_are_labeled_and_select_the_right_runtime_flags(self):
        for flag in ("--no-llm", "--skip-render"):
            with self.subTest(flag=flag):
                self.commands.clear()
                output = self.base / flag.removeprefix("--")
                code, _ = self.run_cli(flag, output=output)
                self.assertEqual(code, 0)
                summary = self.summary(output)
                self.assertEqual(summary["mode"], "partial")
                self.assertEqual(summary["per_instance"][0]["status"], "partial")
                self.assertFalse(summary["by_model"][MODEL]["evaluation_complete"])
                command = next(c for c in self.commands if "omnidr_eval.engine" in c)
                if flag == "--no-llm":
                    self.assertIn("--no-llm", command)
                else:
                    self.assertIn("--skip-vision", command)
                    self.assertFalse(
                        any("omnidr_eval.render_probe" in c for c in self.commands)
                    )

    def test_unresolved_judgments_are_incomplete_but_propagated_na_is_excluded(self):
        result = judgment(
            [
                {
                    "id": "A1-01",
                    "check": "J",
                    "score": None,
                    "reason": "Unparseable answer.",
                },
                {"id": "A1-02", "check": "J", "score": None, "reason": "Tied votes."},
                {"id": "C1-01", "check": "J", "score": None, "na_propagated": True},
                {
                    "id": "C2-01",
                    "check": "M",
                    "score": None,
                    "reason": "No applicable date window.",
                },
            ]
        )
        self.assertEqual(evaluate.unresolved_ids(result), ["A1-01", "A1-02"])
        row = evaluate.score_record(MODEL, FIRST, result, partial=False)
        self.assertEqual(
            (row["status"], row["n_errored"], row["n_unresolved"]), ("incomplete", 0, 2)
        )
        self.assertFalse(evaluate.summarize([row])["evaluation_complete"])

    def test_matching_complete_cache_reuses_scores_without_subprocesses(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.commands.clear()
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.commands, [])
        self.assertTrue(self.summary()["per_instance"][0]["cached"])

    def test_resume_retries_unresolved_j_and_preserves_valid_and_na_judgments(self):
        self.results = [
            judgment(
                [
                    {
                        "id": "A1-01",
                        "check": "J",
                        "score": None,
                        "reason": "Tied votes.",
                    },
                    {"id": "A1-02", "check": "J", "score": 1},
                    {"id": "C1-01", "check": "J", "score": None, "na_propagated": True},
                ]
            )
        ]
        self.assertEqual(self.run_cli()[0], 1)
        self.commands.clear()
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertIn("--rescore", command)
        self.assertIn("--rescore-partial", command)
        self.assertEqual(command[command.index("--invalidate") + 1], "A1-01")
        self.assertEqual(self.summary()["per_instance"][0]["status"], "scored")

    def test_changed_query_report_or_rubric_invalidates_the_complete_cache(self):
        for change in ("query", "report", "rubric", "video_url"):
            with self.subTest(change=change):
                output = self.base / ("changed-" + change)
                self.assertEqual(self.run_cli(output=output)[0], 0)
                if change == "query":
                    self.add_task(FIRST, "Explain a revised research question.")
                elif change == "report":
                    (self.reports / MODEL / FIRST / "index.html").write_text(
                        "<html><body>Revised report</body></html>", encoding="utf-8"
                    )
                elif change == "rubric":
                    self.records[FIRST]["rubric"]["directions"][0]["atoms"][0][
                        "text"
                    ] = "Does the report explain the evidence?"
                    self.save_dataset()
                else:
                    self.records[FIRST][
                        "video_url"
                    ] = "https://www.bilibili.com/video/<VIDEO_ID>"
                    self.save_dataset()
                self.commands.clear()
                self.assertEqual(self.run_cli(output=output)[0], 0)
                self.assertEqual(len(self.commands), 2)
                command = next(c for c in self.commands if "omnidr_eval.engine" in c)
                self.assertNotIn("--rescore", command)
                self.assertFalse(self.summary(output)["per_instance"][0]["cached"])

    def test_machine_error_retry_keeps_successful_judge_cache(self):
        self.results = [
            judgment(
                [
                    {"id": "A1-01", "check": "J", "score": 1},
                    {
                        "id": "P1-01",
                        "check": "M",
                        "score": None,
                        "reason": "ERROR: transient failure",
                    },
                ]
            )
        ]
        self.assertEqual(self.run_cli()[0], 1)
        self.commands.clear()
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(len(self.commands), 1)
        self.assertIn("--rescore", self.commands[0])
        self.assertNotIn("--invalidate", self.commands[0])

    def test_linked_directory_content_changes_invalidate_cache(self):
        folder = self.reports / MODEL / FIRST
        shared = self.base / "shared-js"
        (folder / "js").rename(shared)
        (folder / "js").symlink_to(shared, target_is_directory=True)
        self.assertEqual(self.run_cli()[0], 0)
        (shared / "data.js").write_text(
            'window.DATA = {summary: "Changed."};', encoding="utf-8"
        )
        self.commands.clear()
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(len(self.commands), 2)
        self.assertNotIn("--rescore", self.commands[-1])

    def test_cyclic_report_link_returns_failure_without_hanging(self):
        folder = self.reports / MODEL / FIRST
        (folder / "cycle").symlink_to(folder, target_is_directory=True)
        self.assertEqual(self.run_cli()[0], 1)
        row = self.summary()["per_instance"][0]
        self.assertEqual(row["status"], "evaluation_failed")
        self.assertIn("Cyclic directory", row["reason"])
        self.assertFalse(self.commands)

    def test_malformed_direction_is_an_input_error(self):
        self.records[FIRST]["rubric"]["directions"] = ["invalid"]
        self.save_dataset()
        code, message = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("Invalid direction", message)
        self.assertFalse(self.commands)

    def test_evaluation_date_and_evidence_policy_invalidate_the_cache(self):
        self.assertEqual(self.run_cli()[0], 0)
        for options, policy in (
            (["--evaluation-date", "2026-01-03"], ""),
            ([], "hard"),
        ):
            with self.subTest(options=options, policy=policy):
                self.commands.clear()
                os.environ["VALIDATE_EVIDENCE"] = policy
                self.assertEqual(self.run_cli(*options)[0], 0)
                self.assertEqual(len(self.commands), 2)
                self.assertNotIn("--rescore", self.commands[-1])

    def test_symlink_outputs_are_rejected_before_writing_to_inputs(self):
        original = (self.reports / MODEL / FIRST / "index.html").read_bytes()
        alias = self.base / "output-alias"
        alias.symlink_to(self.reports / MODEL / FIRST, target_is_directory=True)
        self.assertEqual(self.run_cli(output=alias)[0], 2)
        self.output.mkdir()
        (self.output / "unsafe-cache").symlink_to(
            self.reports, target_is_directory=True
        )
        self.assertEqual(self.run_cli()[0], 2)
        self.runner.assert_not_called()
        self.assertEqual(
            (self.reports / MODEL / FIRST / "index.html").read_bytes(), original
        )
        self.assertFalse((self.reports / MODEL / FIRST / "summary.json").exists())

    def test_report_symlink_target_cannot_overlap_the_output(self):
        report = self.reports / MODEL / FIRST
        self.output.mkdir()
        target = self.output / "submitted-report"
        report.rename(target)
        report.symlink_to(target, target_is_directory=True)
        code, message = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("overlaps", message)
        self.runner.assert_not_called()
        self.assertFalse((self.output / "summary.json").exists())

    def test_engine_partial_visual_paths_make_no_model_or_screenshot_calls(self):
        atom = {
            "id": "P3-01",
            "check": "J",
            "template": "aesthetic",
            "text": "Is text legible?",
        }
        with patch.object(engine, "_SKIP_VISION", True), patch.object(
            engine, "ensure_report_shot"
        ) as screenshot, patch.object(engine, "_chat") as chat:
            result = engine.eval_atom(
                atom, {}, [], {}, str(self.reports), "", "example-judge", False
            )
            self.assertEqual(result["reason"], "Visual judging disabled")
            result = engine.eval_atom(
                atom, {}, [], {}, str(self.reports), "", "example-judge", True
            )
            self.assertEqual(result["reason"], "LLM disabled")
            self.assertIsNone(result["score"])
            screenshot.assert_not_called()
            chat.assert_not_called()


class JudgeAnswerTests(unittest.TestCase):
    def test_binary_json_answers_preserve_both_polarities(self):
        for answer, decision in (
            (True, "yes"),
            (False, "no"),
            (1, "yes"),
            (0, "no"),
            ("yes", "yes"),
            ("no", "no"),
        ):
            for expected in ("yes", "no"):
                with self.subTest(answer=answer, expected=expected):
                    response = {"answer": answer, "reason": "Checked the report."}
                    result = engine._normalize(
                        response, {"expected": expected}, json.dumps(response)
                    )
                    self.assertEqual(result["answer"], decision)
                    self.assertEqual(result["score"], int(decision == expected))

    def test_missing_and_unrecognized_answers_remain_unresolved(self):
        for answer in (None, "", "uncertain"):
            with self.subTest(answer=answer):
                response = {"answer": answer}
                result = engine._normalize(
                    response, {"expected": "yes"}, json.dumps(response)
                )
                self.assertIsNone(result["answer"])
                self.assertIsNone(result["score"])


if __name__ == "__main__":
    unittest.main()
