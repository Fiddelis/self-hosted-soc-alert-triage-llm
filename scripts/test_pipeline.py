import argparse
import json
import tempfile
import unittest
from pathlib import Path

from comiset.metrics import (
    classification_outcome,
    classify_report,
    empty_filter_totals,
    filter_outcome,
    update_filter_metrics,
)
from comiset.privacy import MITRE_LABEL_PATTERN, split_event_labels
from comiset.records import aggregate_chunk_results, record_to_prompt_payload
from comiset_llm_pipeline import build_parser, filter_dataset, finalize_phase_manifest, load_segments, prepare_model


class LabelPrivacyTests(unittest.TestCase):
    def test_removes_labels_from_rule_name_and_message(self) -> None:
        event = {
            "process_name": "powershell.exe",
            "RuleName": "technique_id=T1059.001,technique_name=PowerShell",
            "event_original_message": "Process created\nRuleName: technique_id=T1059.001\nImage: powershell.exe",
            "rule_technique_id": "T1059.001",
        }
        clean, hidden = split_event_labels(event, ("rule_technique_id",))
        record = {
            "segment_id": "segment",
            "anchor_time": "2022-01-01T00:00:00+00:00",
            "event_line": 1,
            "llm_event": clean,
        }
        for prompt_format in ("csv", "json"):
            payload = record_to_prompt_payload(record, prompt_format)
            self.assertIsNone(MITRE_LABEL_PATTERN.search(payload))
            self.assertIn("powershell.exe", payload)
        self.assertEqual(set(hidden), {"RuleName", "event_original_message", "rule_technique_id"})


class CliTests(unittest.TestCase):
    def test_filter_dataset_command(self) -> None:
        args = build_parser().parse_args(
            [
                "filter-dataset",
                "--run-dir",
                "runs/test",
                "--model",
                "org/model:model.gguf",
            ]
        )
        self.assertEqual(args.func, filter_dataset)
        self.assertEqual(args.input_dir, "dataset/processed")


class RunManifestTests(unittest.TestCase):
    def test_records_input_output_prompts_and_model_manifest(self) -> None:
        class FakeGateway:
            def prepare(self, model, warmup_runs):
                return {"reference": model, "sha256": "model-hash", "setup_timing": {"warmup_runs": warmup_runs}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.jsonl"
            output_path = root / "output.jsonl"
            manifest_path = root / "run_manifest.json"
            input_path.write_text("input\n")
            output_path.write_text("output\n")
            args = argparse.Namespace(
                input=str(input_path),
                output=str(output_path),
                run_manifest=str(manifest_path),
                warmup_runs=1,
                inference_runs=2,
                prompt_format="csv",
                max_tokens=1500,
            )
            prepare_model(args, FakeGateway(), "org/model:model.gguf", "filter")
            finalize_phase_manifest(args, "org/model:model.gguf", "filter")
            manifest = json.loads(manifest_path.read_text())

        self.assertEqual(manifest["models"]["org/model:model.gguf"]["sha256"], "model-hash")
        self.assertIn(str(input_path), manifest["inputs"])
        phase = manifest["phases"]["filter:org/model:model.gguf"]
        self.assertIn("output_sha256", phase)
        self.assertEqual(phase["inference_runs"], 2)
        self.assertIn("filter_prompt_sha256", phase)


class FilterErrorTests(unittest.TestCase):
    def test_operational_error_is_unscored_not_dropped(self) -> None:
        record = {
            "segment_id": "segment",
            "anchor_line": 1,
            "anchor_time": "2022-01-01T00:00:00+00:00",
            "evaluation": {
                "segment_label": {"technique_ids": ["T1059"]},
                "hidden_label_fields": {"rule_technique_id": "T1059"},
            },
            "filter_result": {"error": "failed", "relevant": False},
        }
        totals = empty_filter_totals()
        by_segment = {}
        update_filter_metrics(totals, by_segment, record)
        self.assertEqual(filter_outcome(record), "error")
        self.assertEqual((totals["events"], totals["errors"], totals["rule_errors"]), (1, 1, 1))
        self.assertEqual((totals["kept"], totals["dropped"]), (0, 0))


class SegmentAndAggregationTests(unittest.TestCase):
    def test_preserves_segment_when_every_event_was_dropped(self) -> None:
        record = {
            "segment_id": "positive",
            "anchor_line": 10,
            "anchor_time": "2022-01-01T00:00:00+00:00",
            "evaluation": {"segment_label": {"technique_ids": ["T1059"]}},
            "filter_result": {"relevant": False},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "filtered.jsonl"
            path.write_text(json.dumps(record) + "\n")
            segments = load_segments(path, False, 1000)
        self.assertIn("positive", segments)
        self.assertEqual(segments["positive"]["records"], [])

    def test_majority_tie_and_invalid_votes_are_explicit(self) -> None:
        majority = aggregate_chunk_results(
            [
                {"classification": "Interesting"},
                {"classification": "Interesting"},
                {"classification": "Not Interesting"},
                {"error": "failed"},
            ]
        )
        self.assertEqual(majority["classification"], "Interesting")
        self.assertEqual(majority["invalid_votes"], 1)

        tie = aggregate_chunk_results(
            [{"classification": "Interesting"}, {"classification": "Not Interesting"}]
        )
        self.assertEqual((tie["status"], tie["classification"]), ("tie", "Not Interesting"))

        error = aggregate_chunk_results([{"parse_error": True}])
        self.assertEqual((error["status"], error["classification"]), ("error", None))

    def test_empty_positive_segment_is_counted_as_false_negative(self) -> None:
        record = {
            "evaluation": {"segment_label": {"technique_ids": ["T1059"]}},
            "classification_result": {"aggregate": aggregate_chunk_results([], empty_segment=True), "chunks": []},
        }
        self.assertEqual(classification_outcome(record), "fn")

    def test_operational_error_is_not_a_negative_prediction(self) -> None:
        record = {
            "evaluation": {"segment_label": {"technique_ids": ["T1059"]}},
            "classification_result": {"aggregate": aggregate_chunk_results([{"error": "failed"}]), "chunks": []},
        }
        self.assertEqual(classification_outcome(record), "error")

    def test_report_keeps_all_249_empty_segments_in_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "classifications.jsonl"
            with input_path.open("w") as handle:
                for index in range(249):
                    positive = index < 49
                    record = {
                        "segment_id": str(index),
                        "evaluation": {
                            "segment_label": {"technique_ids": ["T1059"] if positive else []}
                        },
                        "classification_result": {
                            "aggregate": aggregate_chunk_results([], empty_segment=True),
                            "chunks": [],
                        },
                    }
                    handle.write(json.dumps(record) + "\n")
            classify_report(input_path, Path(directory))
            metrics = json.loads((Path(directory) / "classification_metrics.json").read_text())
        self.assertEqual(metrics["processed_segments"], 249)
        self.assertEqual(metrics["total"], 249)
        self.assertEqual((metrics["fn"], metrics["tn"]), (49, 200))


if __name__ == "__main__":
    unittest.main()
