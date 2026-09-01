import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comiset.checkpoint import save_checkpoint as real_save_checkpoint
from comiset.metrics import (
    classification_outcome,
    classify_report,
    empty_filter_totals,
    filter_outcome,
    update_filter_metrics,
)
from comiset.privacy import MITRE_LABEL_PATTERN, split_event_labels
from comiset.responses import validate_json_response
from comiset.records import (
    aggregate_chunk_results,
    approx_chunks,
    chunk_to_prompt_payload,
    record_to_prompt_payload,
)
from comiset_llm_pipeline import (
    build_parser,
    classify_prompt,
    classify_model_pipeline,
    apply_filter_to_record,
    filter_dataset,
    finalize_phase_manifest,
    load_segments,
    prepare_model,
    validate_filter_output,
)


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

        raw_record = dict(record, llm_event=event)
        for prompt_format in ("csv", "json"):
            classification_payload = chunk_to_prompt_payload([raw_record], prompt_format)
            self.assertIsNone(MITRE_LABEL_PATTERN.search(classification_payload))


class ResponseValidationTests(unittest.TestCase):
    def test_rejects_structurally_valid_but_semantically_invalid_json(self) -> None:
        self.assertEqual(
            validate_json_response({"classification": "Interesting"}, "classify")["parse_error"],
            True,
        )
        self.assertEqual(
            validate_json_response(
                {"classification": "Interesting", "confidence": 0.9, "reason": "ok"}, "classify"
            )["classification"],
            "Interesting",
        )


class ClassificationPayloadTests(unittest.TestCase):
    def test_compacts_message_only_for_classification(self) -> None:
        record = {
            "segment_id": "segment",
            "anchor_time": "2022-01-01T00:00:00+00:00",
            "event_line": 1,
            "llm_event": {
                "@timestamp": "2022-01-01T00:00:00Z",
                "process_name": "source.exe",
                "event_original_message": (
                    "Process accessed:\n"
                    "UtcTime: 2022-01-01 00:00:00.000\n"
                    "SourceProcessGUID: {long-random-guid}\n"
                    "TargetImage: C:\\\\Windows\\\\target.exe\n"
                    "GrantedAccess: 0x1000\n"
                    "CallTrace: C:\\\\a.dll+123|C:\\\\b.dll+456"
                ),
            },
        }

        for prompt_format in ("csv", "json"):
            filter_payload = record_to_prompt_payload(record, prompt_format)
            classify_payload = chunk_to_prompt_payload([record], prompt_format)

            self.assertIn("CallTrace", filter_payload)
            self.assertNotIn("CallTrace", classify_payload)
            self.assertNotIn("long-random-guid", classify_payload)
            compact_details = (
                json.loads(classify_payload)[0]["event_original_message"]
                if prompt_format == "json"
                else classify_payload
            )
            self.assertIn("TargetImage=C:\\\\Windows\\\\target.exe", compact_details)
            self.assertIn("GrantedAccess=0x1000", compact_details)

    def test_counts_one_csv_header_per_chunk(self) -> None:
        records = [
            {
                "segment_id": "segment",
                "anchor_time": "2022-01-01T00:00:00+00:00",
                "event_line": index,
                "llm_event": {"process_name": f"process-{index}.exe"},
            }
            for index in (1, 2)
        ]

        chunks = approx_chunks(records, max_tokens=80, prompt_format="csv")

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunk_to_prompt_payload(chunks[0], "csv").count("event_line,"), 1)


class CliTests(unittest.TestCase):
    def test_classification_defaults_and_thinking_switch(self) -> None:
        args = build_parser().parse_args(
            [
                "classify",
                "--input",
                "filtered.jsonl",
                "--output",
                "classifications.jsonl",
                "--checkpoint",
                "checkpoint.json",
                "--model",
                "org/model:model.gguf",
                "--thinking-mode",
                "no_think",
            ]
        )

        self.assertEqual(args.n_ctx, 8192)
        self.assertEqual(args.max_tokens, 5000)
        self.assertEqual(args.max_output_tokens, 256)
        self.assertEqual(args.thinking_mode, "no_think")

    def test_combined_pipeline_has_separate_classifier_context_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "run-dataset",
                "--run-dir",
                "runs/test",
                "--small-model",
                "org/small:small.gguf",
                "--big-model",
                "org/big:big.gguf",
            ]
        )

        self.assertEqual((args.n_ctx, args.max_output_tokens), (4096, 512))
        self.assertEqual((args.classify_n_ctx, args.classify_max_output_tokens), (8192, 256))
        self.assertEqual((args.max_tokens, args.thinking_mode), (5000, "auto"))

    def test_classification_prompt_records_thinking_mode(self) -> None:
        segment = {"segment_id": "segment", "anchor_time": "2022-01-01T00:00:00Z"}
        auto = classify_prompt(segment, 1, 1, [], "csv", "auto")
        think = classify_prompt(segment, 1, 1, [], "csv", "think")
        no_think = classify_prompt(segment, 1, 1, [], "csv", "no_think")

        self.assertNotIn("/think", auto)
        self.assertTrue(think.startswith("/think\n"))
        self.assertTrue(no_think.startswith("/no_think\n"))

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
                n_ctx=4096,
                max_output_tokens=512,
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


