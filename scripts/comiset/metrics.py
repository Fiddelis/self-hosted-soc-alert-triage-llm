from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from comiset.records import event_has_hidden_rule, is_relevant, segment_technique_ids


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
