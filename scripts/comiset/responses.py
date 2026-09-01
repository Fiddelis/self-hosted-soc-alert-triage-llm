from __future__ import annotations

import json
import re
from typing import Any


def parse_json_response(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {"parse_error": True, "raw_response": text}


def validate_json_response(response: Any, kind: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {"parse_error": True, "raw_response": response}
    if response.get("error") or response.get("parse_error"):
        return response

    required = {
        "filter": {"relevant", "confidence", "reason"},
        "classify": {"classification", "confidence", "reason"},
    }[kind]
    missing = sorted(field for field in required if field not in response)
    if missing:
        return {**response, "parse_error": True, "validation_error": f"missing fields: {', '.join(missing)}"}
    if not isinstance(response["confidence"], int | float) or isinstance(response["confidence"], bool):
        return {**response, "parse_error": True, "validation_error": "confidence must be numeric"}
    if not isinstance(response["reason"], str):
        return {**response, "parse_error": True, "validation_error": "reason must be a string"}
    if kind == "filter" and not isinstance(response["relevant"], bool):
        return {**response, "parse_error": True, "validation_error": "relevant must be boolean"}
    if kind == "classify" and response["classification"] not in {"Interesting", "Not Interesting"}:
        return {**response, "parse_error": True, "validation_error": "invalid classification"}
    return response
