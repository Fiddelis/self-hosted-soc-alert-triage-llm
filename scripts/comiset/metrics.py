from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from comiset.records import event_has_hidden_rule, is_relevant, segment_technique_ids


CONFUSION_FIELDS = ("tp", "fp", "fn", "tn")


def safe_div(numerator: int | float, denominator: int | float) -> float | None:
    return (numerator / denominator) if denominator else None


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * ratio
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def timing_summary(values: list[float]) -> dict[str, Any]:
    total = sum(values)
    return {
        "count": len(values),
        "total_seconds": total,
        "mean_seconds": safe_div(total, len(values)),
        "median_seconds": percentile(values, 0.5),
        "p95_seconds": percentile(values, 0.95),
        "min_seconds": min(values) if values else None,
        "max_seconds": max(values) if values else None,
    }


def confusion_metrics(tp: int, fp: int, fn: int, tn: int) -> dict[str, Any]:
    total = tp + fp + fn + tn
    recall = safe_div(tp, tp + fn)
    specificity = safe_div(tn, tn + fp)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "total": total,
        "precision": safe_div(tp, tp + fp),
        "recall": recall,
        "specificity": specificity,
        "accuracy": safe_div(tp + tn, total),
        "f1": safe_div(2 * tp, 2 * tp + fp + fn),
        "balanced_accuracy": ((recall + specificity) / 2) if recall is not None and specificity is not None else None,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def label_names(record: dict[str, Any]) -> list[str]:
    evaluation = record.get("evaluation", {})
    if not isinstance(evaluation, dict):
        return []
    label = evaluation.get("segment_label", {})
    if not isinstance(label, dict):
        return []
    values = label.get("technique_name") or []
    if isinstance(values, str):
        return [values]
    return [str(value) for value in values]


def hidden_label_fields(record: dict[str, Any]) -> dict[str, Any]:
    evaluation = record.get("evaluation", {})
    if not isinstance(evaluation, dict):
        return {}
    value = evaluation.get("hidden_label_fields")
    return value if isinstance(value, dict) else {}


def filter_error_record(record: dict[str, Any], outcome: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "segment_id": record.get("segment_id"),
        "anchor_line": record.get("anchor_line"),
        "anchor_time": record.get("anchor_time"),
        "event_line": record.get("event_line"),
        "event_id": record.get("event_id"),
        "technique_ids": segment_technique_ids(record),
        "technique_names": label_names(record),
        "hidden_label_fields": hidden_label_fields(record),
        "filter_result": record.get("filter_result", {}),
        "evaluation": record.get("evaluation", {}),
        "llm_event": record.get("llm_event", {}),
    }


def filter_outcome(record: dict[str, Any]) -> str:
    truth = event_has_hidden_rule(record)
    predicted = is_relevant(record)
    if truth and predicted:
        return "tp"
    if truth and not predicted:
        return "fn"
    if not truth and predicted:
        return "fp"
    return "tn"


