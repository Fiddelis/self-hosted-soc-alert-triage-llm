from __future__ import annotations

import re
from typing import Any


MITRE_LABEL_PATTERN = re.compile(r"(?i:technique_(?:id|name)\s*=)|\bT\d{4}(?:\.\d{3})?\b")


def reveals_mitre_label(value: Any) -> bool:
    return isinstance(value, str) and bool(MITRE_LABEL_PATTERN.search(value))


def sanitize_label_value(value: Any) -> Any:
    if not isinstance(value, str) or not reveals_mitre_label(value):
        return value
    clean_lines = [line for line in value.splitlines() if not reveals_mitre_label(line)]
    return "\n".join(clean_lines).strip()


def split_event_labels(
    event: dict[str, Any],
    label_fields: tuple[str, ...],
) -> tuple[dict[str, Any], dict[str, Any]]:
    label_names = {field.lower() for field in label_fields}
    llm_event: dict[str, Any] = {}
    hidden: dict[str, Any] = {}
    for key, value in event.items():
        if key.lower() in label_names or reveals_mitre_label(value):
            hidden[key] = value
            sanitized = sanitize_label_value(value)
            if sanitized not in (None, "", [], {}):
                llm_event[key] = sanitized
        else:
            llm_event[key] = value
    return llm_event, hidden


def sanitize_llm_event(event: dict[str, Any]) -> dict[str, Any]:
    sanitized, _ = split_event_labels(
        event,
        (
            "rule_technique_id",
            "Rule_technique_id",
            "RuleTechniqueId",
            "rule_technique_name",
            "Rule_technique_name",
            "RuleTechniqueName",
            "rule_technique",
        ),
    )
    return sanitized