class ClassificationCheckpointTests(unittest.TestCase):
    def test_flushes_output_before_checkpoint_and_records_thinking_mode(self) -> None:
        class FakeGateway:
            def prepare(self, model, warmup_runs):
                return {"reference": model, "setup_timing": {"warmup_runs": warmup_runs}}

            def chat(self, model, system_prompt, user_prompt, timeout_seconds):
                self.assert_prompt = user_prompt
                return '{"classification":"Interesting","confidence":0.9,"reason":"test"}'

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "filtered.jsonl"
            output_path = root / "classifications.jsonl"
            checkpoint_path = root / "checkpoint.json"
            input_path.write_text(
                json.dumps(
                    {
                        "segment_id": "segment",
                        "anchor_line": 1,
                        "anchor_time": "2022-01-01T00:00:00Z",
                        "evaluation": {"segment_label": {"technique_ids": []}},
                        "llm_event": {"process_name": "process.exe"},
                        "filter_result": {"relevant": True},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                input=str(input_path),
                output=str(output_path),
                checkpoint=str(checkpoint_path),
                model="org/model:model.gguf",
                timeout_seconds=180,
                resume=False,
                include_irrelevant=False,
                max_events_per_segment=1000,
                max_tokens=5000,
                max_segments=None,
                prompt_format="csv",
                n_ctx=8192,
                n_gpu_layers=-1,
                n_batch=512,
                seed=2026,
                max_output_tokens=256,
                warmup_runs=0,
                inference_runs=1,
                run_manifest=str(root / "run_manifest.json"),
                progress=False,
                thinking_mode="no_think",
            )

            def save_and_check(path, state):
                if Path(path) == checkpoint_path and state.get("phase") == "classify":
                    written_lines = output_path.read_text(encoding="utf-8").splitlines()
                    self.assertEqual(len(written_lines), state["written"])
                real_save_checkpoint(path, state)

            with (
                patch("comiset_llm_pipeline.gateway_from_args", return_value=FakeGateway()),
                patch("comiset_llm_pipeline.save_checkpoint", side_effect=save_and_check),
            ):
                from comiset_llm_pipeline import classify_segments

                classify_segments(args)

            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["classification_result"]["thinking_mode"], "no_think")
            self.assertEqual(json.loads(checkpoint_path.read_text())["thinking_mode"], "no_think")

    def test_resume_uses_durable_output_when_checkpoint_is_ahead(self) -> None:
        class FakeGateway:
            def prepare(self, model, warmup_runs):
                return {"reference": model, "setup_timing": {"warmup_runs": warmup_runs}}

            def chat(self, model, system_prompt, user_prompt, timeout_seconds):
                return '{"classification":"Not Interesting","confidence":0.9,"reason":"test"}'

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "filtered.jsonl"
            output_path = root / "classifications.jsonl"
            checkpoint_path = root / "checkpoint.json"
            with input_path.open("w", encoding="utf-8") as handle:
                for index in (1, 2):
                    handle.write(
                        json.dumps(
                            {
                                "segment_id": f"segment-{index}",
                                "anchor_line": index,
                                "anchor_time": f"2022-01-0{index}T00:00:00Z",
                                "evaluation": {"segment_label": {"technique_ids": []}},
                                "llm_event": {"process_name": "process.exe"},
                                "filter_result": {"relevant": True},
                            }
                        )
                        + "\n"
                    )
            output_path.write_text("", encoding="utf-8")
            checkpoint_path.write_text(
                json.dumps({"phase": "classify", "segment_index": 1, "written": 1}),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                input=str(input_path), output=str(output_path), checkpoint=str(checkpoint_path),
                model="org/model:model.gguf", timeout_seconds=180, resume=True,
                include_irrelevant=False, max_events_per_segment=1000, max_tokens=5000,
                max_segments=None, prompt_format="csv", n_ctx=8192, n_gpu_layers=-1,
                n_batch=512, seed=2026, max_output_tokens=256, warmup_runs=0,
                inference_runs=1, run_manifest=str(root / "run_manifest.json"),
                progress=False, thinking_mode="auto",
            )

            with patch("comiset_llm_pipeline.gateway_from_args", return_value=FakeGateway()):
                from comiset_llm_pipeline import classify_segments

                classify_segments(args)

            results = [json.loads(line) for line in output_path.read_text().splitlines()]
            self.assertEqual([result["segment_id"] for result in results], ["segment-1", "segment-2"])


class CombinedClassificationTests(unittest.TestCase):
    def test_classifies_raw_once_and_each_filter_with_one_big_model(self) -> None:
        class FakeGateway:
            def __init__(self):
                self.calls = []

            def prepare(self, model, warmup_runs):
                return {"reference": model, "setup_timing": {"warmup_runs": warmup_runs}}

            def chat(self, model, system_prompt, user_prompt, timeout_seconds):
                self.calls.append(user_prompt)
                return '{"classification":"Interesting","confidence":0.9,"reason":"test"}'

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "processed" / "lab"
            input_dir.mkdir(parents=True)
            records = []
            for index in (1, 2):
                records.append(
                    {
                        "segment_id": f"segment-{index}",
                        "event_id": f"event-{index}",
                        "anchor_line": index,
                        "anchor_time": f"2022-01-0{index}T00:00:00Z",
                        "event_line": index,
                        "evaluation": {"segment_label": {"technique_ids": []}},
                        "llm_event": {"process_name": "process.exe"},
                    }
                )
            source = input_dir / "segment.jsonl"
            source.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            for filter_name in ("filter-a", "filter-b"):
                filter_dir = root / "run" / filter_name / "filter" / filter_name
                filter_dir.mkdir(parents=True)
                filter_dir.joinpath("filtered_events.jsonl").write_text(
                    "".join(
                        json.dumps(dict(record, filter_result={"relevant": True})) + "\n"
                        for record in records
                    ),
                    encoding="utf-8",
                )

            args = build_parser().parse_args(
                [
                    "classify-model",
                    "--input-dir",
                    str(root / "processed"),
                    "--run-dir",
                    str(root / "run"),
                    "--big-model",
                    "org/big:big.gguf",
                    "--no-progress",
                ]
            )
            fake = FakeGateway()
            with (
                patch("comiset_llm_pipeline.gateway_from_args", return_value=fake),
                patch("comiset_llm_pipeline.validate_dataset_input", return_value={"segments": 2, "events": 2}),
            ):
                classify_model_pipeline(args)

            big_name = "org_big_big.gguf__thinking-auto"
            index = json.loads((root / "run" / "classify" / big_name / "sources.json").read_text())
            self.assertEqual([entry["source"] for entry in index["sources"]], ["raw", "filtered:filter-a", "filtered:filter-b"])
            self.assertEqual(len(list((root / "run" / "classify" / "raw" / big_name).glob("classifications.jsonl"))), 1)
            self.assertEqual(len(fake.calls), 6)

    def test_filter_resume_validation_rejects_duplicate_or_reordered_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.jsonl"
            output_path = root / "filtered.jsonl"
            records = [
                {"segment_id": "segment", "event_id": str(index), "event_line": index}
                for index in (1, 2)
            ]
            input_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            output_path.write_text(
                "".join(json.dumps(records[index]) + "\n" for index in (1, 0)),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                validate_filter_output(input_path, output_path)


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

    def test_filter_inference_error_does_not_synthesize_negative_decision(self) -> None:
        class FailingGateway:
            def chat(self, *args, **kwargs):
                raise RuntimeError("timeout")

        record = {
            "segment_id": "segment",
            "anchor_time": "2022-01-01T00:00:00+00:00",
            "event_line": 1,
            "llm_event": {"process_name": "process.exe"},
        }
        result = apply_filter_to_record(record, "org/model:model.gguf", "csv", 10, 1, FailingGateway())
        self.assertNotIn("relevant", result["filter_result"])
        self.assertEqual(filter_outcome(result), "error")


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

    def test_filter_error_is_preserved_as_segment_error_state(self) -> None:
        record = {
            "segment_id": "failed",
            "anchor_line": 10,
            "anchor_time": "2022-01-01T00:00:00+00:00",
            "event_line": 10,
            "evaluation": {"segment_label": {"technique_ids": ["T1059"]}},
            "filter_result": {"error": "timeout"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "filtered.jsonl"
            path.write_text(json.dumps(record) + "\n")
            segment = load_segments(path, False, 1000)["failed"]
        self.assertEqual((segment["records"], segment["filter_errors"]), ([], 1))

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
