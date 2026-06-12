#!/usr/bin/env python3
"""Stream COMISET JSONL data from ZIP files and extract MITRE-centered windows."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import shutil
import sys
import zipfile
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from comiset.progress import ProgressBar


DEFAULT_REAL_PREFIX_BYTES = 2 * 1024 * 1024 * 1024


DEFAULT_ANCHOR_FIELDS = (
    "rule_technique_id",
    "Rule_technique_id",
    "RuleTechniqueId",
    "rule.technique.id",
)

DEFAULT_KEEP_FIELDS = (
    "@timestamp",
    "event_original_time",
    "event_recorded_time",
    "host_name",
    "user_name",
    "process_id",
    "process_guid",
    "process_name",
    "process_path",
    "CommandLine",
    "ParentCommandLine",
    "process_parent_id",
    "process_parent_guid",
    "process_parent_name",
    "file.path",
    "winlog.task",
    "Task",
    "RuleName",
    "rule_technique_id",
    "Rule_technique_id",
    "rule_technique_name",
    "Rule_technique_name",
    "event_id",
    "log_name",
    "source_name",
    "event_original_message",
)

DEFAULT_LABEL_FIELDS = (
    "rule_technique_id",
    "Rule_technique_id",
    "RuleTechniqueId",
    "rule_technique_name",
    "Rule_technique_name",
    "RuleTechniqueName",
    "rule_technique",
)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def get_source(obj: dict[str, Any]) -> dict[str, Any]:
    source = obj.get("_source")
    return source if isinstance(source, dict) else obj


def nested_get(obj: dict[str, Any], path: str) -> Any:
    current: Any = obj
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def first_value(source: dict[str, Any], fields: tuple[str, ...]) -> Any:
    lower_map = {key.lower(): key for key in source}
    for field_name in fields:
        if "." in field_name:
            value = nested_get(source, field_name)
        else:
            actual = lower_map.get(field_name.lower())
            value = source.get(actual) if actual else None
        if value not in (None, "", [], {}):
            return value
    return None


def extract_anchor(source: dict[str, Any], anchor_fields: tuple[str, ...]) -> dict[str, Any] | None:
    technique_id = first_value(source, anchor_fields)
    technique_name = first_value(
        source,
        ("rule_technique_name", "Rule_technique_name", "RuleTechniqueName", "rule.technique.name"),
    )

    if technique_id in (None, "", [], {}):
        return None

    if isinstance(technique_id, list):
        technique_ids = [str(item) for item in technique_id if item not in (None, "")]
    else:
        technique_ids = [str(technique_id)]

    return {
        "technique_ids": technique_ids,
        "technique_name": technique_name,
        "rule_name": first_value(source, ("RuleName", "rule.name")),
    }


def reduce_event(obj: dict[str, Any], keep_fields: tuple[str, ...]) -> dict[str, Any]:
    source = get_source(obj)
    reduced: dict[str, Any] = {"_id": obj.get("_id"), "_index": obj.get("_index")}
    for field_name in keep_fields:
        value = nested_get(source, field_name) if "." in field_name else source.get(field_name)
        if value not in (None, "", [], {}):
            reduced[field_name] = value
    return reduced


def split_llm_and_evaluation_event(
    event: dict[str, Any],
    label_fields: tuple[str, ...],
) -> tuple[dict[str, Any], dict[str, Any]]:
    label_names = {field.lower() for field in label_fields}
    llm_event: dict[str, Any] = {}
    hidden_labels: dict[str, Any] = {}
    for key, value in event.items():
        if key.lower() in label_names:
            hidden_labels[key] = value
        else:
            llm_event[key] = value

    return llm_event, {
        "event_has_rule_technique": bool(hidden_labels),
        "hidden_label_fields": hidden_labels,
    }


def build_segment_event_record(
    member: str,
    line_number: int,
    anchor: dict[str, Any],
    reduced_event: dict[str, Any],
    label_fields: tuple[str, ...],
) -> dict[str, Any]:
    llm_event, evaluation = split_llm_and_evaluation_event(reduced_event, label_fields)
    return {
        "segment_id": anchor["segment_id"],
        "anchor_line": anchor["anchor_line"],
        "anchor_time": anchor["anchor_time"],
        "event_line": line_number,
        "event_id": stable_event_id(member, line_number, reduced_event),
        "llm_event": llm_event,
        "evaluation": {
            "segment_label": anchor["label"],
            **evaluation,
        },
    }


def event_time(source: dict[str, Any]) -> datetime | None:
    for field_name in ("@timestamp", "event_original_time", "Event_original_time", "event_recorded_time"):
        value = source.get(field_name)
        parsed = parse_timestamp(value)
        if parsed is not None:
            return parsed
    return None


def event_host(source: dict[str, Any]) -> str | None:
    value = first_value(source, ("host_name", "host.name", "beat_name"))
    return str(value).lower() if value not in (None, "") else None


def stable_segment_id(member: str, line_number: int, anchor: dict[str, Any]) -> str:
    raw = json.dumps([member, line_number, anchor], sort_keys=True, default=str).encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def stable_merged_segment_id(anchor: dict[str, Any]) -> str:
    raw = json.dumps(
        [
            "merged",
            anchor.get("host"),
            anchor.get("start_time"),
            anchor.get("end_time"),
            anchor.get("anchor_lines"),
            anchor.get("label"),
        ],
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def stable_event_id(member: str, line_number: int, reduced: dict[str, Any]) -> str:
    raw = json.dumps([member, line_number, reduced.get("_id")], sort_keys=True, default=str).encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def stable_real_segment_id(member: str, line_number: int, ts: datetime, host: str | None, seed: int) -> str:
    raw = json.dumps(["real", member, line_number, ts.isoformat(), host, seed], sort_keys=True).encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def load_checkpoint(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(path: Path | None, state: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


@dataclass
class Segment:
    segment_id: str
    anchor_line: int
    anchor_time: datetime
    end_time: datetime
    anchor_host: str | None
    label: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)

    def accepts(self, ts: datetime, host: str | None, same_host: bool) -> bool:
        if ts > self.end_time:
            return False
        if same_host and self.anchor_host is not None and host != self.anchor_host:
            return False
        return True

    def to_json(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "anchor_line": self.anchor_line,
            "anchor_time": self.anchor_time.isoformat(),
            "label": self.label,
            "events": self.events,
        }


def build_ordered_segment_record(segment: Segment, label_fields: tuple[str, ...]) -> dict[str, Any]:
    segment_json = segment.to_json()
    segment_json["llm_events"] = []
    segment_json["evaluation"] = {"segment_label": segment_json.pop("label")}
    for event in segment_json.pop("events"):
        llm_event, event_evaluation = split_llm_and_evaluation_event(event, label_fields)
        segment_json["llm_events"].append(llm_event)
        segment_json.setdefault("event_evaluations", []).append(event_evaluation)
    return segment_json


def open_zip_member(zip_path: Path, member: str | None) -> tuple[zipfile.ZipFile, str]:
    zf = zipfile.ZipFile(zip_path)
    names = [info.filename for info in zf.infolist() if not info.is_dir()]
    if not names:
        zf.close()
        raise SystemExit(f"No file members found in {zip_path}")
    selected = member or names[0]
    if selected not in names:
        zf.close()
        raise SystemExit(f"Member {selected!r} not found. Available: {', '.join(names)}")
    return zf, selected


def default_raw_anchors_path(anchors_path: Path) -> Path:
    return anchors_path.with_name(f"{anchors_path.stem}.raw{anchors_path.suffix}")


def prepare_raw_anchors_path(anchors_path: Path, checkpoint: dict[str, Any], resume: bool) -> Path:
    raw_path = default_raw_anchors_path(anchors_path)
    if resume and checkpoint.get("phase") == "anchors" and anchors_path.exists() and not raw_path.exists():
        shutil.copyfile(anchors_path, raw_path)
    return raw_path


def merge_label(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    technique_ids = set(str(item) for item in base.get("technique_ids", []) if item)
    technique_ids.update(str(item) for item in incoming.get("technique_ids", []) if item)

    technique_names = set()
    for value in (base.get("technique_name"), incoming.get("technique_name")):
        if isinstance(value, list):
            technique_names.update(str(item) for item in value if item)
        elif value:
            technique_names.add(str(value))

    rule_names = set()
    for value in (base.get("rule_name"), incoming.get("rule_name")):
        if isinstance(value, list):
            rule_names.update(str(item) for item in value if item)
        elif value:
            rule_names.add(str(value))

    return {
        "technique_ids": sorted(technique_ids),
        "technique_name": sorted(technique_names),
        "rule_name": sorted(rule_names),
    }


def normalized_technique_ids(label: dict[str, Any]) -> tuple[str, ...]:
    values = label.get("technique_ids") or []
    if isinstance(values, str):
        values = [values]
    normalized = []
    for value in values:
        text = str(value).strip().upper()
        if re.fullmatch(r"T\d{4}(?:\.\d{3})?", text):
            normalized.append(text)
    return tuple(sorted(set(normalized)))


def normalized_rule_names(label: dict[str, Any]) -> tuple[str, ...]:
    values = label.get("rule_name") or []
    if isinstance(values, str):
        values = [values]
    normalized = [str(value).strip() for value in values if str(value).strip()]
    return tuple(sorted(set(normalized)))


def anchor_merge_key(label: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    technique_ids = normalized_technique_ids(label)
    if technique_ids:
        return ("technique_ids", technique_ids)
    rule_names = normalized_rule_names(label)
    if rule_names:
        return ("rule_names", rule_names)
    values = label.get("technique_ids") or []
    if isinstance(values, str):
        values = [values]
    raw_values = tuple(sorted(set(str(value).strip() for value in values if str(value).strip())))
    return ("raw_technique_ids", raw_values)


def merge_anchor_records(anchors: list[dict[str, Any]], gap_seconds: int, progress: bool = False) -> list[dict[str, Any]]:
    if gap_seconds < 0:
        return anchors

    parsed: list[dict[str, Any]] = []
    parse_progress = ProgressBar("merge anchors parse", len(anchors), enabled=progress)
    for index, anchor in enumerate(anchors, 1):
        start_dt = parse_timestamp(anchor["start_time"])
        end_dt = parse_timestamp(anchor["end_time"])
        anchor_dt = parse_timestamp(anchor["anchor_time"])
        if start_dt is None or end_dt is None or anchor_dt is None:
            parse_progress.update(index, suffix=f"valid={len(parsed)}")
            continue
        parsed.append(
            {
                **anchor,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "anchor_dt": anchor_dt,
                "anchor_lines": anchor.get("anchor_lines", [anchor["anchor_line"]]),
                "source_segment_ids": anchor.get("source_segment_ids", [anchor["segment_id"]]),
                "merge_key": anchor_merge_key(anchor.get("label", {})),
            }
        )
        parse_progress.update(index, suffix=f"valid={len(parsed)}")
    parse_progress.close(suffix=f"valid={len(parsed)}")

    sort_progress = ProgressBar("merge anchors sort", 1, enabled=progress)
    sort_progress.update(0, suffix=f"items={len(parsed)}", force=True)
    parsed.sort(key=lambda item: (item.get("host") or "", item["start_dt"], item["end_dt"]))
    sort_progress.update(1, suffix=f"items={len(parsed)}", force=True)
    sort_progress.close(suffix="done")
    merged: list[dict[str, Any]] = []
    gap = timedelta(seconds=gap_seconds)

    merge_progress = ProgressBar("merge anchors group", len(parsed), enabled=progress)
    for index, anchor in enumerate(parsed, 1):
        if not merged:
            merged.append(anchor)
            merge_progress.update(index, suffix=f"merged={len(merged)}")
            continue
        current = merged[-1]
        same_host = (current.get("host") or "") == (anchor.get("host") or "")
        overlaps = anchor["start_dt"] <= current["end_dt"] + gap
        same_identity = current.get("merge_key") == anchor.get("merge_key")
        if same_host and overlaps and same_identity:
            current["start_dt"] = min(current["start_dt"], anchor["start_dt"])
            current["end_dt"] = max(current["end_dt"], anchor["end_dt"])
            current["anchor_dt"] = min(current["anchor_dt"], anchor["anchor_dt"])
            current["start_time"] = current["start_dt"].isoformat()
            current["end_time"] = current["end_dt"].isoformat()
            current["anchor_time"] = current["anchor_dt"].isoformat()
            current["anchor_lines"].extend(anchor["anchor_lines"])
            current["source_segment_ids"].extend(anchor["source_segment_ids"])
            current["anchor_line"] = min(current["anchor_lines"])
            current["label"] = merge_label(current.get("label", {}), anchor.get("label", {}))
        else:
            merged.append(anchor)
        merge_progress.update(index, suffix=f"merged={len(merged)}")
    merge_progress.close(suffix=f"merged={len(merged)}")

    output = []
    clean_progress = ProgressBar("merge anchors clean", len(merged), enabled=progress)
    for index, anchor in enumerate(merged, 1):
        cleaned = {
            key: value
            for key, value in anchor.items()
            if key not in {"start_dt", "end_dt", "anchor_dt", "merge_key"}
        }
        cleaned["anchor_lines"] = sorted(set(cleaned["anchor_lines"]))
        cleaned["source_segment_ids"] = sorted(set(cleaned["source_segment_ids"]))
        cleaned["segment_id"] = stable_merged_segment_id(cleaned)
        output.append(cleaned)
        clean_progress.update(index, suffix=f"output={len(output)}")
    clean_progress.close(suffix=f"output={len(output)}")
    output.sort(key=lambda item: (item["anchor_time"], item["segment_id"]))
    return output


def load_anchor_candidates(anchors_path: Path) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    with anchors_path.open(encoding="utf-8") as handle:
        for line in handle:
            anchor = json.loads(line)
            anchor["start_dt"] = parse_timestamp(anchor.get("start_time"))
            anchor["end_dt"] = parse_timestamp(anchor.get("end_time"))
            anchor["anchor_dt"] = parse_timestamp(anchor.get("anchor_time"))
            if anchor["start_dt"] is None or anchor["end_dt"] is None or anchor["anchor_dt"] is None:
                continue
            anchor["normalized_technique_ids"] = normalized_technique_ids(anchor.get("label", {}))
            anchor["anchor_line_count"] = len(anchor.get("anchor_lines") or [anchor.get("anchor_line")])
            anchors.append(anchor)
    return anchors


def infer_technique_name(label: dict[str, Any], technique_id: str) -> str | list[str] | None:
    rule_name = label.get("rule_name")
    rule_names = rule_name if isinstance(rule_name, list) else ([rule_name] if rule_name else [])
    matches = [str(item) for item in rule_names if technique_id in str(item)]
    if matches:
        extracted = []
        for item in matches:
            marker = "technique_name="
            if marker in item:
                extracted.append(item.split(marker, 1)[1].strip())
        if extracted:
            unique = sorted(set(extracted))
            return unique[0] if len(unique) == 1 else unique

    technique_name = label.get("technique_name")
    if isinstance(technique_name, list):
        if len(technique_name) == 1:
            return technique_name[0]
        return technique_name
    return technique_name


def anchor_for_technique(anchor: dict[str, Any], technique_id: str) -> dict[str, Any]:
    cloned = {
        key: value
        for key, value in anchor.items()
        if key not in {"start_dt", "end_dt", "anchor_dt", "normalized_technique_ids"}
    }
    label = dict(cloned.get("label", {}))
    label["technique_ids"] = [technique_id]
    technique_name = infer_technique_name(label, technique_id)
    if technique_name not in (None, "", [], {}):
        label["technique_name"] = technique_name
    rule_name = label.get("rule_name")
    if isinstance(rule_name, list):
        matching_rule_names = [str(item) for item in rule_name if technique_id in str(item)]
        if matching_rule_names:
            label["rule_name"] = matching_rule_names[0] if len(matching_rule_names) == 1 else matching_rule_names
    cloned["label"] = label
    cloned["selected_technique_id"] = technique_id
    cloned["segment_id"] = stable_merged_segment_id(
        {
            **cloned,
            "label": label,
            "anchor_lines": cloned.get("anchor_lines", [cloned.get("anchor_line")]),
        }
    )
    return cloned


def anchor_is_time_separated(
    candidate: dict[str, Any],
    selected_anchors: list[dict[str, Any]],
    min_time_gap_seconds: int,
) -> bool:
    if min_time_gap_seconds <= 0:
        return True

    gap = timedelta(seconds=min_time_gap_seconds)
    candidate_start = candidate["start_dt"]
    candidate_end = candidate["end_dt"]
    for selected in selected_anchors:
        selected_start = selected["start_dt"]
        selected_end = selected["end_dt"]
        if candidate_end + gap <= selected_start or selected_end + gap <= candidate_start:
            continue
        return False
    return True


def choose_best_anchor_for_techniques(
    anchors: list[dict[str, Any]],
    top_per_technique: int = 1,
    min_time_gap_seconds: int = 60,
) -> list[dict[str, Any]]:
    if top_per_technique < 1:
        raise ValueError("top_per_technique must be >= 1")
    if min_time_gap_seconds < 0:
        raise ValueError("min_time_gap_seconds must be >= 0")

    candidates: dict[str, list[dict[str, Any]]] = {}
    for anchor in anchors:
        for technique_id in anchor.get("normalized_technique_ids", ()):
            candidates.setdefault(technique_id, []).append(anchor)

    selected = []
    for technique_id, technique_anchors in sorted(candidates.items()):
        selected_for_technique = []
        ranked = sorted(
            technique_anchors,
            key=lambda anchor: (
                anchor.get("anchor_line_count", 0),
                -anchor.get("anchor_line", 0),
            ),
            reverse=True,
        )
        for anchor in ranked:
            if not anchor_is_time_separated(anchor, selected_for_technique, min_time_gap_seconds):
                continue
            selected_for_technique.append(anchor)
            selected.append(anchor_for_technique(anchor, technique_id))
            if len(selected_for_technique) >= top_per_technique:
                break

    selected.sort(key=lambda item: (item["anchor_time"], item["selected_technique_id"], item["segment_id"]))
    return selected


def merge_anchors_file(
    anchors_path: Path,
    gap_seconds: int,
    raw_anchors_path: Path | None = None,
    progress: bool = False,
) -> tuple[int, int]:
    source_path = raw_anchors_path or anchors_path
    if gap_seconds < 0:
        if raw_anchors_path is not None and raw_anchors_path != anchors_path:
            shutil.copyfile(raw_anchors_path, anchors_path)
        with anchors_path.open(encoding="utf-8") as handle:
            count = sum(1 for _ in handle)
        return count, count

    anchors = []
    read_progress = ProgressBar("merge anchors read", source_path.stat().st_size, enabled=progress)
    bytes_read = 0
    with source_path.open(encoding="utf-8") as handle:
        for line in handle:
            bytes_read += len(line.encode("utf-8"))
            anchors.append(json.loads(line))
            read_progress.update(bytes_read, suffix=f"raw={len(anchors)}")
    read_progress.close(suffix=f"raw={len(anchors)}")

    merged = merge_anchor_records(anchors, gap_seconds, progress)
    tmp = anchors_path.with_suffix(anchors_path.suffix + ".tmp")
    write_progress = ProgressBar("merge anchors write", len(merged), enabled=progress)
    with tmp.open("w", encoding="utf-8") as handle:
        for index, anchor in enumerate(merged, 1):
            handle.write(json.dumps(anchor, ensure_ascii=False) + "\n")
            write_progress.update(index, suffix=f"merged={index}")
    write_progress.close(suffix=f"merged={len(merged)}")
    tmp.replace(anchors_path)
    return len(anchors), len(merged)


def refresh_anchor_checkpoint(anchors_path: Path, raw_path: Path, gap_seconds: int, original_count: int, merged_count: int) -> None:
    checkpoint_path = anchors_path.parent / "checkpoint.json"
    if not checkpoint_path.exists():
        return
    checkpoint = load_checkpoint(checkpoint_path)
    if checkpoint.get("anchors") != str(anchors_path):
        return
    checkpoint["raw_anchors"] = str(raw_path)
    checkpoint["original_anchor_count"] = original_count
    checkpoint["merged_anchor_count"] = merged_count
    checkpoint["merge_anchor_gap_seconds"] = gap_seconds
    if checkpoint.get("phase") == "anchors":
        checkpoint["phase"] = "anchors_done"
    save_checkpoint(checkpoint_path, checkpoint)


def stream_extract(args: argparse.Namespace) -> None:
    if args.ordered:
        stream_extract_ordered(args)
    else:
        stream_extract_two_pass(args)


def collect_anchors(args: argparse.Namespace) -> None:
    zip_path = Path(args.zip)
    anchors_path = Path(args.anchors)
    checkpoint_path = Path(args.checkpoint)
    checkpoint = load_checkpoint(checkpoint_path)
    raw_anchors_path = prepare_raw_anchors_path(anchors_path, checkpoint, args.resume)

    anchor_fields = tuple(args.anchor_field or DEFAULT_ANCHOR_FIELDS)
    before = timedelta(seconds=args.before_seconds)
    after = timedelta(seconds=args.after_seconds)

    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    raw_anchors_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    phase = checkpoint.get("phase") if args.resume else None
    if phase == "anchors_done":
        print(json.dumps(checkpoint, indent=2))
        return

    resume_lines = int(checkpoint.get("lines_read", 0)) if args.resume and phase == "anchors" else 0
    anchors_mode = "a" if args.resume and resume_lines and raw_anchors_path.exists() else "w"
    anchors_found = int(checkpoint.get("anchors_found", 0)) if anchors_mode == "a" else 0
    lines_read = 0
    uncompressed_bytes = 0

    zf, member = open_zip_member(zip_path, args.member)
    try:
        total_bytes = zf.getinfo(member).file_size
        progress = ProgressBar("anchors", total_bytes, enabled=args.progress)
        with zf.open(member, "r") as src, raw_anchors_path.open(anchors_mode, encoding="utf-8") as anchors_out:
            for raw_line in src:
                lines_read += 1
                uncompressed_bytes += len(raw_line)
                if lines_read <= resume_lines:
                    progress.update(
                        uncompressed_bytes,
                        suffix=f"fast-forward={lines_read}/{resume_lines} found={anchors_found}",
                    )
                    continue
                progress.update(uncompressed_bytes, suffix=f"lines={lines_read} found={anchors_found}")
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    print(f"Skipping invalid JSON at line {lines_read}: {exc}", file=sys.stderr)
                    continue
                source = get_source(obj)
                ts = event_time(source)
                anchor = extract_anchor(source, anchor_fields)
                if ts is not None and anchor is not None:
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    host = event_host(source)
                    segment_id = stable_segment_id(member, lines_read, anchor)
                    anchors_out.write(
                        json.dumps(
                            {
                                "segment_id": segment_id,
                                "anchor_line": lines_read,
                                "anchor_time": ts.isoformat(),
                                "start_time": (ts - before).isoformat(),
                                "end_time": (ts + after).isoformat(),
                                "host": host,
                                "label": anchor,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    anchors_found += 1
                    if args.max_segments and anchors_found >= args.max_segments:
                        break
                if lines_read % args.checkpoint_every == 0:
                    save_checkpoint(
                        checkpoint_path,
                        {
                            "phase": "anchors",
                            "zip": str(zip_path),
                            "member": member,
                            "anchors": str(anchors_path),
                            "raw_anchors": str(raw_anchors_path),
                            "lines_read": lines_read,
                            "uncompressed_bytes": uncompressed_bytes,
                            "anchors_found": anchors_found,
                        },
                    )
                if args.max_lines and lines_read >= args.max_lines:
                    break
        progress.close(suffix=f"lines={lines_read} found={anchors_found}")
    finally:
        zf.close()

    original_anchor_count, merged_anchor_count = merge_anchors_file(
        anchors_path,
        args.merge_anchor_gap_seconds,
        raw_anchors_path,
        args.progress,
    )
    result = {
        "phase": "anchors_done",
        "zip": str(zip_path),
        "member": member,
        "anchors": str(anchors_path),
        "raw_anchors": str(raw_anchors_path),
        "lines_read": lines_read,
        "uncompressed_bytes": uncompressed_bytes,
        "anchors_found": anchors_found,
        "original_anchor_count": original_anchor_count,
        "merged_anchor_count": merged_anchor_count,
        "merge_anchor_gap_seconds": args.merge_anchor_gap_seconds,
    }
    save_checkpoint(checkpoint_path, result)
    print(json.dumps(result, indent=2))


def stream_extract_two_pass(args: argparse.Namespace) -> None:
    zip_path = Path(args.zip)
    output_path = Path(args.output)
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
    anchors_path = Path(args.anchors) if args.anchors else output_path.with_suffix(output_path.suffix + ".anchors.jsonl")
    checkpoint = load_checkpoint(checkpoint_path)
    raw_anchors_path = prepare_raw_anchors_path(anchors_path, checkpoint, args.resume)

    anchor_fields = tuple(args.anchor_field or DEFAULT_ANCHOR_FIELDS)
    keep_fields = tuple(args.keep_field or DEFAULT_KEEP_FIELDS)
    label_fields = tuple(args.label_field or DEFAULT_LABEL_FIELDS)
    before = timedelta(seconds=args.before_seconds)
    after = timedelta(seconds=args.after_seconds)
    max_window = before + after

    output_path.parent.mkdir(parents=True, exist_ok=True)
    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    raw_anchors_path.parent.mkdir(parents=True, exist_ok=True)

    phase = checkpoint.get("phase") if args.resume else None
    if phase == "done":
        print(json.dumps(checkpoint, indent=2))
        return
    if phase not in ("anchors_done", "context"):
        resume_lines = int(checkpoint.get("lines_read", 0)) if args.resume and phase == "anchors" else 0
        anchors_mode = "a" if args.resume and resume_lines and raw_anchors_path.exists() else "w"
        zf, member = open_zip_member(zip_path, args.member)
        lines_read = 0
        uncompressed_bytes = 0
        anchors_found = int(checkpoint.get("anchors_found", 0)) if anchors_mode == "a" else 0
        try:
            total_bytes = zf.getinfo(member).file_size
            progress = ProgressBar("extract anchors", total_bytes, enabled=args.progress)
            with zf.open(member, "r") as src, raw_anchors_path.open(anchors_mode, encoding="utf-8") as anchors_out:
                for raw_line in src:
                    lines_read += 1
                    uncompressed_bytes += len(raw_line)
                    progress.update(uncompressed_bytes, suffix=f"lines={lines_read} found={anchors_found}")
                    if lines_read <= resume_lines:
                        continue
                    try:
                        obj = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        print(f"Skipping invalid JSON at line {lines_read}: {exc}", file=sys.stderr)
                        continue
                    source = get_source(obj)
                    ts = event_time(source)
                    anchor = extract_anchor(source, anchor_fields)
                    if ts is not None and anchor is not None:
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        host = event_host(source)
                        segment_id = stable_segment_id(member, lines_read, anchor)
                        anchors_out.write(
                            json.dumps(
                                {
                                    "segment_id": segment_id,
                                    "anchor_line": lines_read,
                                    "anchor_time": ts.isoformat(),
                                    "start_time": (ts - before).isoformat(),
                                    "end_time": (ts + after).isoformat(),
                                    "host": host,
                                    "label": anchor,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        anchors_found += 1
                        if args.max_segments and anchors_found >= args.max_segments:
                            break
                    if lines_read % args.checkpoint_every == 0:
                        save_checkpoint(
                            checkpoint_path,
                            {
                                "phase": "anchors",
                                "zip": str(zip_path),
                                "member": member,
                                "anchors": str(anchors_path),
                                "raw_anchors": str(raw_anchors_path),
                                "lines_read": lines_read,
                                "uncompressed_bytes": uncompressed_bytes,
                                "anchors_found": anchors_found,
                            },
                        )
                    if args.max_lines and lines_read >= args.max_lines:
                        break
            progress.close(suffix=f"lines={lines_read} found={anchors_found}")
        finally:
            zf.close()
        original_anchor_count, merged_anchor_count = merge_anchors_file(
            anchors_path,
            args.merge_anchor_gap_seconds,
            raw_anchors_path,
            args.progress,
        )
        save_checkpoint(
            checkpoint_path,
            {
                "phase": "anchors_done",
                "zip": str(zip_path),
                "member": member,
                "anchors": str(anchors_path),
                "raw_anchors": str(raw_anchors_path),
                "lines_read": lines_read,
                "uncompressed_bytes": uncompressed_bytes,
                "anchors_found": anchors_found,
                "original_anchor_count": original_anchor_count,
                "merged_anchor_count": merged_anchor_count,
                "merge_anchor_gap_seconds": args.merge_anchor_gap_seconds,
            },
        )

    anchors = load_anchor_index(anchors_path, args.same_host)
    checkpoint = load_checkpoint(checkpoint_path)
    resume_lines = int(checkpoint.get("context_lines_read", 0)) if args.resume and checkpoint.get("phase") == "context" else 0
    records_written = int(checkpoint.get("records_written", 0)) if args.resume and output_path.exists() else 0
    output_mode = "a" if args.resume and output_path.exists() else "w"
    zf, member = open_zip_member(zip_path, args.member)
    lines_read = 0
    uncompressed_bytes = 0
    try:
        total_bytes = zf.getinfo(member).file_size
        progress = ProgressBar("extract context", total_bytes, enabled=args.progress)
        with zf.open(member, "r") as src, output_path.open(output_mode, encoding="utf-8") as out:
            for raw_line in src:
                lines_read += 1
                uncompressed_bytes += len(raw_line)
                progress.update(uncompressed_bytes, suffix=f"lines={lines_read} written={records_written}")
                if lines_read <= resume_lines:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    print(f"Skipping invalid JSON at line {lines_read}: {exc}", file=sys.stderr)
                    continue
                source = get_source(obj)
                ts = event_time(source)
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                host = event_host(source)
                reduced = reduce_event(obj, keep_fields)
                for anchor in find_matching_anchors(anchors, ts, host, args.same_host, max_window):
                    record = build_segment_event_record(member, lines_read, anchor, reduced, label_fields)
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    records_written += 1
                if lines_read % args.checkpoint_every == 0:
                    save_checkpoint(
                        checkpoint_path,
                        {
                            "phase": "context",
                            "zip": str(zip_path),
                            "member": member,
                            "anchors": str(anchors_path),
                            "output": str(output_path),
                            "context_lines_read": lines_read,
                            "context_uncompressed_bytes": uncompressed_bytes,
                            "records_written": records_written,
                        },
                    )
                if args.max_lines and lines_read >= args.max_lines:
                    break
        progress.close(suffix=f"lines={lines_read} written={records_written}")
    finally:
        zf.close()

    save_checkpoint(
        checkpoint_path,
        {
            "phase": "done",
            "zip": str(zip_path),
            "member": member,
            "anchors": str(anchors_path),
            "raw_anchors": str(raw_anchors_path),
            "output": str(output_path),
            "context_lines_read": lines_read,
            "context_uncompressed_bytes": uncompressed_bytes,
            "records_written": records_written,
            "original_anchor_count": checkpoint.get("original_anchor_count"),
            "merged_anchor_count": sum(1 for _ in anchors_path.open(encoding="utf-8")),
            "merge_anchor_gap_seconds": args.merge_anchor_gap_seconds,
        },
    )
    print(
        json.dumps(
            {
                "anchors": str(anchors_path),
                "output": str(output_path),
                "records_written": records_written,
                "checkpoint": str(checkpoint_path) if checkpoint_path else None,
            },
            indent=2,
        )
    )


def load_anchor_index(anchors_path: Path, same_host: bool) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    with anchors_path.open(encoding="utf-8") as handle:
        for line in handle:
            anchor = json.loads(line)
            anchor["start_dt"] = parse_timestamp(anchor["start_time"])
            anchor["end_dt"] = parse_timestamp(anchor["end_time"])
            if anchor["start_dt"] is None or anchor["end_dt"] is None:
                continue
            key = anchor["host"] if same_host else "*"
            buckets.setdefault(key or "*", []).append(anchor)
    indexed = {}
    for key, items in buckets.items():
        items.sort(key=lambda item: item["start_dt"])
        indexed[key] = {"starts": [item["start_dt"] for item in items], "items": items}
    return indexed


def find_matching_anchors(
    anchors: dict[str, Any],
    ts: datetime,
    host: str | None,
    same_host: bool,
    max_window: timedelta,
) -> list[dict[str, Any]]:
    key = host if same_host else "*"
    bucket = anchors.get(key or "*")
    if not bucket:
        return []
    starts = bucket["starts"]
    items = bucket["items"]
    pos = bisect_right(starts, ts)
    matches = []
    for idx in range(pos - 1, -1, -1):
        anchor = items[idx]
        if anchor["start_dt"] <= ts <= anchor["end_dt"]:
            matches.append(anchor)
    return matches


def stream_extract_ordered(args: argparse.Namespace) -> None:
    zip_path = Path(args.zip)
    output_path = Path(args.output)
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
    checkpoint = load_checkpoint(checkpoint_path)

    resume_lines = int(checkpoint.get("lines_read", 0)) if args.resume else 0
    resume_bytes = int(checkpoint.get("uncompressed_bytes", 0)) if args.resume else 0

    anchor_fields = tuple(args.anchor_field or DEFAULT_ANCHOR_FIELDS)
    keep_fields = tuple(args.keep_field or DEFAULT_KEEP_FIELDS)
    label_fields = tuple(args.label_field or DEFAULT_LABEL_FIELDS)
    before = timedelta(seconds=args.before_seconds)
    after = timedelta(seconds=args.after_seconds)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.resume and output_path.exists() else "w"

    lines_read = 0
    uncompressed_bytes = 0
    segments_written = int(checkpoint.get("segments_written", 0)) if mode == "a" else 0
    buffer: list[tuple[datetime, str | None, dict[str, Any]]] = []
    active: list[Segment] = []

    zf, member = open_zip_member(zip_path, args.member)
    try:
        total_bytes = zf.getinfo(member).file_size
        progress = ProgressBar("extract ordered", total_bytes, enabled=args.progress)
        with zf.open(member, "r") as src, output_path.open(mode, encoding="utf-8") as out:
            for raw_line in src:
                lines_read += 1
                uncompressed_bytes += len(raw_line)
                progress.update(uncompressed_bytes, suffix=f"lines={lines_read} segments={segments_written}")

                if lines_read <= resume_lines or uncompressed_bytes <= resume_bytes:
                    continue

                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    print(f"Skipping invalid JSON at line {lines_read}: {exc}", file=sys.stderr)
                    continue

                source = get_source(obj)
                ts = event_time(source)
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                host = event_host(source)
                reduced = reduce_event(obj, keep_fields)

                buffer = [(item_ts, item_host, item) for item_ts, item_host, item in buffer if item_ts >= ts - before]

                still_active: list[Segment] = []
                for segment in active:
                    if segment.accepts(ts, host, args.same_host):
                        if len(segment.events) < args.max_events_per_segment:
                            segment.events.append(reduced)
                        still_active.append(segment)
                    elif ts > segment.end_time:
                        segment_record = build_ordered_segment_record(segment, label_fields)
                        out.write(json.dumps(segment_record, ensure_ascii=False) + "\n")
                        segments_written += 1
                    else:
                        still_active.append(segment)
                active = still_active

                anchor = extract_anchor(source, anchor_fields)
                if anchor is not None:
                    segment = Segment(
                        segment_id=stable_segment_id(member, lines_read, anchor),
                        anchor_line=lines_read,
                        anchor_time=ts,
                        end_time=ts + after,
                        anchor_host=host,
                        label=anchor,
                    )
                    start_time = ts - before
                    for buffered_ts, buffered_host, buffered_event in buffer:
                        if (
                            start_time <= buffered_ts <= ts
                            and (not args.same_host or host is None or buffered_host == host)
                        ):
                            if len(segment.events) < args.max_events_per_segment:
                                segment.events.append(buffered_event)
                    if len(segment.events) < args.max_events_per_segment:
                        segment.events.append(reduced)
                    active.append(segment)

                buffer.append((ts, host, reduced))

                if lines_read % args.checkpoint_every == 0:
                    save_checkpoint(
                        checkpoint_path,
                        {
                            "zip": str(zip_path),
                            "member": member,
                            "output": str(output_path),
                            "lines_read": lines_read,
                            "uncompressed_bytes": uncompressed_bytes,
                            "segments_written": segments_written,
                        },
                    )

                if args.max_lines and lines_read >= args.max_lines:
                    break
                if args.max_segments and segments_written >= args.max_segments:
                    break

            for segment in active:
                segment_record = build_ordered_segment_record(segment, label_fields)
                out.write(json.dumps(segment_record, ensure_ascii=False) + "\n")
                segments_written += 1
        progress.close(suffix=f"lines={lines_read} segments={segments_written}")

    finally:
        zf.close()

    save_checkpoint(
        checkpoint_path,
        {
            "zip": str(zip_path),
            "member": member,
            "output": str(output_path),
            "lines_read": lines_read,
            "uncompressed_bytes": uncompressed_bytes,
            "segments_written": segments_written,
            "finished": not args.max_lines and not args.max_segments,
        },
    )
    print(
        json.dumps(
            {
                "lines_read": lines_read,
                "uncompressed_bytes": uncompressed_bytes,
                "segments_written": segments_written,
                "output": str(output_path),
                "checkpoint": str(checkpoint_path) if checkpoint_path else None,
            },
            indent=2,
        )
    )


def inspect_zip(args: argparse.Namespace) -> None:
    zip_path = Path(args.zip)
    zf, member = open_zip_member(zip_path, args.member)
    try:
        info = zf.getinfo(member)
        print(
            json.dumps(
                {
                    "zip": str(zip_path),
                    "member": member,
                    "compressed_size": info.compress_size,
                    "uncompressed_size": info.file_size,
                },
                indent=2,
            )
        )
    finally:
        zf.close()


def scan_labels(args: argparse.Namespace) -> None:
    zip_path = Path(args.zip)
    anchor_fields = tuple(args.anchor_field or DEFAULT_ANCHOR_FIELDS)
    pattern = re.compile(args.key_regex, re.IGNORECASE)
    found = 0
    zf, member = open_zip_member(zip_path, args.member)
    try:
        with zf.open(member, "r") as src:
            for line_number, raw_line in enumerate(src, 1):
                obj = json.loads(raw_line)
                source = get_source(obj)
                anchor = extract_anchor(source, anchor_fields)
                matching_keys = [key for key in source if pattern.search(key)]
                if anchor or matching_keys:
                    print(
                        json.dumps(
                            {
                                "line": line_number,
                                "anchor": anchor,
                                "matching_keys": {key: source.get(key) for key in matching_keys},
                            },
                            ensure_ascii=False,
                        )
                    )
                    found += 1
                    if found >= args.limit:
                        break
                if args.max_lines and line_number >= args.max_lines:
                    break
    finally:
        zf.close()


def select_best_anchors_command(args: argparse.Namespace) -> None:
    anchors_path = Path(args.anchors)
    output_path = Path(args.output)

    if args.top_per_technique < 1:
        raise SystemExit("--top-per-technique must be >= 1")
    if args.min_time_gap_seconds < 0:
        raise SystemExit("--min-time-gap-seconds must be >= 0")

    anchors = load_anchor_candidates(anchors_path)
    if not anchors:
        raise SystemExit(f"No valid anchors found in {anchors_path}")

    selected = choose_best_anchor_for_techniques(anchors, args.top_per_technique, args.min_time_gap_seconds)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for anchor in selected:
            handle.write(json.dumps(anchor, ensure_ascii=False) + "\n")

    print(
        json.dumps(
            {
                "input_anchors": str(anchors_path),
                "output": str(output_path),
                "candidate_anchor_count": len(anchors),
                "selected_anchor_count": len(selected),
                "selection_mode": "top_anchor_lines_per_technique",
                "top_per_technique": args.top_per_technique,
                "min_time_gap_seconds": args.min_time_gap_seconds,
            },
            indent=2,
        )
    )


def merge_anchors_command(args: argparse.Namespace) -> None:
    anchors_path = Path(args.anchors)
    raw_path = Path(args.raw_anchors) if args.raw_anchors else default_raw_anchors_path(anchors_path)
    if not raw_path.exists() and anchors_path.exists():
        shutil.copyfile(anchors_path, raw_path)
    original_count, merged_count = merge_anchors_file(anchors_path, args.merge_anchor_gap_seconds, raw_path, args.progress)
    refresh_anchor_checkpoint(anchors_path, raw_path, args.merge_anchor_gap_seconds, original_count, merged_count)
    print(
        json.dumps(
            {
                "anchors": args.anchors,
                "raw_anchors": str(raw_path),
                "original_anchor_count": original_count,
                "merged_anchor_count": merged_count,
                "merge_anchor_gap_seconds": args.merge_anchor_gap_seconds,
            },
            indent=2,
        )
    )


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def technique_filename(anchor: dict[str, Any], counts: dict[str, int]) -> str:
    technique_id = anchor.get("selected_technique_id")
    if not technique_id:
        technique_ids = normalized_technique_ids(anchor.get("label", {}))
        technique_id = technique_ids[0] if technique_ids else "unknown"
    base = re.sub(r"[^A-Za-z0-9]+", "_", str(technique_id)).strip("_") or "unknown"
    counts[base] = counts.get(base, 0) + 1
    if counts[base] == 1:
        return f"{base}.jsonl"
    return f"{base}_{counts[base]:03d}.jsonl"


def normalize_anchor_window(anchor: dict[str, Any], before_seconds: int, after_seconds: int) -> dict[str, Any]:
    anchor_time = parse_timestamp(anchor.get("anchor_time"))
    if anchor_time is None:
        raise SystemExit(f"Anchor {anchor.get('segment_id', '<unknown>')} has invalid anchor_time")
    if anchor_time.tzinfo is None:
        anchor_time = anchor_time.replace(tzinfo=timezone.utc)
    normalized = dict(anchor)
    normalized["anchor_time"] = anchor_time.isoformat()
    normalized["start_time"] = (anchor_time - timedelta(seconds=before_seconds)).isoformat()
    normalized["end_time"] = (anchor_time + timedelta(seconds=after_seconds)).isoformat()
    normalized["window_before_seconds"] = before_seconds
    normalized["window_after_seconds"] = after_seconds
    return normalized


def normalize_anchor_windows(
    anchors: list[dict[str, Any]],
    before_seconds: int,
    after_seconds: int,
) -> list[dict[str, Any]]:
    return [normalize_anchor_window(anchor, before_seconds, after_seconds) for anchor in anchors]


def line_window_for_anchor(anchor_line: int, events_per_segment: int, total_lines: int | None = None) -> tuple[int, int]:
    if events_per_segment < 1:
        raise SystemExit("--events-per-segment must be >= 1")
    before = events_per_segment // 2
    after = events_per_segment - before - 1
    start = max(1, anchor_line - before)
    end = anchor_line + after
    if total_lines is not None and end > total_lines:
        overflow = end - total_lines
        end = total_lines
        start = max(1, start - overflow)
    return start, end


def add_anchor_line_window(
    anchor: dict[str, Any],
    events_per_segment: int,
    total_lines: int | None = None,
) -> dict[str, Any]:
    anchor_line = int(anchor.get("resolved_anchor_line") or anchor["anchor_line"])
    line_start, line_end = line_window_for_anchor(anchor_line, events_per_segment, total_lines)
    updated = dict(anchor)
    updated["anchor_line"] = anchor_line
    updated["line_start"] = line_start
    updated["line_end"] = line_end
    updated["events_per_segment"] = events_per_segment
    return updated


def add_anchor_line_windows(
    anchors: list[dict[str, Any]],
    events_per_segment: int,
    total_lines: int | None = None,
) -> list[dict[str, Any]]:
    return [add_anchor_line_window(anchor, events_per_segment, total_lines) for anchor in anchors]


def prepare_best_anchors(
    anchors_path: Path,
    top_per_technique: int,
    min_time_gap_seconds: int,
    select_best: bool,
) -> list[dict[str, Any]]:
    if not select_best:
        anchors = load_jsonl(anchors_path)
        if not anchors:
            raise SystemExit(f"No anchors found in {anchors_path}")
        return anchors

    anchors = load_anchor_candidates(anchors_path)
    if not anchors:
        raise SystemExit(f"No valid anchors found in {anchors_path}")
    return choose_best_anchor_for_techniques(anchors, top_per_technique, min_time_gap_seconds)


def selected_anchor_technique(anchor: dict[str, Any]) -> str | None:
    value = anchor.get("selected_technique_id")
    if value:
        return str(value).strip().upper()
    technique_ids = normalized_technique_ids(anchor.get("label", {}))
    return technique_ids[0] if technique_ids else None


def anchor_original_bounds(anchor: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    start = parse_timestamp(anchor.get("source_start_time") or anchor.get("start_time"))
    end = parse_timestamp(anchor.get("source_end_time") or anchor.get("end_time"))
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start, end


def event_technique_ids(source: dict[str, Any], anchor_fields: tuple[str, ...]) -> set[str]:
    anchor = extract_anchor(source, anchor_fields)
    if not anchor:
        return set()
    return set(normalized_technique_ids(anchor))


def resolve_lab_anchor(
    anchor: dict[str, Any],
    member: str,
    line_number: int,
    ts: datetime,
    source: dict[str, Any],
    before_seconds: int,
    after_seconds: int,
) -> dict[str, Any]:
    resolved = dict(anchor)
    resolved["source_anchor_line"] = anchor.get("anchor_line")
    resolved["source_anchor_time"] = anchor.get("anchor_time")
    resolved["source_start_time"] = anchor.get("start_time")
    resolved["source_end_time"] = anchor.get("end_time")
    resolved["anchor_line"] = line_number
    resolved["resolved_anchor_line"] = line_number
    resolved["resolved_event_id"] = stable_event_id(member, line_number, reduce_event(source, DEFAULT_KEEP_FIELDS))
    resolved["resolution_mode"] = "first_matching_technique_event"
    resolved["anchor_time"] = ts.isoformat()
    return normalize_anchor_window(resolved, before_seconds, after_seconds)


def resolve_lab_anchors_from_zip(
    zip_path: Path,
    member_arg: str | None,
    anchors: list[dict[str, Any]],
    before_seconds: int,
    after_seconds: int,
    same_host: bool,
    anchor_fields: tuple[str, ...],
    progress_enabled: bool,
    max_lines: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    pending: dict[str, dict[str, Any]] = {}
    for anchor in anchors:
        technique_id = selected_anchor_technique(anchor)
        if not technique_id:
            raise SystemExit(f"Anchor {anchor.get('segment_id', '<unknown>')} has no selected technique id")
        pending[technique_id] = anchor

    resolved_by_technique: dict[str, dict[str, Any]] = {}
    lines_read = 0
    zf, member = open_zip_member(zip_path, member_arg)
    try:
        total_bytes = zf.getinfo(member).file_size
        progress = ProgressBar("resolve lab anchors", total_bytes, enabled=progress_enabled)
        uncompressed_bytes = 0
        with zf.open(member, "r") as src:
            for raw_line in src:
                lines_read += 1
                if max_lines and lines_read > max_lines:
                    lines_read = max_lines
                    break
                uncompressed_bytes += len(raw_line)
                progress.update(uncompressed_bytes, suffix=f"lines={lines_read} resolved={len(resolved_by_technique)}")
                if not pending:
                    break
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    print(f"Skipping invalid JSON at line {lines_read}: {exc}", file=sys.stderr)
                    continue
                source = get_source(obj)
                ts = event_time(source)
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                host = event_host(source)
                matching_techniques = event_technique_ids(source, anchor_fields) & set(pending)
                for technique_id in sorted(matching_techniques):
                    anchor = pending[technique_id]
                    if same_host and anchor.get("host") and host != anchor.get("host"):
                        continue
                    start, end = anchor_original_bounds(anchor)
                    if start is not None and ts < start:
                        continue
                    if end is not None and ts > end:
                        continue
                    resolved_by_technique[technique_id] = resolve_lab_anchor(
                        anchor,
                        member,
                        lines_read,
                        ts,
                        source,
                        before_seconds,
                        after_seconds,
                    )
                    del pending[technique_id]
                if not pending:
                    break
        progress.close(suffix=f"lines={lines_read} resolved={len(resolved_by_technique)}")
    finally:
        zf.close()

    if pending:
        missing = ", ".join(sorted(pending))
        raise SystemExit(f"Could not resolve lab anchors for techniques: {missing}")

    resolved = [resolved_by_technique[selected_anchor_technique(anchor) or ""] for anchor in anchors]
    return resolved, lines_read


def build_anchor_file_map(
    anchors: list[dict[str, Any]],
    output_dir: Path,
    source: str,
) -> dict[str, dict[str, Any]]:
    by_segment: dict[str, dict[str, Any]] = {}
    technique_counts: dict[str, int] = {}
    for index, anchor in enumerate(anchors, 1):
        if source == "lab":
            filename = technique_filename(anchor, technique_counts)
        else:
            filename = f"real_{index:03d}.jsonl"
        by_segment[str(anchor["segment_id"])] = {
            "anchor": anchor,
            "path": output_dir / filename,
            "source": source,
            "event_count": 0,
            "first_event_line": None,
            "last_event_line": None,
            "first_event_time": None,
            "last_event_time": None,
        }
    return by_segment


def update_manifest_stats(info: dict[str, Any], record: dict[str, Any]) -> None:
    event_line = record.get("event_line")
    if isinstance(event_line, int):
        info["first_event_line"] = min(info["first_event_line"], event_line) if info["first_event_line"] else event_line
        info["last_event_line"] = max(info["last_event_line"], event_line) if info["last_event_line"] else event_line
    timestamp = event_time(record.get("llm_event", {}))
    timestamp_text = timestamp.isoformat() if timestamp is not None else None
    info["event_count"] += 1
    if timestamp_text:
        info["first_event_time"] = min(info["first_event_time"], timestamp_text) if info["first_event_time"] else timestamp_text
        info["last_event_time"] = max(info["last_event_time"], timestamp_text) if info["last_event_time"] else timestamp_text


def write_manifest(path: Path, segment_infos: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "path",
            "segment_id",
            "source",
            "anchor_line",
            "line_start",
            "line_end",
            "anchor_time",
            "host",
            "label",
            "event_count",
            "first_event_line",
            "last_event_line",
            "first_event_time",
            "last_event_time",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for segment_id, info in sorted(segment_infos.items(), key=lambda item: str(item[1]["path"])):
            anchor = info["anchor"]
            writer.writerow(
                {
                    "path": str(info["path"]),
                    "segment_id": segment_id,
                    "source": info["source"],
                    "anchor_line": anchor.get("anchor_line"),
                    "line_start": anchor.get("line_start"),
                    "line_end": anchor.get("line_end"),
                    "anchor_time": anchor.get("anchor_time"),
                    "host": anchor.get("host"),
                    "label": json.dumps(anchor.get("label", {}), ensure_ascii=False, sort_keys=True),
                    "event_count": info["event_count"],
                    "first_event_line": info["first_event_line"],
                    "last_event_line": info["last_event_line"],
                    "first_event_time": info["first_event_time"],
                    "last_event_time": info["last_event_time"],
                }
            )


def extract_segment_files_from_anchors(
    zip_path: Path,
    member_arg: str | None,
    anchors: list[dict[str, Any]],
    output_dir: Path,
    source: str,
    same_host: bool,
    keep_fields: tuple[str, ...],
    label_fields: tuple[str, ...],
    progress_enabled: bool,
    max_lines: int | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    segment_infos = build_anchor_file_map(anchors, output_dir, source)
    for info in segment_infos.values():
        info["path"].write_text("", encoding="utf-8")

    anchor_path = output_dir / ".anchors.tmp.jsonl"
    write_jsonl(anchor_path, anchors)
    indexed_anchors = load_anchor_index(anchor_path, same_host)
    anchor_path.unlink(missing_ok=True)

    handles: dict[str, Any] = {}
    lines_read = 0
    records_written = 0
    zf, member = open_zip_member(zip_path, member_arg)
    try:
        total_bytes = zf.getinfo(member).file_size
        progress = ProgressBar(f"{source} dataset", total_bytes, enabled=progress_enabled)
        uncompressed_bytes = 0
        with zf.open(member, "r") as src:
            for raw_line in src:
                lines_read += 1
                if max_lines and lines_read > max_lines:
                    lines_read = max_lines
                    break
                uncompressed_bytes += len(raw_line)
                progress.update(uncompressed_bytes, suffix=f"lines={lines_read} written={records_written}")
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    print(f"Skipping invalid JSON at line {lines_read}: {exc}", file=sys.stderr)
                    continue
                source_obj = get_source(obj)
                ts = event_time(source_obj)
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                host = event_host(source_obj)
                reduced = reduce_event(obj, keep_fields)
                for anchor in find_matching_anchors(indexed_anchors, ts, host, same_host, timedelta(seconds=0)):
                    record = build_segment_event_record(member, lines_read, anchor, reduced, label_fields)
                    segment_id = str(record["segment_id"])
                    info = segment_infos[segment_id]
                    handle = handles.get(segment_id)
                    if handle is None:
                        handle = info["path"].open("a", encoding="utf-8")
                        handles[segment_id] = handle
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    update_manifest_stats(info, record)
                    records_written += 1
        progress.close(suffix=f"lines={lines_read} written={records_written}")
    finally:
        for handle in handles.values():
            handle.close()
        zf.close()
    return segment_infos, lines_read


def matching_line_window_anchors(anchors: list[dict[str, Any]], line_number: int) -> list[dict[str, Any]]:
    return [
        anchor
        for anchor in anchors
        if int(anchor.get("line_start", anchor.get("anchor_line", 0))) <= line_number <= int(anchor.get("line_end", 0))
    ]


def write_line_window_record(
    obj: dict[str, Any],
    member: str,
    line_number: int,
    anchor: dict[str, Any],
    label_fields: tuple[str, ...],
    keep_fields: tuple[str, ...],
    segment_infos: dict[str, dict[str, Any]],
    handles: dict[str, Any],
) -> int:
    reduced = reduce_event(obj, keep_fields)
    record = build_segment_event_record(member, line_number, anchor, reduced, label_fields)
    segment_id = str(record["segment_id"])
    info = segment_infos[segment_id]
    handle = handles.get(segment_id)
    if handle is None:
        handle = info["path"].open("a", encoding="utf-8")
        handles[segment_id] = handle
    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    update_manifest_stats(info, record)
    return 1


def extract_segment_files_from_zip_line_windows(
    zip_path: Path,
    member_arg: str | None,
    anchors: list[dict[str, Any]],
    output_dir: Path,
    source: str,
    keep_fields: tuple[str, ...],
    label_fields: tuple[str, ...],
    progress_enabled: bool,
    max_lines: int | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    segment_infos = build_anchor_file_map(anchors, output_dir, source)
    for info in segment_infos.values():
        info["path"].write_text("", encoding="utf-8")

    max_needed_line = max(int(anchor["line_end"]) for anchor in anchors) if anchors else 0
    handles: dict[str, Any] = {}
    lines_read = 0
    records_written = 0
    zf, member = open_zip_member(zip_path, member_arg)
    try:
        total_bytes = zf.getinfo(member).file_size
        progress = ProgressBar(f"{source} line windows", total_bytes, enabled=progress_enabled)
        uncompressed_bytes = 0
        with zf.open(member, "r") as src:
            for raw_line in src:
                lines_read += 1
                if max_lines and lines_read > max_lines:
                    lines_read = max_lines
                    break
                if lines_read > max_needed_line:
                    lines_read -= 1
                    break
                uncompressed_bytes += len(raw_line)
                progress.update(uncompressed_bytes, suffix=f"lines={lines_read} written={records_written}")
                matches = matching_line_window_anchors(anchors, lines_read)
                if not matches:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    print(f"Skipping invalid JSON at line {lines_read}: {exc}", file=sys.stderr)
                    continue
                for anchor in matches:
                    records_written += write_line_window_record(
                        obj,
                        member,
                        lines_read,
                        anchor,
                        label_fields,
                        keep_fields,
                        segment_infos,
                        handles,
                    )
        progress.close(suffix=f"lines={lines_read} written={records_written}")
    finally:
        for handle in handles.values():
            handle.close()
        zf.close()
    return segment_infos, lines_read


def extract_segment_files_from_file_line_windows(
    input_path: Path,
    anchors: list[dict[str, Any]],
    output_dir: Path,
    source: str,
    keep_fields: tuple[str, ...],
    label_fields: tuple[str, ...],
    progress_enabled: bool,
    max_lines: int | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    segment_infos = build_anchor_file_map(anchors, output_dir, source)
    for info in segment_infos.values():
        info["path"].write_text("", encoding="utf-8")

    max_needed_line = max(int(anchor["line_end"]) for anchor in anchors) if anchors else 0
    handles: dict[str, Any] = {}
    lines_read = 0
    records_written = 0
    total_bytes = input_path.stat().st_size if input_path.exists() else None
    bytes_read = 0
    progress = ProgressBar(f"{source} line windows", total_bytes, enabled=progress_enabled)
    try:
        with input_path.open(encoding="utf-8") as src:
            for raw_line in src:
                lines_read += 1
                if max_lines and lines_read > max_lines:
                    lines_read = max_lines
                    break
                if lines_read > max_needed_line:
                    lines_read -= 1
                    break
                bytes_read += len(raw_line.encode("utf-8"))
                progress.update(bytes_read, suffix=f"lines={lines_read} written={records_written}")
                matches = matching_line_window_anchors(anchors, lines_read)
                if not matches:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    print(f"Skipping invalid JSON at line {lines_read}: {exc}", file=sys.stderr)
                    continue
                for anchor in matches:
                    records_written += write_line_window_record(
                        obj,
                        str(input_path),
                        lines_read,
                        anchor,
                        label_fields,
                        keep_fields,
                        segment_infos,
                        handles,
                    )
        progress.close(suffix=f"lines={lines_read} written={records_written}")
    finally:
        for handle in handles.values():
            handle.close()
    return segment_infos, lines_read


def sample_real_anchors(
    zip_path: Path,
    member_arg: str | None,
    count: int,
    seed: int,
    before_seconds: int,
    after_seconds: int,
    progress_enabled: bool,
    max_lines: int | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    rng = random.Random(seed)
    reservoir: list[dict[str, Any]] = []
    valid_events = 0
    lines_read = 0
    zf, member = open_zip_member(zip_path, member_arg)
    try:
        total_bytes = zf.getinfo(member).file_size
        progress = ProgressBar("real sample", total_bytes, enabled=progress_enabled)
        uncompressed_bytes = 0
        with zf.open(member, "r") as src:
            for raw_line in src:
                lines_read += 1
                if max_lines and lines_read > max_lines:
                    lines_read = max_lines
                    break
                uncompressed_bytes += len(raw_line)
                progress.update(uncompressed_bytes, suffix=f"lines={lines_read} valid={valid_events}")
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    print(f"Skipping invalid JSON at line {lines_read}: {exc}", file=sys.stderr)
                    continue
                source_obj = get_source(obj)
                ts = event_time(source_obj)
                host = event_host(source_obj)
                if ts is None or host is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                valid_events += 1
                candidate = {
                    "line": lines_read,
                    "anchor_time": ts,
                    "host": host,
                }
                if len(reservoir) < count:
                    reservoir.append(candidate)
                else:
                    index = rng.randrange(valid_events)
                    if index < count:
                        reservoir[index] = candidate
        progress.close(suffix=f"lines={lines_read} valid={valid_events}")
    finally:
        zf.close()

    if len(reservoir) < count:
        raise SystemExit(f"Only {len(reservoir)} valid real events found; requested {count}")

    before = timedelta(seconds=before_seconds)
    after = timedelta(seconds=after_seconds)
    anchors = []
    for index, item in enumerate(sorted(reservoir, key=lambda value: (value["anchor_time"], value["line"])), 1):
        ts = item["anchor_time"]
        anchor = {
            "segment_id": stable_real_segment_id(member, item["line"], ts, item["host"], seed),
            "anchor_line": item["line"],
            "anchor_time": ts.isoformat(),
            "start_time": (ts - before).isoformat(),
            "end_time": (ts + after).isoformat(),
            "host": item["host"],
            "label": {
                "source": "real",
                "expected": "benign_or_unknown",
                "technique_ids": [],
            },
            "sample_index": index,
            "sample_seed": seed,
        }
        anchors.append(anchor)
    return anchors, lines_read, valid_events


def ensure_real_prefix_cache(
    zip_path: Path,
    member_arg: str | None,
    cache_path: Path,
    prefix_bytes: int,
    progress_enabled: bool,
) -> tuple[str, int]:
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return str(cache_path), cache_path.stat().st_size
    if prefix_bytes < 1:
        raise SystemExit("--real-prefix-bytes must be >= 1")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    bytes_written = 0
    zf, member = open_zip_member(zip_path, member_arg)
    try:
        total_bytes = min(zf.getinfo(member).file_size, prefix_bytes)
        progress = ProgressBar("real prefix cache", total_bytes, enabled=progress_enabled)
        with zf.open(member, "r") as src, tmp_path.open("wb") as out:
            for raw_line in src:
                if bytes_written >= prefix_bytes:
                    break
                out.write(raw_line)
                bytes_written += len(raw_line)
                progress.update(min(bytes_written, prefix_bytes), suffix=f"bytes={bytes_written}")
        progress.close(suffix=f"bytes={bytes_written}")
    finally:
        zf.close()
    tmp_path.replace(cache_path)
    return str(cache_path), bytes_written


def real_prefix_candidates(prefix_path: Path, progress_enabled: bool) -> tuple[list[dict[str, Any]], int]:
    candidates: list[dict[str, Any]] = []
    total_lines = 0
    total_bytes = prefix_path.stat().st_size
    bytes_read = 0
    progress = ProgressBar("real prefix scan", total_bytes, enabled=progress_enabled)
    with prefix_path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            total_lines += 1
            bytes_read += len(raw_line.encode("utf-8"))
            progress.update(bytes_read, suffix=f"lines={total_lines} candidates={len(candidates)}")
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                print(f"Skipping invalid JSON at prefix line {total_lines}: {exc}", file=sys.stderr)
                continue
            source = get_source(obj)
            ts = event_time(source)
            host = event_host(source)
            if ts is None or host is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            candidates.append(
                {
                    "line": total_lines,
                    "anchor_time": ts,
                    "host": host,
                }
            )
    progress.close(suffix=f"lines={total_lines} candidates={len(candidates)}")
    return candidates, total_lines


def intervals_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def sample_real_anchors_from_prefix(
    prefix_path: Path,
    count: int,
    seed: int,
    before_seconds: int,
    after_seconds: int,
    events_per_segment: int,
    progress_enabled: bool,
) -> tuple[list[dict[str, Any]], int, int]:
    candidates, total_lines = real_prefix_candidates(prefix_path, progress_enabled)
    rng = random.Random(seed)
    shuffled = list(candidates)
    rng.shuffle(shuffled)

    selected: list[dict[str, Any]] = []
    used_intervals: list[tuple[int, int]] = []
    for candidate in shuffled:
        line_start, line_end = line_window_for_anchor(candidate["line"], events_per_segment, total_lines)
        if (line_end - line_start + 1) < events_per_segment:
            continue
        interval = (line_start, line_end)
        if any(intervals_overlap(interval, used) for used in used_intervals):
            continue
        selected.append({**candidate, "line_start": line_start, "line_end": line_end})
        used_intervals.append(interval)
        if len(selected) >= count:
            break

    if len(selected) < count:
        raise SystemExit(
            f"Only {len(selected)} non-overlapping real windows found in {prefix_path}; requested {count}. "
            "Increase --real-prefix-bytes or reduce --real-count/--events-per-segment."
        )

    before = timedelta(seconds=before_seconds)
    after = timedelta(seconds=after_seconds)
    anchors = []
    for index, item in enumerate(sorted(selected, key=lambda value: value["line"]), 1):
        ts = item["anchor_time"]
        anchor = {
            "segment_id": stable_real_segment_id(str(prefix_path), item["line"], ts, item["host"], seed),
            "anchor_line": item["line"],
            "anchor_time": ts.isoformat(),
            "start_time": (ts - before).isoformat(),
            "end_time": (ts + after).isoformat(),
            "host": item["host"],
            "label": {
                "source": "real",
                "expected": "benign_or_unknown",
                "technique_ids": [],
            },
            "line_start": item["line_start"],
            "line_end": item["line_end"],
            "events_per_segment": events_per_segment,
            "sample_index": index,
            "sample_seed": seed,
            "real_prefix_cache": str(prefix_path),
        }
        anchors.append(anchor)
    return anchors, total_lines, len(candidates)


def prepare_dataset_command(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    lab_dir = output_dir / "lab"
    real_dir = output_dir / "real"
    best_anchors_path = Path(args.best_anchors)
    keep_fields = tuple(args.keep_field or DEFAULT_KEEP_FIELDS)
    label_fields = tuple(args.label_field or DEFAULT_LABEL_FIELDS)

    best_anchors = prepare_best_anchors(
        Path(args.lab_anchors),
        args.top_per_technique,
        args.min_time_gap_seconds,
        args.select_best_lab_anchors,
    )
    if args.expected_lab_segments and len(best_anchors) != args.expected_lab_segments:
        raise SystemExit(
            f"Expected {args.expected_lab_segments} lab anchors, selected {len(best_anchors)}. "
            "Adjust --expected-lab-segments for smoke tests."
        )
    best_anchors, lab_resolve_lines = resolve_lab_anchors_from_zip(
        Path(args.lab_zip),
        args.lab_member,
        best_anchors,
        args.before_seconds,
        args.after_seconds,
        args.same_host,
        tuple(args.anchor_field or DEFAULT_ANCHOR_FIELDS),
        args.progress,
        args.max_lab_resolve_lines,
    )
    best_anchors = add_anchor_line_windows(best_anchors, args.events_per_segment)
    write_jsonl(best_anchors_path, best_anchors)

    lab_infos, lab_lines = extract_segment_files_from_zip_line_windows(
        Path(args.lab_zip),
        args.lab_member,
        best_anchors,
        lab_dir,
        "lab",
        keep_fields,
        label_fields,
        args.progress,
        args.max_lab_lines,
    )
    if not args.max_lab_lines:
        empty_lab = [str(info["path"]) for info in lab_infos.values() if info["event_count"] == 0]
        if empty_lab:
            raise SystemExit(f"Lab extraction produced empty segment files: {', '.join(empty_lab[:5])}")
    write_manifest(lab_dir / "manifest.csv", lab_infos)

    real_prefix_path, real_prefix_bytes = ensure_real_prefix_cache(
        Path(args.real_zip),
        args.real_member,
        Path(args.real_prefix_cache),
        args.real_prefix_bytes,
        args.progress,
    )
    real_anchors, real_prefix_lines, real_valid_events = sample_real_anchors_from_prefix(
        Path(real_prefix_path),
        args.real_count,
        args.seed,
        args.before_seconds,
        args.after_seconds,
        args.events_per_segment,
        args.progress,
    )
    write_jsonl(real_dir / "anchors.sampled.jsonl", real_anchors)
    real_infos, real_extract_lines = extract_segment_files_from_file_line_windows(
        Path(real_prefix_path),
        real_anchors,
        real_dir,
        "real",
        keep_fields,
        label_fields,
        args.progress,
        args.max_real_extract_lines,
    )
    if not args.max_real_extract_lines:
        empty_real = [str(info["path"]) for info in real_infos.values() if info["event_count"] == 0]
        if empty_real:
            raise SystemExit(f"Real extraction produced empty segment files: {', '.join(empty_real[:5])}")
    write_manifest(real_dir / "manifest.csv", real_infos)

    print(
        json.dumps(
            {
                "best_anchors": str(best_anchors_path),
                "lab_files": len(lab_infos),
                "lab_records": sum(info["event_count"] for info in lab_infos.values()),
                "lab_resolve_lines_read": lab_resolve_lines,
                "lab_lines_read": lab_lines,
                "real_files": len(real_infos),
                "real_records": sum(info["event_count"] for info in real_infos.values()),
                "real_prefix_cache": real_prefix_path,
                "real_prefix_bytes": real_prefix_bytes,
                "real_prefix_lines": real_prefix_lines,
                "real_valid_events_seen": real_valid_events,
                "real_extract_lines_read": real_extract_lines,
                "events_per_segment": args.events_per_segment,
                "output_dir": str(output_dir),
            },
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_cmd = sub.add_parser("inspect", help="Show ZIP member sizes without extraction.")
    inspect_cmd.add_argument("--zip", required=True)
    inspect_cmd.add_argument("--member")
    inspect_cmd.set_defaults(func=inspect_zip)

    scan_cmd = sub.add_parser("scan-labels", help="Scan for rule/technique fields.")
    scan_cmd.add_argument("--zip", required=True)
    scan_cmd.add_argument("--member")
    scan_cmd.add_argument("--anchor-field", action="append")
    scan_cmd.add_argument("--key-regex", default=r"rule|technique")
    scan_cmd.add_argument("--limit", type=int, default=10)
    scan_cmd.add_argument("--max-lines", type=int)
    scan_cmd.set_defaults(func=scan_labels)

    select_cmd = sub.add_parser(
        "select-best-anchors",
        help="Keep the top N anchors per technique_id, choosing anchors with the largest anchor_lines groups.",
    )
    select_cmd.add_argument("--anchors", required=True)
    select_cmd.add_argument("--output", required=True)
    select_cmd.add_argument("--top-per-technique", type=int, default=1)
    select_cmd.add_argument("--min-time-gap-seconds", type=int, default=60)
    select_cmd.set_defaults(func=select_best_anchors_command)

    merge_cmd = sub.add_parser("merge-anchors", help="Merge close anchors already written to an anchors JSONL file.")
    merge_cmd.add_argument("--anchors", required=True)
    merge_cmd.add_argument("--raw-anchors")
    merge_cmd.add_argument(
        "--merge-anchor-gap-seconds",
        type=int,
        default=0,
        help="Merge anchors on the same host when their windows overlap plus this gap. Use -1 to disable.",
    )
    merge_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    merge_cmd.set_defaults(func=merge_anchors_command)

    prepare_cmd = sub.add_parser(
        "prepare-dataset",
        help="Materialize the final lab and real segment JSONL files used by LLM tests.",
    )
    prepare_cmd.add_argument("--lab-zip", default="dataset/lab.zip")
    prepare_cmd.add_argument("--lab-member")
    prepare_cmd.add_argument("--real-zip", default="dataset/real.zip")
    prepare_cmd.add_argument("--real-member")
    prepare_cmd.add_argument("--lab-anchors", default="dataset/lab_anchors.jsonl")
    prepare_cmd.add_argument("--best-anchors", default="dataset/anchors.best.jsonl")
    prepare_cmd.add_argument("--output-dir", default="dataset/processed")
    prepare_cmd.add_argument("--before-seconds", type=int, default=60)
    prepare_cmd.add_argument("--after-seconds", type=int, default=60)
    prepare_cmd.add_argument("--same-host", action=argparse.BooleanOptionalAction, default=True)
    prepare_cmd.add_argument("--top-per-technique", type=int, default=1)
    prepare_cmd.add_argument("--min-time-gap-seconds", type=int, default=60)
    prepare_cmd.add_argument(
        "--select-best-lab-anchors",
        action="store_true",
        help="Select best anchors from --lab-anchors instead of using that file directly.",
    )
    prepare_cmd.add_argument("--expected-lab-segments", type=int, default=49)
    prepare_cmd.add_argument("--real-count", type=int, default=200)
    prepare_cmd.add_argument("--events-per-segment", type=int, default=200)
    prepare_cmd.add_argument("--real-prefix-cache", default="dataset/cache/real_prefix.jsonl")
    prepare_cmd.add_argument("--real-prefix-bytes", type=int, default=DEFAULT_REAL_PREFIX_BYTES)
    prepare_cmd.add_argument("--seed", type=int, default=2026)
    prepare_cmd.add_argument("--keep-field", action="append")
    prepare_cmd.add_argument("--anchor-field", action="append")
    prepare_cmd.add_argument(
        "--label-field",
        action="append",
        help="Field hidden from llm_event and preserved under evaluation.hidden_label_fields.",
    )
    prepare_cmd.add_argument("--max-lab-resolve-lines", type=int)
    prepare_cmd.add_argument("--max-lab-lines", type=int)
    prepare_cmd.add_argument("--max-real-extract-lines", type=int)
    prepare_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    prepare_cmd.set_defaults(func=prepare_dataset_command)

    anchors_cmd = sub.add_parser("anchors", help="Collect and merge shared MITRE anchor windows from a ZIP.")
    anchors_cmd.add_argument("--zip", required=True)
    anchors_cmd.add_argument("--member")
    anchors_cmd.add_argument("--anchors", required=True)
    anchors_cmd.add_argument("--checkpoint", required=True)
    anchors_cmd.add_argument("--resume", action="store_true")
    anchors_cmd.add_argument("--before-seconds", type=int, default=60)
    anchors_cmd.add_argument("--after-seconds", type=int, default=60)
    anchors_cmd.add_argument("--anchor-field", action="append")
    anchors_cmd.add_argument("--checkpoint-every", type=int, default=10000)
    anchors_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    anchors_cmd.add_argument(
        "--merge-anchor-gap-seconds",
        type=int,
        default=0,
        help="Merge anchors on the same host when their windows overlap plus this gap. Use -1 to disable.",
    )
    anchors_cmd.add_argument("--max-lines", type=int)
    anchors_cmd.add_argument("--max-segments", type=int)
    anchors_cmd.set_defaults(func=collect_anchors)

    extract_cmd = sub.add_parser("extract", help="Extract MITRE-labeled temporal windows.")
    extract_cmd.add_argument("--zip", required=True)
    extract_cmd.add_argument("--member")
    extract_cmd.add_argument("--output", required=True)
    extract_cmd.add_argument("--checkpoint", required=True)
    extract_cmd.add_argument("--anchors", help="Anchor sidecar JSONL path. Defaults to OUTPUT.anchors.jsonl.")
    extract_cmd.add_argument("--resume", action="store_true")
    extract_cmd.add_argument(
        "--ordered",
        action="store_true",
        help="Use the faster one-pass extractor only when the JSONL is already timestamp ordered.",
    )
    extract_cmd.add_argument("--before-seconds", type=int, default=60)
    extract_cmd.add_argument("--after-seconds", type=int, default=60)
    extract_cmd.add_argument("--same-host", action=argparse.BooleanOptionalAction, default=True)
    extract_cmd.add_argument("--anchor-field", action="append")
    extract_cmd.add_argument("--keep-field", action="append")
    extract_cmd.add_argument(
        "--label-field",
        action="append",
        help="Field hidden from llm_event and preserved under evaluation.hidden_label_fields.",
    )
    extract_cmd.add_argument("--checkpoint-every", type=int, default=10000)
    extract_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    extract_cmd.add_argument(
        "--merge-anchor-gap-seconds",
        type=int,
        default=0,
        help="Merge anchors on the same host when their windows overlap plus this gap. Use -1 to disable.",
    )
    extract_cmd.add_argument("--max-events-per-segment", type=int, default=1000)
    extract_cmd.add_argument("--max-lines", type=int)
    extract_cmd.add_argument("--max-segments", type=int)
    extract_cmd.set_defaults(func=stream_extract)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