def filter_report(input_path: Path, output_dir: Path) -> dict[str, str]:
    counts = {key: 0 for key in CONFUSION_FIELDS}
    by_segment: dict[str, dict[str, Any]] = {}
    confusion_rows: list[dict[str, Any]] = []
    false_negatives: list[dict[str, Any]] = []
    false_positives: list[dict[str, Any]] = []
    timings: list[float] = []
    parse_errors = 0
    inference_errors = 0
    processed = 0

    with input_path.open(encoding="utf-8") as src:
        for raw_line in src:
            if not raw_line.strip():
                continue
            processed += 1
            record = json.loads(raw_line)
            outcome = filter_outcome(record)
            counts[outcome] += 1
            result = record.get("filter_result", {})
            elapsed = result.get("elapsed_seconds") if isinstance(result, dict) else None
            if isinstance(elapsed, int | float):
                timings.append(float(elapsed))
            parse_errors += int(bool(isinstance(result, dict) and result.get("parse_error")))
            inference_errors += int(bool(isinstance(result, dict) and result.get("error")))

            segment_id = str(record.get("segment_id", ""))
            segment = by_segment.setdefault(
                segment_id,
                {
                    "segment_id": segment_id,
                    "anchor_line": record.get("anchor_line"),
                    "anchor_time": record.get("anchor_time"),
                    "technique_ids": ",".join(segment_technique_ids(record)),
                    "technique_names": ",".join(label_names(record)),
                    **{key: 0 for key in CONFUSION_FIELDS},
                },
            )
            segment[outcome] += 1
            row = {
                "outcome": outcome,
                "truth_malicious": event_has_hidden_rule(record),
                "predicted_relevant": is_relevant(record),
                "segment_id": segment_id,
                "anchor_line": record.get("anchor_line"),
                "anchor_time": record.get("anchor_time"),
                "event_line": record.get("event_line"),
                "event_id": record.get("event_id"),
                "technique_ids": ",".join(segment_technique_ids(record)),
                "technique_names": ",".join(label_names(record)),
                "confidence": result.get("confidence") if isinstance(result, dict) else None,
                "elapsed_seconds": elapsed,
                "has_parse_error": bool(isinstance(result, dict) and result.get("parse_error")),
                "has_inference_error": bool(isinstance(result, dict) and result.get("error")),
            }
            confusion_rows.append(row)
            if outcome == "fn":
                false_negatives.append(filter_error_record(record, outcome))
            elif outcome == "fp":
                false_positives.append(filter_error_record(record, outcome))

    metrics = {
        **confusion_metrics(counts["tp"], counts["fp"], counts["fn"], counts["tn"]),
        "input": str(input_path),
        "processed_records": processed,
        "parse_errors": parse_errors,
        "inference_errors": inference_errors,
        "parse_error_rate": safe_div(parse_errors, processed),
        "inference_error_rate": safe_div(inference_errors, processed),
        "timing": timing_summary(timings),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / "filter_metrics.json"
    confusion_path = output_dir / "filter_confusion.csv"
    by_segment_path = output_dir / "filter_metrics_by_segment.csv"
    timing_path = output_dir / "filter_timing.csv"
    false_negatives_path = output_dir / "filter_false_negatives.jsonl"
    false_positives_path = output_dir / "filter_false_positives.jsonl"

    atomic_write_json(metrics_path, metrics)
    write_csv(
        confusion_path,
        [
            "outcome",
            "truth_malicious",
            "predicted_relevant",
            "segment_id",
            "anchor_line",
            "anchor_time",
            "event_line",
            "event_id",
            "technique_ids",
            "technique_names",
            "confidence",
            "elapsed_seconds",
            "has_parse_error",
            "has_inference_error",
        ],
        confusion_rows,
    )
    segment_rows = sorted(by_segment.values(), key=lambda row: (str(row.get("anchor_time", "")), row["segment_id"]))
    for row in segment_rows:
        row.update(confusion_metrics(row["tp"], row["fp"], row["fn"], row["tn"]))
    write_csv(
        by_segment_path,
        [
            "segment_id",
            "anchor_line",
            "anchor_time",
            "technique_ids",
            "technique_names",
            "tp",
            "fp",
            "fn",
            "tn",
            "total",
            "precision",
            "recall",
            "specificity",
            "accuracy",
            "f1",
            "balanced_accuracy",
        ],
        segment_rows,
    )
    timing = timing_summary(timings)
    write_csv(timing_path, list(timing.keys()), [timing])
    write_jsonl(false_negatives_path, false_negatives)
    write_jsonl(false_positives_path, false_positives)
    return {
        "metrics": str(metrics_path),
        "confusion": str(confusion_path),
        "by_segment": str(by_segment_path),
        "timing": str(timing_path),
        "false_negatives": str(false_negatives_path),
        "false_positives": str(false_positives_path),
    }


def is_interesting(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"interesting", "true", "yes", "relevant"}
    return False


def classification_outcome(record: dict[str, Any]) -> str:
    truth = bool(segment_technique_ids(record))
    result = record.get("classification_result", {})
    chunks = result.get("chunks", []) if isinstance(result, dict) else []
    predicted = any(is_interesting(chunk.get("classification")) for chunk in chunks if isinstance(chunk, dict))
    if truth and predicted:
        return "tp"
    if truth and not predicted:
        return "fn"
    if not truth and predicted:
        return "fp"
    return "tn"


def classification_detail_record(record: dict[str, Any], outcome: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "segment_id": record.get("segment_id"),
        "anchor_line": record.get("anchor_line"),
        "anchor_time": record.get("anchor_time"),
        "technique_ids": segment_technique_ids(record),
        "technique_names": label_names(record),
        "classification_result": record.get("classification_result", {}),
        "evaluation": record.get("evaluation", {}),
    }


def classify_report(input_path: Path, output_dir: Path) -> dict[str, str]:
    counts = {key: 0 for key in CONFUSION_FIELDS}
    rows: list[dict[str, Any]] = []
    details = {key: [] for key in CONFUSION_FIELDS}
    chunk_timings: list[float] = []
    segment_timings: list[float] = []
    parse_errors = 0
    inference_errors = 0
    processed = 0
    total_chunks = 0

    with input_path.open(encoding="utf-8") as src:
        for raw_line in src:
            if not raw_line.strip():
                continue
            processed += 1
            record = json.loads(raw_line)
            outcome = classification_outcome(record)
            counts[outcome] += 1
            result = record.get("classification_result", {})
            chunks = result.get("chunks", []) if isinstance(result, dict) else []
            total_chunks += len(chunks)
            chunk_elapsed = [
                float(chunk["elapsed_seconds"])
                for chunk in chunks
                if isinstance(chunk, dict) and isinstance(chunk.get("elapsed_seconds"), int | float)
            ]
            chunk_timings.extend(chunk_elapsed)
            segment_elapsed = sum(chunk_elapsed)
            segment_timings.append(segment_elapsed)
            parse_errors += sum(1 for chunk in chunks if isinstance(chunk, dict) and chunk.get("parse_error"))
            inference_errors += sum(1 for chunk in chunks if isinstance(chunk, dict) and chunk.get("error"))
            predicted = any(is_interesting(chunk.get("classification")) for chunk in chunks if isinstance(chunk, dict))
            confidences = [
                float(chunk["confidence"])
                for chunk in chunks
                if isinstance(chunk, dict) and isinstance(chunk.get("confidence"), int | float)
            ]
            row = {
                "outcome": outcome,
                "truth_malicious": bool(segment_technique_ids(record)),
                "predicted_interesting": predicted,
                "segment_id": record.get("segment_id"),
                "anchor_line": record.get("anchor_line"),
                "anchor_time": record.get("anchor_time"),
                "technique_ids": ",".join(segment_technique_ids(record)),
                "technique_names": ",".join(label_names(record)),
                "chunk_count": len(chunks),
                "event_count": sum(
                    int(chunk.get("event_count", 0) or 0) for chunk in chunks if isinstance(chunk, dict)
                ),
                "confidence_max": max(confidences) if confidences else None,
                "confidence_mean": safe_div(sum(confidences), len(confidences)),
                "elapsed_seconds": segment_elapsed,
                "chunk_elapsed_mean": safe_div(sum(chunk_elapsed), len(chunk_elapsed)),
                "has_parse_error": any(isinstance(chunk, dict) and chunk.get("parse_error") for chunk in chunks),
                "has_inference_error": any(isinstance(chunk, dict) and chunk.get("error") for chunk in chunks),
            }
            rows.append(row)
            details[outcome].append(classification_detail_record(record, outcome))

    metrics = {
        **confusion_metrics(counts["tp"], counts["fp"], counts["fn"], counts["tn"]),
        "input": str(input_path),
        "processed_segments": processed,
        "processed_chunks": total_chunks,
        "parse_errors": parse_errors,
        "inference_errors": inference_errors,
        "parse_error_rate": safe_div(parse_errors, total_chunks),
        "inference_error_rate": safe_div(inference_errors, total_chunks),
        "segment_timing": timing_summary(segment_timings),
        "chunk_timing": timing_summary(chunk_timings),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / "classification_metrics.json"
    confusion_path = output_dir / "classification_confusion.csv"
    segment_timing_path = output_dir / "classification_timing.csv"
    chunk_timing_path = output_dir / "classification_chunk_timing.csv"
    paths = {
        "metrics": str(metrics_path),
        "confusion": str(confusion_path),
        "timing": str(segment_timing_path),
        "chunk_timing": str(chunk_timing_path),
    }
    atomic_write_json(metrics_path, metrics)
    write_csv(
        confusion_path,
        [
            "outcome",
            "truth_malicious",
            "predicted_interesting",
            "segment_id",
            "anchor_line",
            "anchor_time",
            "technique_ids",
            "technique_names",
            "chunk_count",
            "event_count",
            "confidence_max",
            "confidence_mean",
            "elapsed_seconds",
            "chunk_elapsed_mean",
            "has_parse_error",
            "has_inference_error",
        ],
        rows,
    )
    segment_timing = timing_summary(segment_timings)
    chunk_timing = timing_summary(chunk_timings)
    write_csv(segment_timing_path, list(segment_timing.keys()), [segment_timing])
    write_csv(chunk_timing_path, list(chunk_timing.keys()), [chunk_timing])
    for outcome, name in (
        ("fn", "classification_false_negatives.jsonl"),
        ("fp", "classification_false_positives.jsonl"),
        ("tp", "classification_true_positives.jsonl"),
        ("tn", "classification_true_negatives.jsonl"),
    ):
        path = output_dir / name
        write_jsonl(path, details[outcome])
        paths[outcome] = str(path)
    return paths


def empty_filter_totals() -> dict[str, int]:
    return {
        "events": 0,
        "kept": 0,
        "dropped": 0,
        "rule_events": 0,
        "rule_kept": 0,
        "rule_dropped": 0,
    }


def update_filter_metrics(
    totals: dict[str, int],
    by_segment: dict[str, dict[str, Any]],
    record: dict[str, Any],
) -> None:
    relevant = is_relevant(record)
    has_rule = event_has_hidden_rule(record)

    totals["events"] += 1
    totals["kept"] += int(relevant)
    totals["dropped"] += int(not relevant)
    totals["rule_events"] += int(has_rule)
    totals["rule_kept"] += int(has_rule and relevant)
    totals["rule_dropped"] += int(has_rule and not relevant)

    segment = by_segment.setdefault(
        record["segment_id"],
        {
            "segment_id": record["segment_id"],
            "anchor_line": record["anchor_line"],
            "anchor_time": record["anchor_time"],
            "technique_ids": ",".join(segment_technique_ids(record)),
            "events": 0,
            "kept": 0,
            "dropped": 0,
            "rule_events": 0,
            "rule_kept": 0,
            "rule_dropped": 0,
        },
    )
    segment["events"] += 1
    segment["kept"] += int(relevant)
    segment["dropped"] += int(not relevant)
    segment["rule_events"] += int(has_rule)
    segment["rule_kept"] += int(has_rule and relevant)
    segment["rule_dropped"] += int(has_rule and not relevant)


def filter_metrics_summary(totals: dict[str, int], segment_count: int, output: Path | None = None) -> dict[str, Any]:
    rule_events = totals["rule_events"]
    return {
        **totals,
        "segments": segment_count,
        "keep_rate": (totals["kept"] / totals["events"]) if totals["events"] else None,
        "drop_rate": (totals["dropped"] / totals["events"]) if totals["events"] else None,
        "rule_recall_after_filter": (totals["rule_kept"] / rule_events) if rule_events else None,
        "rule_drop_rate": (totals["rule_dropped"] / rule_events) if rule_events else None,
        "output": str(output) if output else None,
    }


def write_filter_metrics(
    metrics_path: Path,
    by_segment_path: Path,
    totals: dict[str, int],
    by_segment: dict[str, dict[str, Any]],
) -> None:
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    by_segment_path.parent.mkdir(parents=True, exist_ok=True)

    summary = filter_metrics_summary(totals, len(by_segment), by_segment_path)
    tmp = metrics_path.with_suffix(metrics_path.suffix + ".tmp")
    tmp.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(metrics_path)

    rows = sorted(by_segment.values(), key=lambda row: (row["anchor_time"], row["segment_id"]))
    tmp_csv = by_segment_path.with_suffix(by_segment_path.suffix + ".tmp")
    with tmp_csv.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "segment_id",
            "anchor_line",
            "anchor_time",
            "technique_ids",
            "events",
            "kept",
            "dropped",
            "rule_events",
            "rule_kept",
            "rule_dropped",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp_csv.replace(by_segment_path)


def load_filter_metrics(
    metrics_path: Path,
    by_segment_path: Path,
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    if not metrics_path.exists() or not by_segment_path.exists():
        return empty_filter_totals(), {}

    raw_totals = json.loads(metrics_path.read_text(encoding="utf-8"))
    totals = empty_filter_totals()
    for key in totals:
        totals[key] = int(raw_totals.get(key, 0) or 0)

    by_segment: dict[str, dict[str, Any]] = {}
    with by_segment_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            segment_id = row["segment_id"]
            parsed = dict(row)
            for key in ("events", "kept", "dropped", "rule_events", "rule_kept", "rule_dropped"):
                parsed[key] = int(parsed.get(key, 0) or 0)
            parsed["anchor_line"] = int(parsed["anchor_line"]) if parsed.get("anchor_line") else None
            by_segment[segment_id] = parsed

    return totals, by_segment


def collect_filter_metrics(path: Path, technique_id: str | None = None) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    totals = defaultdict(int)
    totals.update(empty_filter_totals())
    by_segment: dict[str, dict[str, Any]] = {}

    with path.open(encoding="utf-8") as src:
        for raw_line in src:
            record = json.loads(raw_line)
            if technique_id and technique_id not in segment_technique_ids(record):
                continue
            update_filter_metrics(totals, by_segment, record)

    return dict(totals), by_segment
