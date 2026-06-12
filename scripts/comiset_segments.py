#!/usr/bin/env python3
"""Summarize and split COMISET extracted segment JSONL files."""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SegmentStats:
    segment_id: str
    anchor_line: int | None = None
    anchor_time: str | None = None
    technique_ids: set[str] = field(default_factory=set)
    technique_name: str | None = None
    rule_name: str | None = None
    event_count: int = 0
    labelled_event_count: int = 0
    first_event_time: str | None = None
    last_event_time: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "anchor_line": self.anchor_line,
            "anchor_time": self.anchor_time,
            "technique_ids": ",".join(sorted(self.technique_ids)),
            "technique_name": self.technique_name,
            "rule_name": self.rule_name,
            "event_count": self.event_count,
            "labelled_event_count": self.labelled_event_count,
            "first_event_time": self.first_event_time,
            "last_event_time": self.last_event_time,
        }


def segment_label(record: dict[str, Any]) -> dict[str, Any]:
    evaluation = record.get("evaluation")
    if isinstance(evaluation, dict):
        label = evaluation.get("segment_label")
        if isinstance(label, dict):
            return label
    label = record.get("label")
    return label if isinstance(label, dict) else {}


def event_payload(record: dict[str, Any]) -> dict[str, Any]:
    for key in ("llm_event", "event"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return {}


def event_time(record: dict[str, Any]) -> str | None:
    event = event_payload(record)
    for key in ("@timestamp", "event_original_time", "event_recorded_time"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    return None


def event_has_label(record: dict[str, Any]) -> bool:
    evaluation = record.get("evaluation")
    if isinstance(evaluation, dict):
        if evaluation.get("event_has_rule_technique"):
            return True
        hidden = evaluation.get("hidden_label_fields")
        return bool(hidden)
    event = event_payload(record)
    return any("technique" in key.lower() for key in event)


def update_stats(stats: SegmentStats, record: dict[str, Any]) -> None:
    stats.anchor_line = stats.anchor_line or record.get("anchor_line")
    stats.anchor_time = stats.anchor_time or record.get("anchor_time")

    label = segment_label(record)
    technique_ids = label.get("technique_ids") or []
    if isinstance(technique_ids, str):
        technique_ids = [technique_ids]
    for technique_id in technique_ids:
        stats.technique_ids.add(str(technique_id))

    stats.technique_name = stats.technique_name or label.get("technique_name")
    stats.rule_name = stats.rule_name or label.get("rule_name")
    stats.event_count += 1
    if event_has_label(record):
        stats.labelled_event_count += 1

    timestamp = event_time(record)
    if timestamp is not None:
        stats.first_event_time = min(stats.first_event_time, timestamp) if stats.first_event_time else timestamp
        stats.last_event_time = max(stats.last_event_time, timestamp) if stats.last_event_time else timestamp


def collect_stats(input_path: Path) -> dict[str, SegmentStats]:
    segments: dict[str, SegmentStats] = {}
    with input_path.open(encoding="utf-8") as src:
        for raw_line in src:
            record = json.loads(raw_line)
            segment_id = str(record["segment_id"])
            stats = segments.setdefault(segment_id, SegmentStats(segment_id=segment_id))
            update_stats(stats, record)
    return segments


def write_summary(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else None
    rows = [stats.to_row() for stats in collect_stats(input_path).values()]
    rows.sort(key=lambda row: (str(row["anchor_time"]), str(row["segment_id"])))

    if args.format == "jsonl":
        text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    else:
        fieldnames = [
            "segment_id",
            "anchor_line",
            "anchor_time",
            "technique_ids",
            "technique_name",
            "rule_name",
            "event_count",
            "labelled_event_count",
            "first_event_time",
            "last_event_time",
        ]
        import io

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        text = buffer.getvalue()

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    else:
        print(text, end="")

    print(
        json.dumps(
            {
                "segments": len(rows),
                "events": sum(int(row["event_count"]) for row in rows),
                "output": str(output_path) if output_path else None,
            },
            indent=2,
        )
    )


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "unknown"


def segment_filename(record: dict[str, Any], sequence: int) -> str:
    label = segment_label(record)
    technique_ids = label.get("technique_ids") or ["unknown"]
    if isinstance(technique_ids, str):
        technique_ids = [technique_ids]
    technique = safe_name("-".join(str(item) for item in technique_ids))
    anchor_line = record.get("anchor_line", "unknown-line")
    segment_id = record["segment_id"]
    return f"{sequence:06d}_{technique}_line-{anchor_line}_{segment_id}.jsonl"


def split_segments(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    handles: dict[str, Any] = {}
    paths: dict[str, Path] = {}
    sequences: dict[str, int] = {}
    try:
        with input_path.open(encoding="utf-8") as src:
            for raw_line in src:
                record = json.loads(raw_line)
                segment_id = str(record["segment_id"])
                if segment_id not in handles:
                    sequences[segment_id] = len(sequences) + 1
                    path = output_dir / segment_filename(record, sequences[segment_id])
                    paths[segment_id] = path
                    handles[segment_id] = path.open("w", encoding="utf-8")
                handles[segment_id].write(raw_line)
    finally:
        for handle in handles.values():
            handle.close()

    manifest_path = Path(args.manifest) if args.manifest else output_dir / "manifest.csv"
    rows = []
    stats = collect_stats(input_path)
    for segment_id, path in paths.items():
        row = stats[segment_id].to_row()
        row["path"] = str(path)
        rows.append(row)
    rows.sort(key=lambda row: row["path"])

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "path",
            "segment_id",
            "anchor_line",
            "anchor_time",
            "technique_ids",
            "technique_name",
            "rule_name",
            "event_count",
            "labelled_event_count",
            "first_event_time",
            "last_event_time",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(
        json.dumps(
            {
                "segments": len(paths),
                "output_dir": str(output_dir),
                "manifest": str(manifest_path),
            },
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    summary = sub.add_parser("summary", help="Count events per segment/attack.")
    summary.add_argument("--input", required=True)
    summary.add_argument("--output")
    summary.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    summary.set_defaults(func=write_summary)

    split = sub.add_parser("split", help="Write each segment/attack to its own JSONL file.")
    split.add_argument("--input", required=True)
    split.add_argument("--output-dir", required=True)
    split.add_argument("--manifest")
    split.set_defaults(func=split_segments)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
