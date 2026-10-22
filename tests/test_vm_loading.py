"""Exercise the Node data loaders without accessing credentials or starting a browser."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DATA = {
    "summary": "Fixture summary",
    "video": {"title": "Fixture video", "duration": 2},
    "segments": [
        {
            "id": "one",
            "start": 0,
            "end": 2,
            "title": "One",
            "caption": [{"type": "text", "text": "Fixture observation."}],
        }
    ],
    "anchors": [
        {
            "id": "A1",
            "label": "Fixture topic",
            "category": "background",
            "why": "Fixture",
            "segmentIds": ["one"],
        }
    ],
    "research": [],
    "insights": [],
}


class ReportDataContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("Node.js is required for the data-loader tests.")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="omnidr_vm_test_")
        self.addCleanup(temporary.cleanup)
        self.report = Path(temporary.name)
        (self.report / "js").mkdir()
        (self.report / "index.html").write_text(
            "<!doctype html><title>Data-loader fixture</title>", encoding="utf-8"
        )
        self.data_file = self.report / "js" / "data.js"

    def write_report(self, extra):
        self.data_file.write_text(
            "window.DATA = " + json.dumps(DATA) + ";\n" + extra, encoding="utf-8"
        )

    def run_node(self, name):
        argument = self.report if name == "load_report.mjs" else self.data_file
        # Do not pass API keys, NODE_OPTIONS, or other caller configuration to probes.
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if key in os.environ
        }
        return subprocess.run(
            [self.node, str(ROOT / "omnidr_eval" / name), str(argument)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            env=environment,
        )

    def load_data(self):
        result = self.run_node("load_report.mjs")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"], payload.get("error"))
        return payload["data"]

    def assert_validator_accepts(self):
        result = self.run_node("validate_data.mjs")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dynamic_window_data_remains_supported(self):
        self.write_report(
            """
const double = value => value * 2;
window.DATA.summary = [2, 3].map(double).join(",");
console.log("A report diagnostic must not corrupt loader JSON.");
"""
        )
        self.assertEqual(self.load_data()["summary"], "4,6")
        self.assert_validator_accepts()

    def test_report_constructors_cannot_reach_host_process(self):
        constructors = (
            "window.constructor.constructor",
            "globalThis.constructor.constructor",
            "console.constructor.constructor",
            "console.log.constructor",
        )
        for constructor in constructors:
            with self.subTest(constructor=constructor):
                self.write_report(
                    """
let observed = "blocked";
try { observed = CONSTRUCTOR("return typeof process")(); } catch (_) {}
if (observed === "object") throw new Error("HOST_PROCESS_VISIBLE");
window.DATA.summary = observed;
""".replace(
                        "CONSTRUCTOR", constructor
                    )
                )
                self.assertIn(self.load_data()["summary"], ("blocked", "undefined"))
                self.assert_validator_accepts()

    def test_loader_does_not_pass_host_callbacks_to_report_map(self):
        self.write_report(self.callback_probe("map"))
        self.assertEqual(self.load_data()["summary"], DATA["summary"])

    def test_validator_does_not_pass_host_callbacks_to_report_foreach(self):
        self.write_report(self.callback_probe("forEach"))
        self.assert_validator_accepts()

    @staticmethod
    def callback_probe(method):
        return """
window.DATA.research.METHOD = function (callback) {
    let observed = "blocked";
    try { observed = callback.constructor("return typeof process")(); } catch (_) {}
    throw new Error("HOST_CALLBACK_INVOKED:" + observed);
};
""".replace(
            "METHOD", method
        )


if __name__ == "__main__":
    unittest.main()
