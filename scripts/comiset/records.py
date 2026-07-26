from __future__ import annotations

import csv
import io
import json
from typing import Any

from comiset.privacy import sanitize_llm_event


CSV_FIELDS = (
    "event_line",
    "timestamp",
    "host",
    "user",
    "process_name",
    "process_path",
    "process_id",
    "process_guid",
    "parent_process_name",
    "parent_process_id",
    "event_id",
    "log_name",
    "source_name",
    "rule_name",
    "command_line",
    "parent_command_line",
    "message",
)


def is_relevant(record: dict[str, Any]) -> bool:
    result = record.get("filter_result", {})
    value = result.get("relevant")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "relevant", "interesting"}
    return False


def llm_event(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("llm_event")
    return sanitize_llm_event(value) if isinstance(value, dict) else {}


def event_value(event: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = event.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""


def event_to_csv_row(record: dict[str, Any]) -> dict[str, Any]:
    event = llm_event(record)
    return {
        "event_line": record.get("event_line", ""),
        "timestamp": event_value(event, "@timestamp", "event_original_time", "event_recorded_time"),
        "host": event_value(event, "host_name"),
        "user": event_value(event, "user_name", "User"),
        "process_name": event_value(event, "process_name"),
        "process_path": event_value(event, "process_path"),
        "process_id": event_value(event, "process_id"),
        "process_guid": event_value(event, "process_guid"),
        "parent_process_name": event_value(event, "process_parent_name"),
        "parent_process_id": event_value(event, "process_parent_id"),
        "event_id": event_value(event, "event_id"),
        "log_name": event_value(event, "log_name"),
        "source_name": event_value(event, "source_name"),
        "rule_name": event_value(event, "RuleName"),
        "command_line": event_value(event, "CommandLine"),
        "parent_command_line": event_value(event, "ParentCommandLine"),
        "message": event_value(event, "event_original_message"),
    }


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def record_to_prompt_payload(record: dict[str, Any], prompt_format: str) -> str:
    if prompt_format == "csv":
        return rows_to_csv([event_to_csv_row(record)])
    return json.dumps(
        {
            "segment_id": record["segment_id"],
            "anchor_time": record["anchor_time"],
            "event_line": record["event_line"],
            "event": llm_event(record),
        },
        ensure_ascii=False,
    )


def approx_chunks(records: list[dict[str, Any]], max_tokens: int, prompt_format: str) -> list[list[dict[str, Any]]]:
    max_chars = max_tokens * 4
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for record in records:
        if prompt_format == "csv":
            item_size = len(rows_to_csv([event_to_csv_row(record)]))
        else:
            item_size = len(json.dumps(llm_event(record), ensure_ascii=False))
        if current and current_size + item_size > max_chars:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(record)
        current_size += item_size
    if current:
        chunks.append(current)
    return chunks


def chunk_to_prompt_payload(records: list[dict[str, Any]], prompt_format: str) -> str:
    if prompt_format == "csv":
        return rows_to_csv([event_to_csv_row(record) for record in records])
    return json.dumps([llm_event(record) for record in records], ensure_ascii=False)


def aggregate_chunk_results(chunks: list[dict[str, Any]], empty_segment: bool = False) -> dict[str, Any]:
    if empty_segment:
        return {
            "strategy": "majority",
            "status": "empty_after_filter",
            "classification": "Not Interesting",
            "interesting_votes": 0,
            "not_interesting_votes": 0,
            "invalid_votes": 0,
            "tie_breaker": "Not Interesting",
        }

    interesting = 0
    not_interesting = 0
    invalid = 0
    for chunk in chunks:
        if chunk.get("error") or chunk.get("parse_error"):
            invalid += 1
            continue
        value = str(chunk.get("classification", "")).strip().lower()
        if value == "interesting":
            interesting += 1
        elif value == "not interesting":
            not_interesting += 1
        else:
            invalid += 1

    valid = interesting + not_interesting
    if not valid:
        classification = None
        status = "error"
    else:
        classification = "Interesting" if interesting > not_interesting else "Not Interesting"
        status = "ok" if interesting != not_interesting else "tie"
    return {
        "strategy": "majority",
        "status": status,
        "classification": classification,
        "interesting_votes": interesting,
        "not_interesting_votes": not_interesting,
        "invalid_votes": invalid,
        "tie_breaker": "Not Interesting",
    }


def event_has_hidden_rule(record: dict[str, Any]) -> bool:
    evaluation = record.get("evaluation", {})
    if not isinstance(evaluation, dict):
        return False
    if evaluation.get("event_has_rule_technique"):
        return True
    hidden = evaluation.get("hidden_label_fields")
    return bool(hidden)


def segment_technique_ids(record: dict[str, Any]) -> list[str]:
    evaluation = record.get("evaluation", {})
    if not isinstance(evaluation, dict):
        return []
    label = evaluation.get("segment_label", {})
    if not isinstance(label, dict):
        return []
    values = label.get("technique_ids") or []
    if isinstance(values, str):
        return [values]
    return [str(value) for value in values]

