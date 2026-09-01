#!/usr/bin/env python3
"""Run the COMISET two-stage LLM pipeline over extracted JSONL records."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import shutil
import sys
import time
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from comiset_extract import (
    DEFAULT_ANCHOR_FIELDS,
    DEFAULT_KEEP_FIELDS,
    DEFAULT_LABEL_FIELDS,
    build_segment_event_record,
    default_raw_anchors_path,
    event_host,
    event_time,
    extract_anchor,
    find_matching_anchors,
    get_source,
    load_anchor_index,
    merge_anchors_file,
    open_zip_member,
    prepare_raw_anchors_path,
    reduce_event,
    stable_segment_id,
)
from comiset.checkpoint import load_checkpoint, save_checkpoint
from comiset.metrics import (
    atomic_write_json,
    classify_report,
    collect_filter_metrics,
    empty_filter_totals,
    filter_report,
    filter_metrics_summary,
    load_filter_metrics,
    update_filter_metrics,
    write_filter_metrics,
)
from comiset.naming import safe_name
from comiset.llama_cpp_client import LlamaCppGateway, parse_model_ref, sha256_file
from comiset.progress import ProgressBar
from comiset.prompts import CLASSIFY_SYSTEM_PROMPT, FILTER_SYSTEM_PROMPT
from comiset.records import (
    aggregate_chunk_results,
    approx_chunks,
    chunk_to_prompt_payload,
    is_relevant,
    record_to_prompt_payload,
)
from comiset.responses import parse_json_response, validate_json_response


LOGGER = logging.getLogger("comiset.pipeline")
CONTEXT_SAFETY_MARGIN = 256
EXPECTED_SEGMENTS = 249
EXPECTED_EVENTS = 49_800


def filter_prompt(record: dict[str, Any], prompt_format: str) -> str:
    payload = record_to_prompt_payload(record, prompt_format)
    return (
        f"segment_id={record['segment_id']}\n"
        f"anchor_time={record['anchor_time']}\n"
        f"event_line={record['event_line']}\n"
        f"format={prompt_format}\n\n"
        f"{payload}"
    )


def gateway_from_args(args: argparse.Namespace) -> LlamaCppGateway:
    return LlamaCppGateway(
        args.n_ctx,
        args.n_gpu_layers,
        args.n_batch,
        seed=args.seed,
        max_output_tokens=args.max_output_tokens,
        logger=LOGGER,
    )


def run_manifest_path(args: argparse.Namespace) -> Path:
    if getattr(args, "run_manifest", None):
        return Path(args.run_manifest)
    if getattr(args, "run_dir", None):
        return Path(args.run_dir) / "run_manifest.json"
    return Path(args.output).parent / "run_manifest.json"


def phase_manifest_key(args: argparse.Namespace, model: str, phase: str) -> str:
    thinking_mode = getattr(args, "thinking_mode", None)
    suffix = f":thinking={thinking_mode}" if phase == "classify" and thinking_mode else ""
    source = getattr(args, "source", None)
    if phase == "classify" and source:
        suffix += f":source={source}"
    return f"{phase}:{model}{suffix}"


def prepare_model(args: argparse.Namespace, gateway: LlamaCppGateway, model: str, phase: str) -> None:
    model_data = gateway.prepare(model, args.warmup_runs)
    path = run_manifest_path(args)
    manifest = load_checkpoint(path)
    manifest.setdefault("models", {})[model] = model_data
    input_value = getattr(args, "input", None)
    if input_value:
        input_path = Path(input_value)
        if input_path.is_file() and input_path.suffix.lower() != ".zip":
            inputs = manifest.setdefault("inputs", {})
            if str(input_path) not in inputs:
                inputs[str(input_path)] = {
                    "sha256": sha256_file(input_path),
                    "size_bytes": input_path.stat().st_size,
                }
    manifest.setdefault("phases", {})[phase_manifest_key(args, model, phase)] = {
        "model": model,
        "input": getattr(args, "input", None) or getattr(args, "zip", None),
        "output": getattr(args, "output", None),
        "prompt_format": args.prompt_format,
        "thinking_mode": getattr(args, "thinking_mode", None),
        "source": getattr(args, "source", None),
        "filter_model": getattr(args, "filter_model", None),
        "n_ctx": args.n_ctx,
        "max_input_tokens": getattr(args, "max_tokens", None),
        "max_output_tokens": args.max_output_tokens,
        "inference_runs": args.inference_runs,
        "warmup_runs": args.warmup_runs,
        "chunk_aggregation": "majority; ties are Not Interesting; invalid chunks abstain",
        "filter_prompt_sha256": hashlib.sha256(FILTER_SYSTEM_PROMPT.encode()).hexdigest(),
        "classify_prompt_sha256": hashlib.sha256(CLASSIFY_SYSTEM_PROMPT.encode()).hexdigest(),
    }
    save_checkpoint(path, manifest)


def finalize_phase_manifest(args: argparse.Namespace, model: str, phase: str) -> None:
    output = Path(args.output)
    if not output.is_file():
        return
    path = run_manifest_path(args)
    manifest = load_checkpoint(path)
    phase_data = manifest.setdefault("phases", {}).setdefault(phase_manifest_key(args, model, phase), {})
    phase_data["output_sha256"] = sha256_file(output)
    phase_data["output_size_bytes"] = output.stat().st_size
    save_checkpoint(path, manifest)


def chat_repeated(
    args: argparse.Namespace,
    gateway: LlamaCppGateway,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, list[float], list[dict[str, Any]]]:
    if args.inference_runs < 1:
        raise ValueError("--inference-runs must be >= 1")
    responses = []
    timings = []
    metadata = []
    for _ in range(args.inference_runs):
        started = time.perf_counter()
        responses.append(gateway.chat(model, system_prompt, user_prompt, args.timeout_seconds))
        timings.append(time.perf_counter() - started)
        value = getattr(gateway, "last_response_metadata", {})
        metadata.append(dict(value) if isinstance(value, dict) else {})
    return responses[0], timings, metadata


def normalize_response(response: Any, metadata: dict[str, Any], kind: str) -> dict[str, Any]:
    result = dict(response) if isinstance(response, dict) else {"parse_error": True, "raw_response": response}
    finish_reason = metadata.get("finish_reason")
    if finish_reason is not None:
        result["finish_reason"] = finish_reason
    if metadata.get("usage"):
        result["usage"] = metadata["usage"]
    if finish_reason not in (None, "stop"):
        result["error"] = f"generation finished with {finish_reason}"
    return validate_json_response(result, kind)


def filter_records(args: argparse.Namespace) -> None:
    _filter_records(args, gateway_from_args(args))


def record_identity(record: dict[str, Any]) -> tuple[str, str]:
    event_id = record.get("event_id")
    if event_id in (None, ""):
        event_id = record.get("event_line")
    return str(record.get("segment_id")), str(event_id)


def _json_records(handle: Any, path: Path) -> Any:
    for line_number, raw_line in enumerate(handle, 1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSONL record at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise SystemExit(f"JSONL record is not an object at {path}:{line_number}")
        yield value


def validate_filter_output(input_path: Path, output_path: Path, require_complete: bool = False) -> int:
    if not output_path.exists():
        if require_complete:
            raise SystemExit(f"Completed filter output is missing: {output_path}")
        return 0
    seen: set[tuple[str, str]] = set()
    count = 0
    with input_path.open(encoding="utf-8") as expected, output_path.open(encoding="utf-8") as actual:
        expected_records = iter(_json_records(expected, input_path))
        for actual_record in _json_records(actual, output_path):
            try:
                expected_record = next(expected_records)
            except StopIteration as exc:
                raise SystemExit(f"Filter output has extra records: {output_path}") from exc
            actual_key = record_identity(actual_record)
            expected_key = record_identity(expected_record)
            if actual_key != expected_key:
                raise SystemExit(
                    f"Filter output/input sequence mismatch at record {count + 1}: "
                    f"expected {expected_key}, found {actual_key}"
                )
            if actual_key in seen:
                raise SystemExit(f"Duplicate filter output record {actual_key} in {output_path}")
            seen.add(actual_key)
            count += 1
        if require_complete:
            try:
                next(expected_records)
            except StopIteration:
                pass
            else:
                raise SystemExit(f"Filter output is incomplete: {output_path} has {count} records")
    return count


def validate_dataset_input(path: Path) -> dict[str, int]:
    seen: set[tuple[str, str]] = set()
    segment_counts: dict[str, int] = {}
    events = 0
    with path.open(encoding="utf-8") as handle:
        for record in _json_records(handle, path):
            key = record_identity(record)
            if key in seen:
                raise SystemExit(f"Duplicate dataset event {key} in {path}")
            seen.add(key)
            segment_counts[key[0]] = segment_counts.get(key[0], 0) + 1
            events += 1
    if len(segment_counts) != EXPECTED_SEGMENTS or events != EXPECTED_EVENTS:
        raise SystemExit(
            f"Unexpected dataset shape in {path}: segments={len(segment_counts)} events={events}; "
            f"expected segments={EXPECTED_SEGMENTS} events={EXPECTED_EVENTS}"
        )
    if set(segment_counts.values()) != {200}:
        raise SystemExit(f"Dataset segments must contain 200 events each: {path}")
    return {"segments": len(segment_counts), "events": events}


def _filter_records(args: argparse.Namespace, gateway: LlamaCppGateway) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
    metrics_path = Path(args.metrics) if args.metrics else output_path.parent / "metrics.json"
    metrics_by_segment_path = (
        Path(args.metrics_by_segment) if args.metrics_by_segment else output_path.parent / "metrics_by_segment.csv"
    )
    checkpoint = load_checkpoint(checkpoint_path)

    if args.resume and checkpoint.get("phase") == "done":
        validate_filter_output(input_path, output_path, require_complete=True)
        LOGGER.info("Filter checkpoint already done: %s", checkpoint_path)
        print(json.dumps(checkpoint, indent=2))
        return

    prepare_model(args, gateway, args.model, "filter")
    durable_processed = validate_filter_output(input_path, output_path) if args.resume and output_path.exists() else 0
    checkpoint_processed = int(checkpoint.get("processed", 0)) if args.resume else 0
    checkpoint_line = int(checkpoint.get("input_line", 0)) if args.resume else 0
    if args.resume and (checkpoint_processed != durable_processed or checkpoint_line != durable_processed):
        LOGGER.warning(
            "Filter checkpoint/output mismatch; resuming from %d durable records instead of processed=%d input_line=%d.",
            durable_processed,
            checkpoint_processed,
            checkpoint_line,
        )
    resume_line = durable_processed if args.resume else 0
    processed = durable_processed if args.resume else 0
    if args.resume and output_path.exists():
        totals, by_segment = collect_filter_metrics(output_path)
    else:
        totals = empty_filter_totals()
        by_segment: dict[str, dict[str, Any]] = {}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.resume and output_path.exists() else "w"
    total_bytes = input_path.stat().st_size if input_path.exists() else None
    bytes_read = 0
    progress = ProgressBar("filter", total_bytes, enabled=args.progress)

    with input_path.open(encoding="utf-8") as src, output_path.open(mode, encoding="utf-8") as out:
        for input_line, raw_line in enumerate(src, 1):
            bytes_read += len(raw_line.encode("utf-8"))
            progress.update(bytes_read, suffix=f"lines={input_line} processed={processed}")
            if input_line <= resume_line:
                continue
            record = json.loads(raw_line)
            try:
                response_text, timings, metadata = chat_repeated(
                    args,
                    gateway,
                    args.model,
                    FILTER_SYSTEM_PROMPT,
                    filter_prompt(record, args.prompt_format),
                )
                response = normalize_response(parse_json_response(response_text), metadata[0] if metadata else {}, "filter")
            except Exception as exc:
                timings = []
                metadata = []
                response = {"error": str(exc), "confidence": 0}

            record["filter_result"] = {
                "model": args.model,
                "prompt_format": args.prompt_format,
                "elapsed_seconds": (sum(timings) / len(timings)) if timings else None,
                "run_elapsed_seconds": timings,
                "inference_runs": args.inference_runs,
                "response_metadata": metadata,
                **response,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            processed += 1
            update_filter_metrics(totals, by_segment, record)
            write_filter_metrics(metrics_path, metrics_by_segment_path, totals, by_segment)

            save_checkpoint(
                checkpoint_path,
                {
                    "phase": "filter",
                    "input": str(input_path),
                    "output": str(output_path),
                    "input_line": input_line,
                    "processed": processed,
                    "model": args.model,
                    "prompt_format": args.prompt_format,
                    "metrics": str(metrics_path),
                    "metrics_by_segment": str(metrics_by_segment_path),
                    "metric_totals": totals,
                },
            )
            if args.max_lines and processed >= args.max_lines:
                break
    progress.close(suffix=f"processed={processed}")

    save_checkpoint(
        checkpoint_path,
        {
            "phase": "done" if not args.max_lines else "filter",
            "input": str(input_path),
            "output": str(output_path),
            "input_line": input_line if "input_line" in locals() else resume_line,
            "processed": processed,
            "model": args.model,
            "prompt_format": args.prompt_format,
            "metrics": str(metrics_path),
            "metrics_by_segment": str(metrics_by_segment_path),
            "metric_totals": totals,
        },
    )
    finalize_phase_manifest(args, args.model, "filter")
    report_paths = generate_filter_report(output_path, output_path.parent)
    summary = {
        **filter_metrics_summary(totals, len(by_segment)),
        "processed": processed,
        "output": str(output_path),
        "metrics": str(metrics_path),
        "metrics_by_segment": str(metrics_by_segment_path),
        "report": report_paths,
    }
    LOGGER.info("Filter finished: %s", json.dumps(summary, ensure_ascii=False, sort_keys=True))
    print(json.dumps(summary, indent=2))


def apply_filter_to_record(
    record: dict[str, Any],
    model: str,
    prompt_format: str,
    timeout_seconds: int,
    inference_runs: int,
    gateway: LlamaCppGateway,
) -> dict[str, Any]:
    timings = []
    try:
        for _ in range(inference_runs):
            started = time.perf_counter()
            response_text = gateway.chat(model, FILTER_SYSTEM_PROMPT, filter_prompt(record, prompt_format), timeout_seconds)
            timings.append(time.perf_counter() - started)
        metadata = getattr(gateway, "last_response_metadata", {})
        response = normalize_response(parse_json_response(response_text), metadata, "filter")
    except Exception as exc:
        response = {"error": str(exc), "confidence": 0}
    record["filter_result"] = {
        "model": model,
        "prompt_format": prompt_format,
        "elapsed_seconds": (sum(timings) / len(timings)) if timings else None,
        "run_elapsed_seconds": timings,
        "inference_runs": inference_runs,
        **response,
    }
    return record


def collect_anchors_from_zip(args: argparse.Namespace, checkpoint: dict[str, Any]) -> tuple[str, int, int, int]:
    zip_path = Path(args.zip)
    anchors_path = Path(args.anchors)
    checkpoint_path = Path(getattr(args, "anchor_checkpoint", None) or args.checkpoint)
    raw_anchors_path = prepare_raw_anchors_path(anchors_path, checkpoint, args.resume)
    anchor_fields = tuple(args.anchor_field or DEFAULT_ANCHOR_FIELDS)
    before = timedelta(seconds=args.before_seconds)
    after = timedelta(seconds=args.after_seconds)

    phase = checkpoint.get("phase") if args.resume else None
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
                progress.update(uncompressed_bytes, suffix=f"lines={lines_read} found={anchors_found}")
                if lines_read <= resume_lines:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    LOGGER.warning("Skipping invalid JSON at line %s: %s", lines_read, exc)
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
    return member, lines_read, uncompressed_bytes, anchors_found


def extract_filter(args: argparse.Namespace) -> None:
    _extract_filter(args, gateway_from_args(args))


def _extract_filter(args: argparse.Namespace, gateway: LlamaCppGateway) -> None:
    zip_path = Path(args.zip)
    output_path = Path(args.output)
    anchors_path = Path(args.anchors)
    raw_anchors_path = default_raw_anchors_path(anchors_path)
    checkpoint_path = Path(args.checkpoint)
    anchor_checkpoint_path = Path(getattr(args, "anchor_checkpoint", None) or args.checkpoint)
    metrics_path = Path(args.metrics) if args.metrics else output_path.parent / "metrics.json"
    metrics_by_segment_path = (
        Path(args.metrics_by_segment) if args.metrics_by_segment else output_path.parent / "metrics_by_segment.csv"
    )
    merge_anchor_gap_seconds = getattr(args, "merge_anchor_gap_seconds", 0)
    checkpoint = load_checkpoint(checkpoint_path)
    anchor_checkpoint = load_checkpoint(anchor_checkpoint_path)

    if args.resume and checkpoint.get("phase") == "done":
        LOGGER.info("Extract-filter checkpoint already done: %s", checkpoint_path)
        print(json.dumps(checkpoint, indent=2))
        return

    prepare_model(args, gateway, args.model, "extract_filter")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    raw_anchors_path.parent.mkdir(parents=True, exist_ok=True)
    anchor_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    phase = anchor_checkpoint.get("phase") if args.resume else None
    if getattr(args, "require_existing_anchors", False):
        if not anchors_path.exists() or anchors_path.stat().st_size == 0:
            raise SystemExit(
                "Anchors not found. Generate them first with: "
                f"uv run python scripts/comiset_extract.py anchors --zip {zip_path} "
                f"--anchors {anchors_path} --checkpoint {anchor_checkpoint_path}"
            )
        if anchor_checkpoint.get("phase") == "anchors":
            raise SystemExit(
                "Anchor generation is incomplete. Resume it first with: "
                f"uv run python scripts/comiset_extract.py anchors --zip {zip_path} "
                f"--anchors {anchors_path} --checkpoint {anchor_checkpoint_path} --resume"
            )
    if phase not in ("anchors_done", "filter_context"):
        anchors_ready = anchors_path.exists() and anchors_path.stat().st_size > 0
        if anchors_ready and phase != "anchors" and not getattr(args, "rebuild_anchors", False):
            member = anchor_checkpoint.get("member") or (args.member or "")
            lines_read = int(anchor_checkpoint.get("lines_read", 0))
            uncompressed_bytes = int(anchor_checkpoint.get("uncompressed_bytes", 0))
            anchors_found = int(anchor_checkpoint.get("anchors_found", 0))
            original_anchor_count = int(anchor_checkpoint.get("original_anchor_count", 0))
            merged_anchor_count = sum(1 for _ in anchors_path.open(encoding="utf-8"))
        else:
            member, lines_read, uncompressed_bytes, anchors_found = collect_anchors_from_zip(args, anchor_checkpoint)
            original_anchor_count, merged_anchor_count = merge_anchors_file(
                anchors_path,
                merge_anchor_gap_seconds,
                raw_anchors_path,
            )
        save_checkpoint(
            anchor_checkpoint_path,
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
                "merge_anchor_gap_seconds": merge_anchor_gap_seconds,
            },
        )

    anchors = load_anchor_index(anchors_path, args.same_host)
    checkpoint = load_checkpoint(checkpoint_path)
    keep_fields = tuple(args.keep_field or DEFAULT_KEEP_FIELDS)
    label_fields = tuple(args.label_field or DEFAULT_LABEL_FIELDS)
    max_window = timedelta(seconds=args.before_seconds + args.after_seconds)

    resume_lines = (
        int(checkpoint.get("context_lines_read", 0)) if args.resume and checkpoint.get("phase") == "filter_context" else 0
    )
    processed = int(checkpoint.get("processed", 0)) if args.resume and output_path.exists() else 0
    written = int(checkpoint.get("written", 0)) if args.resume and output_path.exists() else 0
    if args.resume:
        totals, by_segment = load_filter_metrics(metrics_path, metrics_by_segment_path)
    else:
        totals = empty_filter_totals()
        by_segment: dict[str, dict[str, Any]] = {}
    output_mode = "a" if args.resume and output_path.exists() else "w"

    zf, member = open_zip_member(zip_path, args.member)
    lines_read = 0
    uncompressed_bytes = 0
    try:
        total_bytes = zf.getinfo(member).file_size
        progress = ProgressBar("filter context", total_bytes, enabled=args.progress)
        with zf.open(member, "r") as src, output_path.open(output_mode, encoding="utf-8") as out:
            for raw_line in src:
                lines_read += 1
                uncompressed_bytes += len(raw_line)
                progress.update(uncompressed_bytes, suffix=f"lines={lines_read} processed={processed} written={written}")
                if lines_read <= resume_lines:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    LOGGER.warning("Skipping invalid JSON at line %s: %s", lines_read, exc)
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
                    record = apply_filter_to_record(
                        record,
                        args.model,
                        args.prompt_format,
                        args.timeout_seconds,
                        args.inference_runs,
                        gateway,
                    )
                    processed += 1
                    update_filter_metrics(totals, by_segment, record)
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    written += 1

                    if processed % args.metrics_every == 0:
                        write_filter_metrics(metrics_path, metrics_by_segment_path, totals, by_segment)

                if lines_read % args.checkpoint_every == 0:
                    write_filter_metrics(metrics_path, metrics_by_segment_path, totals, by_segment)
                    save_checkpoint(
                        checkpoint_path,
                        {
                            "phase": "filter_context",
                            "zip": str(zip_path),
                            "member": member,
                            "anchors": str(anchors_path),
                            "raw_anchors": str(raw_anchors_path),
                            "output": str(output_path),
                            "context_lines_read": lines_read,
                            "context_uncompressed_bytes": uncompressed_bytes,
                            "processed": processed,
                            "written": written,
                            "model": args.model,
                            "prompt_format": args.prompt_format,
                            "keep_dropped": args.keep_dropped,
                            "metrics": str(metrics_path),
                            "metrics_by_segment": str(metrics_by_segment_path),
                            "metric_totals": totals,
                        },
                    )
                if args.max_lines and lines_read >= args.max_lines:
                    break
                if args.max_filter_records and processed >= args.max_filter_records:
                    break
        progress.close(suffix=f"lines={lines_read} processed={processed} written={written}")
    finally:
        zf.close()

    write_filter_metrics(metrics_path, metrics_by_segment_path, totals, by_segment)
    save_checkpoint(
        checkpoint_path,
        {
            "phase": "done" if not args.max_lines and not args.max_filter_records else "filter_context",
            "zip": str(zip_path),
            "member": member,
            "anchors": str(anchors_path),
            "raw_anchors": str(raw_anchors_path),
            "output": str(output_path),
            "context_lines_read": lines_read,
            "context_uncompressed_bytes": uncompressed_bytes,
            "processed": processed,
            "written": written,
            "model": args.model,
            "prompt_format": args.prompt_format,
            "keep_dropped": args.keep_dropped,
            "metrics": str(metrics_path),
            "metrics_by_segment": str(metrics_by_segment_path),
            "metric_totals": totals,
            "original_anchor_count": checkpoint.get("original_anchor_count"),
            "merged_anchor_count": sum(1 for _ in anchors_path.open(encoding="utf-8")),
            "merge_anchor_gap_seconds": merge_anchor_gap_seconds,
        },
    )
    finalize_phase_manifest(args, args.model, "extract_filter")
    report_paths = generate_filter_report(output_path, output_path.parent)
    summary = {
        **filter_metrics_summary(totals, len(by_segment)),
        "processed": processed,
        "written": written,
        "output": str(output_path),
        "anchors": str(anchors_path),
        "metrics": str(metrics_path),
        "metrics_by_segment": str(metrics_by_segment_path),
        "report": report_paths,
    }
    LOGGER.info("Extract-filter finished: %s", json.dumps(summary, ensure_ascii=False, sort_keys=True))
    print(json.dumps(summary, indent=2))


def load_segments(path: Path, include_irrelevant: bool, max_events_per_segment: int) -> dict[str, dict[str, Any]]:
    segments: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as src:
        for raw_line in src:
            record = json.loads(raw_line)
            segment = segments.setdefault(
                record["segment_id"],
                {
                    "segment_id": record["segment_id"],
                    "anchor_line": record["anchor_line"],
                    "anchor_time": record["anchor_time"],
                    "evaluation": record["evaluation"],
                    "records": [],
                    "filter_errors": 0,
                },
            )
            result = record.get("filter_result", {})
            if isinstance(result, dict) and (result.get("error") or result.get("parse_error")):
                segment["filter_errors"] += 1
            if (include_irrelevant or is_relevant(record) is True) and len(segment["records"]) < max_events_per_segment:
                segment["records"].append(record)
    return segments


def classify_prompt(
    segment: dict[str, Any],
    chunk_index: int,
    chunk_count: int,
    records: list[dict[str, Any]],
    prompt_format: str,
    thinking_mode: str = "auto",
) -> str:
    payload = chunk_to_prompt_payload(records, prompt_format)
    thinking_switch = {"auto": "", "think": "/think\n", "no_think": "/no_think\n"}[thinking_mode]
    return (
        thinking_switch
        + f"segment_id={segment['segment_id']}\n"
        f"anchor_time={segment['anchor_time']}\n"
        f"chunk_index={chunk_index}\n"
        f"chunk_count={chunk_count}\n"
        f"format={prompt_format}\n\n"
        f"{payload}"
    )


def classify_segments(args: argparse.Namespace) -> None:
    _classify_segments(args, gateway_from_args(args))


def make_classify_args(
    args: argparse.Namespace,
    input_path: Path,
    output_path: Path,
    checkpoint_path: Path,
    model: str,
    include_irrelevant: bool,
    source: str,
    filter_model: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        input=str(input_path),
        output=str(output_path),
        checkpoint=str(checkpoint_path),
        model=model,
        timeout_seconds=getattr(args, "classify_timeout_seconds", getattr(args, "timeout_seconds", 180)),
        resume=args.resume,
        include_irrelevant=include_irrelevant,
        max_events_per_segment=getattr(args, "max_events_per_segment", 1000),
        max_tokens=args.max_tokens,
        max_segments=getattr(args, "max_segments", getattr(args, "max_classify_segments", None)),
        prompt_format=args.prompt_format,
        n_ctx=getattr(args, "classify_n_ctx", args.n_ctx),
        n_gpu_layers=args.n_gpu_layers,
        n_batch=args.n_batch,
        seed=args.seed,
        max_output_tokens=getattr(args, "classify_max_output_tokens", args.max_output_tokens),
        warmup_runs=args.warmup_runs,
        inference_runs=args.inference_runs,
        run_manifest=getattr(args, "run_manifest", None),
        log_file=getattr(args, "log_file", None),
        progress=args.progress,
        thinking_mode=args.thinking_mode,
        source=source,
        filter_model=filter_model,
    )


def classification_index(path: Path, entries: list[dict[str, Any]]) -> None:
    atomic_write_json(path, {"sources": entries})


def durable_jsonl_records(path: Path, expected_segment_ids: list[str] | None = None) -> int:
    if not path.exists():
        return 0
    count = 0
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid durable JSONL record at {path}:{line_number}") from exc
            if not isinstance(record, dict):
                raise SystemExit(f"Durable JSONL record is not an object at {path}:{line_number}")
            segment_id = str(record.get("segment_id", ""))
            if segment_id in seen:
                raise SystemExit(f"Duplicate durable classification segment {segment_id} in {path}")
            if expected_segment_ids is not None:
                if count >= len(expected_segment_ids) or segment_id != expected_segment_ids[count]:
                    expected = expected_segment_ids[count] if count < len(expected_segment_ids) else "<end>"
                    raise SystemExit(
                        f"Classification output sequence mismatch at {path}:{line_number}: "
                        f"expected {expected}, found {segment_id}"
                    )
            seen.add(segment_id)
            count += 1
    return count


def context_safe_chunks(
    records: list[dict[str, Any]],
    segment: dict[str, Any],
    args: argparse.Namespace,
    gateway: LlamaCppGateway,
    model: str,
) -> list[list[dict[str, Any]]]:
    seed_chunks = approx_chunks(records, args.max_tokens, args.prompt_format)
    token_counter = getattr(gateway, "prompt_token_count", None)
    if not callable(token_counter):
        return seed_chunks
    budget = args.n_ctx - args.max_output_tokens - CONTEXT_SAFETY_MARGIN
    if budget < 1:
        raise ValueError("classification context has no room for input after output reservation")

    safe_chunks: list[list[dict[str, Any]]] = []
    for seed_chunk in seed_chunks:
        current: list[dict[str, Any]] = []
        for record in seed_chunk:
            candidate = [*current, record]
            prompt = classify_prompt(segment, 1, 1, candidate, args.prompt_format, args.thinking_mode)
            prompt_tokens = token_counter(model, CLASSIFY_SYSTEM_PROMPT, prompt)
            if current and prompt_tokens > budget:
                safe_chunks.append(current)
                current = [record]
            else:
                current = candidate
        if current:
            safe_chunks.append(current)
    return safe_chunks


def prompt_tokens_for_chunk(
    gateway: LlamaCppGateway,
    model: str,
    segment: dict[str, Any],
    records: list[dict[str, Any]],
    args: argparse.Namespace,
    chunk_count: int,
    chunk_index: int,
) -> int | None:
    token_counter = getattr(gateway, "prompt_token_count", None)
    if not callable(token_counter):
        return None
    prompt = classify_prompt(segment, chunk_index, chunk_count, records, args.prompt_format, args.thinking_mode)
    return token_counter(model, CLASSIFY_SYSTEM_PROMPT, prompt)


def _classify_segments(args: argparse.Namespace, gateway: LlamaCppGateway) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
    checkpoint = load_checkpoint(checkpoint_path)
    segments = list(load_segments(input_path, args.include_irrelevant, args.max_events_per_segment).values())
    segments.sort(key=lambda item: (item["anchor_time"], item["segment_id"]))
    if args.max_segments:
        segments = segments[: args.max_segments]
    expected_segment_ids = [str(segment["segment_id"]) for segment in segments]
    durable_written = durable_jsonl_records(output_path, expected_segment_ids) if args.resume else 0

    if (
        args.resume
        and checkpoint.get("phase") == "done"
        and durable_written == int(checkpoint.get("written", 0))
        and durable_written == len(segments)
    ):
        LOGGER.info("Classify checkpoint already done: %s", checkpoint_path)
        print(json.dumps(checkpoint, indent=2))
        return
    if args.resume and durable_written != int(checkpoint.get("written", 0)):
        LOGGER.warning(
            "Classification checkpoint/output mismatch; resuming from %d durable records instead of %s.",
            durable_written,
            checkpoint.get("written", 0),
        )

    prepare_model(args, gateway, args.model, "classify")
    start_index = durable_written
    written = durable_written
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.resume and output_path.exists() else "w"
    progress = ProgressBar("classify", len(segments), enabled=args.progress)
    with output_path.open(mode, encoding="utf-8") as out:
        for segment_index, segment in enumerate(segments, 1):
            progress.update(segment_index - 1, suffix=f"written={written}")
            if segment_index <= start_index:
                continue
            chunks = context_safe_chunks(segment["records"], segment, args, gateway, args.model)
            chunk_results = []
            if segment.get("filter_errors"):
                chunk_results.append(
                    {
                        "error": "filter_error_prevented_classification",
                        "filter_error_count": segment["filter_errors"],
                        "event_count": 0,
                    }
                )
            for chunk_index, records in enumerate(chunks, 1):
                progress.update(
                    segment_index - 1,
                    suffix=(
                        f"segment={segment_index}/{len(segments)} "
                        f"chunk={chunk_index}/{len(chunks)} written={written}"
                    ),
                    force=True,
                )
                try:
                    prompt_tokens = None
                    prompt_tokens = prompt_tokens_for_chunk(
                        gateway,
                        args.model,
                        segment,
                        records,
                        args,
                        len(chunks),
                        chunk_index,
                    )
                    context_budget = args.n_ctx - args.max_output_tokens - CONTEXT_SAFETY_MARGIN
                    if prompt_tokens is not None and prompt_tokens > context_budget:
                        raise ValueError(
                            f"prompt tokens {prompt_tokens} exceed safe context budget {context_budget}"
                        )
                    response_text, timings, metadata = chat_repeated(
                        args,
                        gateway,
                        args.model,
                        CLASSIFY_SYSTEM_PROMPT,
                        classify_prompt(
                            segment,
                            chunk_index,
                            len(chunks),
                            records,
                            args.prompt_format,
                            args.thinking_mode,
                        ),
                    )
                    response = normalize_response(
                        parse_json_response(response_text), metadata[0] if metadata else {}, "classify"
                    )
                    if prompt_tokens is not None:
                        response["prompt_tokens"] = prompt_tokens
                except Exception as exc:
                    timings = []
                    metadata = []
                    response = {"error": str(exc), "confidence": 0}
                    if "prompt_tokens" in locals() and prompt_tokens is not None:
                        response["prompt_tokens"] = prompt_tokens
                chunk_results.append(
                    {
                        "chunk_index": chunk_index,
                        "event_count": len(records),
                        "elapsed_seconds": (sum(timings) / len(timings)) if timings else None,
                        "run_elapsed_seconds": timings,
                        "inference_runs": args.inference_runs,
                        "response_metadata": metadata,
                        **response,
                    }
                )

            out.write(
                json.dumps(
                    {
                        "segment_id": segment["segment_id"],
                        "anchor_line": segment["anchor_line"],
                        "anchor_time": segment["anchor_time"],
                        "evaluation": segment["evaluation"],
                        "classification_result": {
                            "model": args.model,
                            "prompt_format": args.prompt_format,
                            "thinking_mode": args.thinking_mode,
                            "source": getattr(args, "source", None),
                            "filter_model": getattr(args, "filter_model", None),
                            "chunks": chunk_results,
                            "aggregate": aggregate_chunk_results(
                                chunk_results,
                                empty_segment=not segment["records"] and not segment.get("filter_errors"),
                            ),
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()
            written += 1
            progress.update(segment_index, suffix=f"written={written}")
            save_checkpoint(
                checkpoint_path,
                {
                    "phase": "classify",
                    "input": str(input_path),
                    "output": str(output_path),
                    "segment_index": segment_index,
                    "written": written,
                    "model": args.model,
                    "prompt_format": args.prompt_format,
                    "thinking_mode": args.thinking_mode,
                    "source": getattr(args, "source", None),
                    "filter_model": getattr(args, "filter_model", None),
                },
            )

    progress.close(suffix=f"written={written}")

    save_checkpoint(
        checkpoint_path,
        {
            "phase": "done",
            "input": str(input_path),
            "output": str(output_path),
            "segment_index": len(segments),
            "written": written,
            "model": args.model,
            "prompt_format": args.prompt_format,
            "thinking_mode": args.thinking_mode,
            "source": getattr(args, "source", None),
            "filter_model": getattr(args, "filter_model", None),
        },
    )
    finalize_phase_manifest(args, args.model, "classify")
    report_paths = generate_classify_report(output_path, output_path.parent)
    summary = {
        "segments": len(segments),
        "written": written,
        "output": str(output_path),
        "source": getattr(args, "source", None),
        "filter_model": getattr(args, "filter_model", None),
        "report": report_paths,
    }
    LOGGER.info("Classify finished: %s", json.dumps(summary, ensure_ascii=False, sort_keys=True))
    print(json.dumps(summary, indent=2))


def generate_filter_report(input_path: Path, output_dir: Path) -> dict[str, str]:
    if not input_path.exists() or input_path.stat().st_size == 0:
        LOGGER.warning("Skipping filter report because input is missing or empty: %s", input_path)
        return {}
    try:
        paths = filter_report(input_path, output_dir)
    except Exception as exc:
        LOGGER.warning("Failed to generate filter report for %s: %s", input_path, exc)
        return {}
    LOGGER.info("Filter report written: %s", paths)
    return paths


def generate_classify_report(input_path: Path, output_dir: Path) -> dict[str, str]:
    if not input_path.exists() or input_path.stat().st_size == 0:
        LOGGER.warning("Skipping classification report because input is missing or empty: %s", input_path)
        return {}
    try:
        paths = classify_report(input_path, output_dir)
    except Exception as exc:
        LOGGER.warning("Failed to generate classification report for %s: %s", input_path, exc)
        return {}
    LOGGER.info("Classification report written: %s", paths)
    return paths


def filter_metrics(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else None
    totals, by_segment = collect_filter_metrics(input_path, args.technique_id)
    rows = sorted(by_segment.values(), key=lambda row: (row["anchor_time"], row["segment_id"]))
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8", newline="") as handle:
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
                "errors",
                "rule_errors",
            ]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    print(json.dumps(filter_metrics_summary(totals, len(rows), output_path), indent=2))


def filter_report_cmd(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_dir = Path(args.output_dir) if args.output_dir else input_path.parent
    paths = generate_filter_report(input_path, output_dir)
    print(json.dumps(paths, indent=2))


def classify_report_cmd(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_dir = Path(args.output_dir) if args.output_dir else input_path.parent
    paths = generate_classify_report(input_path, output_dir)
    print(json.dumps(paths, indent=2))


def dataset_segment_files(input_dir: Path) -> list[Path]:
    files = []
    for subset in ("lab", "real"):
        subset_dir = input_dir / subset
        if not subset_dir.exists():
            continue
        files.extend(
            path
            for path in subset_dir.glob("*.jsonl")
            if not path.name.startswith("anchors.") and path.name != "all_events.jsonl"
        )
    return sorted(files, key=lambda path: (path.parent.name, path.name))


def build_dataset_input(input_dir: Path, output_path: Path, manifest_path: Path, rebuild: bool) -> None:
    files = dataset_segment_files(input_dir)
    if not files:
        raise SystemExit(f"No segment JSONL files found under {input_dir}/lab or {input_dir}/real")

    if output_path.exists() and manifest_path.exists() and not rebuild:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    total_records = 0
    with output_path.open("w", encoding="utf-8") as out, manifest_path.open(
        "w", encoding="utf-8", newline=""
    ) as manifest:
        writer = csv.DictWriter(manifest, fieldnames=["path", "subset", "records"])
        writer.writeheader()
        for path in files:
            records = 0
            with path.open(encoding="utf-8") as src:
                for raw_line in src:
                    if not raw_line.strip():
                        continue
                    out.write(raw_line)
                    records += 1
                    total_records += 1
            writer.writerow({"path": str(path), "subset": path.parent.name, "records": records})
    if total_records == 0:
        raise SystemExit(f"Dataset files under {input_dir} are empty")


def run_pipeline(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    args.run_manifest = args.run_manifest or str(run_dir / "run_manifest.json")
    small_name = safe_name(args.small_model)
    filter_output = run_dir / "filter" / small_name / "filtered_events.jsonl"
    filter_checkpoint = run_dir / "filter" / small_name / "checkpoint.json"

    filter_args = argparse.Namespace(
        input=args.input,
        output=str(filter_output),
        checkpoint=str(filter_checkpoint),
        model=args.small_model,
        timeout_seconds=args.filter_timeout_seconds,
        resume=args.resume,
        max_lines=args.max_filter_lines,
        prompt_format=args.prompt_format,
        n_ctx=args.n_ctx,
        n_gpu_layers=args.n_gpu_layers,
        n_batch=args.n_batch,
        seed=args.seed,
        max_output_tokens=args.max_output_tokens,
        warmup_runs=args.warmup_runs,
        inference_runs=args.inference_runs,
        run_manifest=getattr(args, "run_manifest", None),
        log_file=getattr(args, "log_file", None),
        metrics=str(run_dir / "filter" / small_name / "metrics.json"),
        metrics_by_segment=str(run_dir / "filter" / small_name / "metrics_by_segment.csv"),
        progress=args.progress,
    )
    filter_records(filter_args)
    validate_filter_output(Path(args.input), filter_output, require_complete=not args.max_filter_lines)

    for model in args.big_model:
        big_name = safe_name(f"{model}__thinking-{args.thinking_mode}")
        gateway = LlamaCppGateway(
            args.classify_n_ctx,
            args.n_gpu_layers,
            args.n_batch,
            seed=args.seed,
            max_output_tokens=args.classify_max_output_tokens,
            logger=LOGGER,
        )
        filtered_args = make_classify_args(
            args,
            filter_output,
            run_dir / "classify" / small_name / big_name / "classifications.jsonl",
            run_dir / "classify" / small_name / big_name / "checkpoint.json",
            model,
            args.include_irrelevant,
            source=f"filtered:{small_name}",
            filter_model=args.small_model,
        )
        _classify_segments(filtered_args, gateway)

        raw_args = make_classify_args(
            args,
            Path(args.input),
            run_dir / "classify" / "raw" / big_name / "classifications.jsonl",
            run_dir / "classify" / "raw" / big_name / "checkpoint.json",
            model,
            True,
            source="raw",
        )
        _classify_segments(raw_args, gateway)
        classification_index(
            run_dir / "classify" / big_name / "sources.json",
            [
                {
                    "source": filtered_args.source,
                    "filter_model": filtered_args.filter_model,
                    "input": filtered_args.input,
                    "output": filtered_args.output,
                    "model": model,
                    "thinking_mode": args.thinking_mode,
                },
                {
                    "source": raw_args.source,
                    "filter_model": None,
                    "input": raw_args.input,
                    "output": raw_args.output,
                    "model": model,
                    "thinking_mode": args.thinking_mode,
                },
            ],
        )


def filter_dataset(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    model_name = safe_name(args.model)
    input_path = run_dir / "dataset_input" / "all_events.jsonl"
    build_dataset_input(
        Path(args.input_dir),
        input_path,
        run_dir / "dataset_input" / "manifest.csv",
        args.rebuild_input,
    )
    filter_records(
        argparse.Namespace(
            input=str(input_path),
            output=str(run_dir / "filter" / model_name / "filtered_events.jsonl"),
            checkpoint=str(run_dir / "filter" / model_name / "checkpoint.json"),
            metrics=str(run_dir / "filter" / model_name / "metrics.json"),
            metrics_by_segment=str(run_dir / "filter" / model_name / "metrics_by_segment.csv"),
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            resume=args.resume,
            max_lines=args.max_lines,
            progress=args.progress,
            n_ctx=args.n_ctx,
            n_gpu_layers=args.n_gpu_layers,
            n_batch=args.n_batch,
            seed=args.seed,
            max_output_tokens=args.max_output_tokens,
            warmup_runs=args.warmup_runs,
            inference_runs=args.inference_runs,
            run_manifest=args.run_manifest or str(run_dir / "run_manifest.json"),
            log_file=args.log_file,
            prompt_format=args.prompt_format,
        )
    )


def run_dataset_pipeline(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    input_path = run_dir / "dataset_input" / "all_events.jsonl"
    input_manifest = run_dir / "dataset_input" / "manifest.csv"
    build_dataset_input(Path(args.input_dir), input_path, input_manifest, args.rebuild_input)

    pipeline_args = argparse.Namespace(
        input=str(input_path),
        run_dir=args.run_dir,
        small_model=args.small_model,
        big_model=args.big_model,
        resume=args.resume,
        include_irrelevant=args.include_irrelevant,
        max_filter_lines=args.max_filter_lines,
        max_events_per_segment=args.max_events_per_segment,
        max_tokens=args.max_tokens,
        max_segments=args.max_segments,
        filter_timeout_seconds=args.filter_timeout_seconds,
        classify_timeout_seconds=args.classify_timeout_seconds,
        progress=args.progress,
        n_ctx=args.n_ctx,
        n_gpu_layers=args.n_gpu_layers,
        n_batch=args.n_batch,
        seed=args.seed,
        max_output_tokens=args.max_output_tokens,
        warmup_runs=args.warmup_runs,
        inference_runs=args.inference_runs,
        run_manifest=getattr(args, "run_manifest", None),
        log_file=getattr(args, "log_file", None),
        prompt_format=args.prompt_format,
        classify_n_ctx=args.classify_n_ctx,
        classify_max_output_tokens=args.classify_max_output_tokens,
        thinking_mode=args.thinking_mode,
    )
    run_pipeline(pipeline_args)


def classify_model_pipeline(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    args.run_manifest = args.run_manifest or str(run_dir / "run_manifest.json")
    input_path = run_dir / "dataset_input" / "all_events.jsonl"
    build_dataset_input(
        Path(args.input_dir),
        input_path,
        run_dir / "dataset_input" / "manifest.csv",
        args.rebuild_input,
    )
    validate_dataset_input(input_path)

    filter_outputs = sorted(
        {
            *run_dir.glob("filter/*/filtered_events.jsonl"),
            *run_dir.glob("*/filter/*/filtered_events.jsonl"),
        }
    )
    if not filter_outputs:
        raise SystemExit(f"No completed filter outputs found under {run_dir / 'filter'}")
    for filter_output in filter_outputs:
        validate_filter_output(input_path, filter_output, require_complete=True)

    big_name = safe_name(f"{args.big_model}__thinking-{args.thinking_mode}")
    gateway = gateway_from_args(args)
    sources: list[dict[str, Any]] = []

    raw_args = make_classify_args(
        args,
        input_path,
        run_dir / "classify" / "raw" / big_name / "classifications.jsonl",
        run_dir / "classify" / "raw" / big_name / "checkpoint.json",
        args.big_model,
        True,
        source="raw",
    )
    _classify_segments(raw_args, gateway)
    sources.append(
        {
            "source": raw_args.source,
            "filter_model": None,
            "input": raw_args.input,
            "output": raw_args.output,
            "model": args.big_model,
            "thinking_mode": args.thinking_mode,
        }
    )

    for filter_output in filter_outputs:
        small_name = filter_output.parent.name
        filtered_args = make_classify_args(
            args,
            filter_output,
            run_dir / "classify" / small_name / big_name / "classifications.jsonl",
            run_dir / "classify" / small_name / big_name / "checkpoint.json",
            args.big_model,
            False,
            source=f"filtered:{small_name}",
            filter_model=small_name,
        )
        _classify_segments(filtered_args, gateway)
        sources.append(
            {
                "source": filtered_args.source,
                "filter_model": filtered_args.filter_model,
                "input": filtered_args.input,
                "output": filtered_args.output,
                "model": args.big_model,
                "thinking_mode": args.thinking_mode,
            }
        )

    classification_index(run_dir / "classify" / big_name / "sources.json", sources)


def migrate_legacy_anchor_state(run_dir: Path, small_name: str, anchors: Path, anchor_checkpoint: Path) -> None:
    legacy_dir = run_dir / "filter" / small_name
    legacy_anchors = legacy_dir / "anchors.jsonl"
    legacy_raw_anchors = default_raw_anchors_path(legacy_anchors)
    legacy_checkpoint = legacy_dir / "checkpoint.json"
    raw_anchors = default_raw_anchors_path(anchors)

    anchors.parent.mkdir(parents=True, exist_ok=True)
    if not anchors.exists() and legacy_anchors.exists():
        shutil.copyfile(legacy_anchors, anchors)
    if not raw_anchors.exists() and legacy_raw_anchors.exists():
        shutil.copyfile(legacy_raw_anchors, raw_anchors)
    if not anchor_checkpoint.exists() and legacy_checkpoint.exists():
        legacy_state = load_checkpoint(legacy_checkpoint)
        if legacy_state.get("phase") in {"anchors", "anchors_done"}:
            migrated_state = dict(legacy_state)
            migrated_state["anchors"] = str(anchors)
            migrated_state["raw_anchors"] = str(raw_anchors)
            save_checkpoint(anchor_checkpoint, migrated_state)


def run_direct_pipeline(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    args.run_manifest = args.run_manifest or str(run_dir / "run_manifest.json")
    small_name = safe_name(args.small_model)
    filter_output = run_dir / "filter" / small_name / "filtered_events.jsonl"
    filter_checkpoint = run_dir / "filter" / small_name / "checkpoint.json"
    anchors = run_dir / "anchors" / "anchors.jsonl"
    anchor_checkpoint = run_dir / "anchors" / "checkpoint.json"

    migrate_legacy_anchor_state(run_dir, small_name, anchors, anchor_checkpoint)

    extract_filter_args = argparse.Namespace(
        zip=args.zip,
        member=args.member,
        output=str(filter_output),
        anchors=str(anchors),
        checkpoint=str(filter_checkpoint),
        anchor_checkpoint=str(anchor_checkpoint),
        model=args.small_model,
        timeout_seconds=args.filter_timeout_seconds,
        resume=args.resume,
        prompt_format=args.prompt_format,
        n_ctx=args.n_ctx,
        n_gpu_layers=args.n_gpu_layers,
        n_batch=args.n_batch,
        seed=args.seed,
        max_output_tokens=args.max_output_tokens,
        warmup_runs=args.warmup_runs,
        inference_runs=args.inference_runs,
        run_manifest=getattr(args, "run_manifest", None),
        log_file=getattr(args, "log_file", None),
        metrics=str(run_dir / "filter" / small_name / "metrics.json"),
        metrics_by_segment=str(run_dir / "filter" / small_name / "metrics_by_segment.csv"),
        before_seconds=args.before_seconds,
        after_seconds=args.after_seconds,
        same_host=args.same_host,
        anchor_field=None,
        keep_field=args.keep_field,
        label_field=args.label_field,
        checkpoint_every=args.checkpoint_every,
        metrics_every=args.metrics_every,
        max_lines=args.max_lines,
        max_segments=None,
        max_filter_records=args.max_filter_records,
        keep_dropped=args.keep_dropped,
        merge_anchor_gap_seconds=0,
        rebuild_anchors=False,
        require_existing_anchors=True,
        progress=args.progress,
    )
    extract_filter(extract_filter_args)

    for model in args.big_model:
        big_name = safe_name(f"{model}__thinking-{args.thinking_mode}")
        classify_output = run_dir / "classify" / small_name / big_name / "classifications.jsonl"
        classify_checkpoint = run_dir / "classify" / small_name / big_name / "checkpoint.json"
        classify_args = argparse.Namespace(
            input=str(filter_output),
            output=str(classify_output),
            checkpoint=str(classify_checkpoint),
            model=model,
            timeout_seconds=args.classify_timeout_seconds,
            resume=args.resume,
            include_irrelevant=args.include_irrelevant,
            max_events_per_segment=args.max_events_per_segment,
            max_tokens=args.max_tokens,
            max_segments=args.max_classify_segments,
            prompt_format=args.prompt_format,
            n_ctx=args.classify_n_ctx,
            n_gpu_layers=args.n_gpu_layers,
            n_batch=args.n_batch,
            seed=args.seed,
            max_output_tokens=args.classify_max_output_tokens,
            warmup_runs=args.warmup_runs,
            inference_runs=args.inference_runs,
            run_manifest=getattr(args, "run_manifest", None),
            log_file=getattr(args, "log_file", None),
            progress=args.progress,
            thinking_mode=args.thinking_mode,
        )
        classify_segments(classify_args)


def add_common_llama_cpp_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--n-ctx", type=int, default=4096)
    parser.add_argument("--n-gpu-layers", type=int, default=-1, help="Number of layers offloaded to GPU; -1 means all.")
    parser.add_argument("--n-batch", type=int, default=512)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--inference-runs", type=int, default=1)
    parser.add_argument("--run-manifest")
    parser.add_argument("--log-file")
    parser.add_argument("--prompt-format", choices=("csv", "json"), default="csv")


def add_thinking_mode_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--thinking-mode",
        choices=("auto", "think", "no_think"),
        default="auto",
        help="Classification prompt mode; Qwen3 supports the switches, DeepSeek treats them as experimental directives.",
    )


def add_combined_classification_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--classify-n-ctx", type=int, default=8192, help="Context window for large classifiers.")
    parser.add_argument(
        "--classify-max-output-tokens",
        type=int,
        default=256,
        help="Output-token limit for large classifiers.",
    )
    add_thinking_mode_arg(parser)


def default_log_file(args: argparse.Namespace) -> Path | None:
    command = getattr(args, "command", "")
    if getattr(args, "log_file", None):
        return Path(args.log_file)
    if command in {"run", "filter-dataset", "run-dataset", "run-direct"}:
        return Path(args.run_dir) / "pipeline.log"
    output = getattr(args, "output", None)
    if output:
        return Path(output).parent / f"{command or 'pipeline'}.log"
    output_dir = getattr(args, "output_dir", None)
    if output_dir:
        return Path(output_dir) / f"{command or 'pipeline'}.log"
    input_path = getattr(args, "input", None)
    if command in {"filter-report", "classify-report"} and input_path:
        return Path(input_path).parent / f"{command}.log"
    return None


def setup_logging(args: argparse.Namespace) -> None:
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    LOGGER.addHandler(console)

    log_path = default_log_file(args)
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(file_handler)
    LOGGER.info("Logging to %s", log_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    filter_cmd = sub.add_parser("filter", help="Run the lightweight LLM filtering stage.")
    filter_cmd.add_argument("--input", required=True)
    filter_cmd.add_argument("--output", required=True)
    filter_cmd.add_argument("--checkpoint", required=True)
    filter_cmd.add_argument("--model", required=True)
    filter_cmd.add_argument("--timeout-seconds", type=int, default=120)
    filter_cmd.add_argument("--resume", action="store_true")
    filter_cmd.add_argument("--max-lines", type=int)
    filter_cmd.add_argument("--metrics")
    filter_cmd.add_argument("--metrics-by-segment")
    filter_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_llama_cpp_args(filter_cmd)
    filter_cmd.set_defaults(func=filter_records)

    extract_filter_cmd = sub.add_parser(
        "extract-filter",
        help="Stream from ZIP and run the lightweight filter without writing segment_events.jsonl.",
    )
    extract_filter_cmd.add_argument("--zip", required=True)
    extract_filter_cmd.add_argument("--member")
    extract_filter_cmd.add_argument("--output", required=True)
    extract_filter_cmd.add_argument("--anchors", required=True)
    extract_filter_cmd.add_argument("--checkpoint", required=True)
    extract_filter_cmd.add_argument("--anchor-checkpoint")
    extract_filter_cmd.add_argument("--model", required=True)
    extract_filter_cmd.add_argument("--timeout-seconds", type=int, default=120)
    extract_filter_cmd.add_argument("--resume", action="store_true")
    extract_filter_cmd.add_argument("--before-seconds", type=int, default=60)
    extract_filter_cmd.add_argument("--after-seconds", type=int, default=60)
    extract_filter_cmd.add_argument("--same-host", action=argparse.BooleanOptionalAction, default=True)
    extract_filter_cmd.add_argument("--keep-field", action="append")
    extract_filter_cmd.add_argument("--label-field", action="append")
    extract_filter_cmd.add_argument("--checkpoint-every", type=int, default=10000)
    extract_filter_cmd.add_argument("--metrics-every", type=int, default=100)
    extract_filter_cmd.add_argument("--max-lines", type=int)
    extract_filter_cmd.add_argument("--max-filter-records", type=int)
    extract_filter_cmd.add_argument(
        "--keep-dropped", action="store_true", help="Deprecated: all records are always preserved."
    )
    extract_filter_cmd.add_argument("--metrics")
    extract_filter_cmd.add_argument("--metrics-by-segment")
    extract_filter_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_llama_cpp_args(extract_filter_cmd)
    extract_filter_cmd.set_defaults(func=extract_filter, require_existing_anchors=True, rebuild_anchors=False)

    classify_cmd = sub.add_parser("classify", help="Run the robust LLM classification stage.")
    classify_cmd.add_argument("--input", required=True)
    classify_cmd.add_argument("--output", required=True)
    classify_cmd.add_argument("--checkpoint", required=True)
    classify_cmd.add_argument("--model", required=True)
    classify_cmd.add_argument("--timeout-seconds", type=int, default=180)
    classify_cmd.add_argument("--resume", action="store_true")
    classify_cmd.add_argument("--include-irrelevant", action="store_true")
    classify_cmd.add_argument("--max-events-per-segment", type=int, default=1000)
    classify_cmd.add_argument("--max-tokens", type=int, default=5000)
    classify_cmd.add_argument("--max-segments", type=int)
    classify_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_llama_cpp_args(classify_cmd)
    add_thinking_mode_arg(classify_cmd)
    classify_cmd.set_defaults(func=classify_segments, n_ctx=8192, max_output_tokens=256)

    run_cmd = sub.add_parser("run", help="Run one small filtering model and N large classifiers.")
    run_cmd.add_argument("--input", required=True)
    run_cmd.add_argument("--run-dir", required=True)
    run_cmd.add_argument("--small-model", required=True)
    run_cmd.add_argument("--big-model", action="append", required=True)
    run_cmd.add_argument("--resume", action="store_true")
    run_cmd.add_argument("--include-irrelevant", action="store_true")
    run_cmd.add_argument("--max-filter-lines", type=int)
    run_cmd.add_argument("--max-events-per-segment", type=int, default=1000)
    run_cmd.add_argument("--max-tokens", type=int, default=5000)
    run_cmd.add_argument("--max-segments", type=int)
    run_cmd.add_argument("--filter-timeout-seconds", type=int, default=120)
    run_cmd.add_argument("--classify-timeout-seconds", type=int, default=180)
    run_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_llama_cpp_args(run_cmd)
    add_combined_classification_args(run_cmd)
    run_cmd.set_defaults(func=run_pipeline)

    filter_dataset_cmd = sub.add_parser(
        "filter-dataset",
        help="Build the materialized dataset input and run only the lightweight filtering stage.",
    )
    filter_dataset_cmd.add_argument("--input-dir", default="dataset/processed")
    filter_dataset_cmd.add_argument("--run-dir", required=True)
    filter_dataset_cmd.add_argument("--model", required=True)
    filter_dataset_cmd.add_argument("--resume", action="store_true")
    filter_dataset_cmd.add_argument("--rebuild-input", action="store_true")
    filter_dataset_cmd.add_argument("--max-lines", type=int)
    filter_dataset_cmd.add_argument("--timeout-seconds", type=int, default=120)
    filter_dataset_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_llama_cpp_args(filter_dataset_cmd)
    filter_dataset_cmd.set_defaults(func=filter_dataset)

    run_dataset_cmd = sub.add_parser(
        "run-dataset",
        help="Run one small filtering model and N large classifiers over materialized segment files.",
    )
    run_dataset_cmd.add_argument("--input-dir", default="dataset/processed")
    run_dataset_cmd.add_argument("--run-dir", required=True)
    run_dataset_cmd.add_argument("--small-model", required=True)
    run_dataset_cmd.add_argument("--big-model", action="append", required=True)
    run_dataset_cmd.add_argument("--resume", action="store_true")
    run_dataset_cmd.add_argument("--include-irrelevant", action="store_true")
    run_dataset_cmd.add_argument("--rebuild-input", action="store_true")
    run_dataset_cmd.add_argument("--max-filter-lines", type=int)
    run_dataset_cmd.add_argument("--max-events-per-segment", type=int, default=1000)
    run_dataset_cmd.add_argument("--max-tokens", type=int, default=5000)
    run_dataset_cmd.add_argument("--max-segments", type=int)
    run_dataset_cmd.add_argument("--filter-timeout-seconds", type=int, default=120)
    run_dataset_cmd.add_argument("--classify-timeout-seconds", type=int, default=180)
    run_dataset_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_llama_cpp_args(run_dataset_cmd)
    add_combined_classification_args(run_dataset_cmd)
    run_dataset_cmd.set_defaults(func=run_dataset_pipeline)

    classify_model_cmd = sub.add_parser(
        "classify-model",
        help="Classify raw data and every completed filter output with one large model.",
    )
    classify_model_cmd.add_argument("--input-dir", default="dataset/processed")
    classify_model_cmd.add_argument("--run-dir", required=True)
    classify_model_cmd.add_argument("--big-model", required=True)
    classify_model_cmd.add_argument("--resume", action="store_true")
    classify_model_cmd.add_argument("--rebuild-input", action="store_true")
    classify_model_cmd.add_argument("--max-events-per-segment", type=int, default=1000)
    classify_model_cmd.add_argument("--max-tokens", type=int, default=5000)
    classify_model_cmd.add_argument("--max-segments", type=int)
    classify_model_cmd.add_argument("--timeout-seconds", type=int, default=180)
    classify_model_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_llama_cpp_args(classify_model_cmd)
    add_thinking_mode_arg(classify_model_cmd)
    classify_model_cmd.set_defaults(func=classify_model_pipeline, n_ctx=8192, max_output_tokens=256)

    run_direct_cmd = sub.add_parser(
        "run-direct",
        help="Run ZIP -> small filter -> N large classifiers without segment_events.jsonl.",
    )
    run_direct_cmd.add_argument("--zip", required=True)
    run_direct_cmd.add_argument("--member")
    run_direct_cmd.add_argument("--run-dir", required=True)
    run_direct_cmd.add_argument("--small-model", required=True)
    run_direct_cmd.add_argument("--big-model", action="append", required=True)
    run_direct_cmd.add_argument("--resume", action="store_true")
    run_direct_cmd.add_argument("--include-irrelevant", action="store_true")
    run_direct_cmd.add_argument(
        "--keep-dropped", action="store_true", help="Deprecated: all records are always preserved."
    )
    run_direct_cmd.add_argument("--before-seconds", type=int, default=60)
    run_direct_cmd.add_argument("--after-seconds", type=int, default=60)
    run_direct_cmd.add_argument("--same-host", action=argparse.BooleanOptionalAction, default=True)
    run_direct_cmd.add_argument("--keep-field", action="append")
    run_direct_cmd.add_argument("--label-field", action="append")
    run_direct_cmd.add_argument("--checkpoint-every", type=int, default=10000)
    run_direct_cmd.add_argument("--metrics-every", type=int, default=100)
    run_direct_cmd.add_argument("--max-lines", type=int)
    run_direct_cmd.add_argument("--max-filter-records", type=int)
    run_direct_cmd.add_argument("--max-events-per-segment", type=int, default=1000)
    run_direct_cmd.add_argument("--max-tokens", type=int, default=5000)
    run_direct_cmd.add_argument("--max-classify-segments", type=int)
    run_direct_cmd.add_argument("--filter-timeout-seconds", type=int, default=120)
    run_direct_cmd.add_argument("--classify-timeout-seconds", type=int, default=180)
    run_direct_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_llama_cpp_args(run_direct_cmd)
    add_combined_classification_args(run_direct_cmd)
    run_direct_cmd.set_defaults(func=run_direct_pipeline)

    metrics_cmd = sub.add_parser("filter-metrics", help="Measure whether filtering kept rule-labelled events.")
    metrics_cmd.add_argument("--input", required=True)
    metrics_cmd.add_argument("--output")
    metrics_cmd.add_argument("--technique-id")
    metrics_cmd.set_defaults(func=filter_metrics)

    filter_report_parser = sub.add_parser("filter-report", help="Generate detailed filter metrics and error reports.")
    filter_report_parser.add_argument("--input", required=True)
    filter_report_parser.add_argument("--output-dir")
    filter_report_parser.add_argument("--log-file")
    filter_report_parser.set_defaults(func=filter_report_cmd)

    classify_report_parser = sub.add_parser(
        "classify-report", help="Generate detailed classification metrics and error reports."
    )
    classify_report_parser.add_argument("--input", required=True)
    classify_report_parser.add_argument("--output-dir")
    classify_report_parser.add_argument("--log-file")
    classify_report_parser.set_defaults(func=classify_report_cmd)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if (
        getattr(args, "warmup_runs", 0) < 0
        or getattr(args, "inference_runs", 1) < 1
        or getattr(args, "max_output_tokens", 1) < 1
        or getattr(args, "classify_max_output_tokens", 1) < 1
        or getattr(args, "max_tokens", 1) < 1
        or getattr(args, "n_ctx", 1) < 1
        or getattr(args, "classify_n_ctx", 1) < 1
    ):
        parser.error("Warm-up must be >= 0; runs, context and token limits must be >= 1")
    try:
        for name in ("model", "small_model"):
            if model := getattr(args, name, None):
                parse_model_ref(model)
        for model in getattr(args, "big_model", []) or []:
            parse_model_ref(model)
    except ValueError as exc:
        parser.error(str(exc))
    setup_logging(args)
    args.func(args)


if __name__ == "__main__":
    main()
