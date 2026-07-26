import json
import tempfile
import unittest
from pathlib import Path

from comiset_extract import sample_real_anchors_from_prefix, source_has_mitre_label


class CleanRealWindowTests(unittest.TestCase):
    def write_prefix(self, directory: str, count: int, labelled_line: int | None = None) -> Path:
        path = Path(directory) / "prefix.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for line in range(1, count + 1):
                source = {
                    "@timestamp": f"2022-07-01T00:00:{line:02d}+00:00",
                    "host_name": "computer01",
                    "RuleName": "-",
                }
                if line == labelled_line:
                    source.update(
                        {
                            "rule_technique_id": "T1055",
                            "RuleName": "technique_id=T1055,technique_name=Process Injection",
                        }
                    )
                handle.write(json.dumps(source) + "\n")
        return path

    def test_detects_explicit_and_rule_name_labels(self) -> None:
        self.assertTrue(source_has_mitre_label({"rule_technique_id": "T1055"}))
        self.assertTrue(source_has_mitre_label({"RuleName": "technique_id=T1003"}))
        self.assertFalse(source_has_mitre_label({"RuleName": "-"}))

    def test_samples_from_clean_non_overlapping_pool_with_lab_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = self.write_prefix(directory, 12)
            anchors, lines_read, pool_count = sample_real_anchors_from_prefix(
                prefix, 2, 3, 2026, 60, 60, 4, False
            )

        self.assertEqual((lines_read, pool_count), (12, 3))
        self.assertEqual(len(anchors), 2)
        for anchor in anchors:
            self.assertEqual(anchor["anchor_line"] - anchor["line_start"], 2)
            self.assertEqual(anchor["line_end"] - anchor["anchor_line"], 1)
            self.assertEqual(anchor["line_end"] - anchor["line_start"] + 1, 4)

    def test_rejects_any_window_containing_a_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = self.write_prefix(directory, 10, labelled_line=2)
            anchors, _, _ = sample_real_anchors_from_prefix(prefix, 2, 2, 2026, 60, 60, 4, False)

        self.assertTrue(all(not (anchor["line_start"] <= 2 <= anchor["line_end"]) for anchor in anchors))


if __name__ == "__main__":
    unittest.main()
