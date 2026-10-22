"""Gate cutoffs and continuous core scores are separate evaluation contracts."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omnidr_eval import engine


def score_direction(tier, ref, scores, **fields):
    atoms = [
        {"id": f"A-{i}", "check": "J", "expected": "yes"} for i in range(len(scores))
    ]
    direction = {
        "id": "A",
        "ref": ref,
        "name": "Example",
        "dimension": "content",
        "tier": tier,
        "atoms": atoms,
        **fields,
    }
    judgments = [{"id": a["id"], "score": value} for a, value in zip(atoms, scores)]
    aggregate = engine.aggregate(
        [{"direction": direction, "atoms": judgments}], {"content": 1.0}
    )
    return direction, judgments, aggregate


class DirectionScoringTests(unittest.TestCase):
    def test_core_has_no_pass_fail_cutoff_even_with_legacy_fields(self):
        baseline = score_direction("core", "C-SEG", [1, 0, 0, None])[2]
        self.assertEqual(baseline["total_score"], 33.33)
        for fields in (
            {"logic": "threshold", "threshold": 0.9},
            {"logic": "threshold", "threshold": 0.1},
            {"logic": "count", "threshold": 2},
            {"logic": "all"},
        ):
            with self.subTest(fields=fields):
                direction, judgments, result = score_direction(
                    "core", "C-SEG", [1, 0, 0, None], **fields
                )
                self.assertIsNone(engine.direction_pass(direction, judgments)[0])
                self.assertEqual(result, baseline)
                self.assertFalse(
                    {"logic", "threshold", "logic_met"} & result["directions"][0].keys()
                )

    def test_core_saturation_remains_distinct_from_legacy_threshold(self):
        for ref in ("C-FC", "C-CA", "C-CMP"):
            with self.subTest(ref=ref):
                result = score_direction(
                    "core", ref, [1] + [0] * 9, logic="threshold", threshold=0.6
                )[2]
                direction = result["directions"][0]
                self.assertEqual(direction["pass_rate"], 0.1)
                self.assertEqual(direction["coverage"]["saturation"], 0.18)
                self.assertEqual(direction["effective_rate"], 0.5556)
                self.assertEqual(result["total_score"], 55.56)

    def test_gate_cutoffs_still_apply_at_their_boundaries(self):
        for scores, fields, passed, total in (
            ([1, 1, 1, 1, 0], {"logic": "threshold", "threshold": 0.8}, True, 80),
            ([1, 1, 1, 0, 0], {"logic": "threshold", "threshold": 0.8}, False, 0),
            ([1, 0], {"logic": "all"}, False, 0),
            ([1, 1], {"logic": "all"}, True, 100),
        ):
            with self.subTest(scores=scores, fields=fields):
                result = score_direction("gate", "C-IF", scores, **fields)[2]
                self.assertEqual(result["gate_pass"], passed)
                self.assertEqual(result["directions"][0]["logic_met"], passed)
                self.assertEqual(result["total_score"], total)


if __name__ == "__main__":
    unittest.main()
