#!/usr/bin/env python3
"""Run the COMISET two-stage LLM pipeline over extracted JSONL records."""

from __future__ import annotations

import argparse
import csv
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
from comiset.ollama_client import OllamaGateway
from comiset.progress import ProgressBar
from comiset.prompts import CLASSIFY_SYSTEM_PROMPT, FILTER_SYSTEM_PROMPT
from comiset.records import (
    approx_chunks,
    chunk_to_prompt_payload,
    is_relevant,
    record_to_prompt_payload,
)
from comiset.responses import parse_json_response


LOGGER = logging.getLogger("comiset.pipeline")


def filter_prompt(record: dict[str, Any], prompt_format: str) -> str:
    payload = record_to_prompt_payload(record, prompt_format)
    return (
        f"segment_id={record['segment_id']}\n"
        f"anchor_time={record['anchor_time']}\n"
        f"event_line={record['event_line']}\n"
        f"format={prompt_format}\n\n"
        f"{payload}"
    )


def filter_records(args: argparse.Namespace) -> None:
    gateway = OllamaGateway(
        args.ollama_url,
        pull_missing=args.pull_missing,
        timeout_seconds=args.timeout_seconds,
        logger=LOGGER,
    )
    try:
        _filter_records(args, gateway)
    finally:
        cleanup_model_after_use(args, gateway, args.model)


def cleanup_model_after_use(args: argparse.Namespace, gateway: OllamaGateway, model: str) -> None:
    if not getattr(args, "delete_model_after_use", False):
        return
    try:
        deleted = gateway.delete_ready_model(model)
    except Exception as exc:
        LOGGER.warning("Failed to delete Ollama model %r: %s", model, exc)
        return
    if deleted:
        LOGGER.info("Deleted Ollama model %r after use.", model)


def _filter_records(args: argparse.Namespace, gateway: OllamaGateway) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
    metrics_path = Path(args.metrics) if args.metrics else output_path.parent / "metrics.json"
    metrics_by_segment_path = (
        Path(args.metrics_by_segment) if args.metrics_by_segment else output_path.parent / "metrics_by_segment.csv"
    )
    checkpoint = load_checkpoint(checkpoint_path)

    if args.resume and checkpoint.get("phase") == "done":
        LOGGER.info("Filter checkpoint already done: %s", checkpoint_path)
        print(json.dumps(checkpoint, indent=2))
        return

    resume_line = int(checkpoint.get("input_line", 0)) if args.resume else 0
    processed = int(checkpoint.get("processed", 0)) if args.resume and output_path.exists() else 0
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
            started = time.perf_counter()
            try:
                response_text = gateway.chat(
                    args.model,
                    FILTER_SYSTEM_PROMPT,
                    filter_prompt(record, args.prompt_format),
                    args.timeout_seconds,
                )
                response = parse_json_response(response_text)
            except Exception as exc:
                response = {"error": str(exc), "relevant": False, "confidence": 0}
            elapsed = time.perf_counter() - started

            record["filter_result"] = {
                "model": args.model,
                "prompt_format": args.prompt_format,
                "elapsed_seconds": elapsed,
                **response,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
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
    gateway: OllamaGateway,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response_text = gateway.chat(
            model,
            FILTER_SYSTEM_PROMPT,
            filter_prompt(record, prompt_format),
            timeout_seconds,
        )
        response = parse_json_response(response_text)
    except Exception as exc:
        response = {"error": str(exc), "relevant": False, "confidence": 0}
    elapsed = time.perf_counter() - started
    record["filter_result"] = {
        "model": model,
        "prompt_format": prompt_format,
        "elapsed_seconds": elapsed,
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
    gateway = OllamaGateway(
        args.ollama_url,
        pull_missing=args.pull_missing,
        timeout_seconds=args.timeout_seconds,
        logger=LOGGER,
    )
    try:
        _extract_filter(args, gateway)
    finally:
        cleanup_model_after_use(args, gateway, args.model)


def _extract_filter(args: argparse.Namespace, gateway: OllamaGateway) -> None:
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
                        gateway,
                    )
                    processed += 1
                    update_filter_metrics(totals, by_segment, record)
                    if is_relevant(record) or args.keep_dropped:
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
    report_paths: dict[str, str] = {}
    if args.keep_dropped:
        report_paths = generate_filter_report(output_path, output_path.parent)
    else:
        LOGGER.warning("Skipping detailed filter report because --keep-dropped was not used.")
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
            if not include_irrelevant and not is_relevant(record):
                continue
            segment = segments.setdefault(
                record["segment_id"],
                {
                    "segment_id": record["segment_id"],
                    "anchor_line": record["anchor_line"],
                    "anchor_time": record["anchor_time"],
                    "evaluation": record["evaluation"],
                    "records": [],
                },
            )
            if len(segment["records"]) < max_events_per_segment:
                segment["records"].append(record)
    return segments


def classify_prompt(
    segment: dict[str, Any],
    chunk_index: int,
    chunk_count: int,
    records: list[dict[str, Any]],
    prompt_format: str,
) -> str:
    payload = chunk_to_prompt_payload(records, prompt_format)
    return (
        f"segment_id={segment['segment_id']}\n"
        f"anchor_time={segment['anchor_time']}\n"
        f"chunk_index={chunk_index}\n"
        f"chunk_count={chunk_count}\n"
        f"format={prompt_format}\n\n"
        f"{payload}"
    )


def classify_segments(args: argparse.Namespace) -> None:
    gateway = OllamaGateway(
        args.ollama_url,
        pull_missing=args.pull_missing,
        timeout_seconds=args.timeout_seconds,
        logger=LOGGER,
    )
    try:
        _classify_segments(args, gateway)
    finally:
        cleanup_model_after_use(args, gateway, args.model)


def _classify_segments(args: argparse.Namespace, gateway: OllamaGateway) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
    checkpoint = load_checkpoint(checkpoint_path)

    if args.resume and checkpoint.get("phase") == "done":
        LOGGER.info("Classify checkpoint already done: %s", checkpoint_path)
        print(json.dumps(checkpoint, indent=2))
        return

    start_index = int(checkpoint.get("segment_index", 0)) if args.resume else 0
    written = int(checkpoint.get("written", 0)) if args.resume and output_path.exists() else 0
    segments = list(load_segments(input_path, args.include_irrelevant, args.max_events_per_segment).values())
    segments.sort(key=lambda item: (item["anchor_time"], item["segment_id"]))
    if args.max_segments:
        segments = segments[: args.max_segments]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.resume and output_path.exists() else "w"
    progress = ProgressBar("classify", len(segments), enabled=args.progress)
    with output_path.open(mode, encoding="utf-8") as out:
        for segment_index, segment in enumerate(segments, 1):
            progress.update(segment_index - 1, suffix=f"written={written}")
            if segment_index <= start_index:
                continue
            chunks = approx_chunks(segment["records"], args.max_tokens, args.prompt_format)
            chunk_results = []
            for chunk_index, records in enumerate(chunks, 1):
                started = time.perf_counter()
                try:
                    response_text = gateway.chat(
                        args.model,
                        CLASSIFY_SYSTEM_PROMPT,
                        classify_prompt(segment, chunk_index, len(chunks), records, args.prompt_format),
                        args.timeout_seconds,
                    )
                    response = parse_json_response(response_text)
                except Exception as exc:
                    response = {"error": str(exc), "classification": "Not Interesting", "confidence": 0}
                chunk_results.append(
                    {
                        "chunk_index": chunk_index,
                        "event_count": len(records),
                        "elapsed_seconds": time.perf_counter() - started,
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
                            "chunks": chunk_results,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
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
        },
    )
    report_paths = generate_classify_report(output_path, output_path.parent)
    summary = {"segments": len(segments), "written": written, "output": str(output_path), "report": report_paths}
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
    small_name = safe_name(args.small_model)
    filter_output = run_dir / "filter" / small_name / "filtered_events.jsonl"
    filter_checkpoint = run_dir / "filter" / small_name / "checkpoint.json"

    filter_args = argparse.Namespace(
        input=args.input,
        output=str(filter_output),
        checkpoint=str(filter_checkpoint),
        model=args.small_model,
        ollama_url=args.ollama_url,
        timeout_seconds=args.filter_timeout_seconds,
        resume=args.resume,
        max_lines=args.max_filter_lines,
        prompt_format=args.prompt_format,
        pull_missing=args.pull_missing,
        delete_model_after_use=args.delete_model_after_use,
        log_file=getattr(args, "log_file", None),
        metrics=str(run_dir / "filter" / small_name / "metrics.json"),
        metrics_by_segment=str(run_dir / "filter" / small_name / "metrics_by_segment.csv"),
        progress=args.progress,
    )
    filter_records(filter_args)

    for model in args.big_model:
        big_name = safe_name(model)
        classify_output = run_dir / "classify" / small_name / big_name / "classifications.jsonl"
        classify_checkpoint = run_dir / "classify" / small_name / big_name / "checkpoint.json"
        classify_args = argparse.Namespace(
            input=str(filter_output),
            output=str(classify_output),
            checkpoint=str(classify_checkpoint),
            model=model,
            ollama_url=args.ollama_url,
            timeout_seconds=args.classify_timeout_seconds,
            resume=args.resume,
            include_irrelevant=args.include_irrelevant,
            max_events_per_segment=args.max_events_per_segment,
            max_tokens=args.max_tokens,
            max_segments=args.max_segments,
            prompt_format=args.prompt_format,
            pull_missing=args.pull_missing,
            delete_model_after_use=args.delete_model_after_use,
            log_file=getattr(args, "log_file", None),
            progress=args.progress,
        )
        classify_segments(classify_args)


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
        ollama_url=args.ollama_url,
        pull_missing=args.pull_missing,
        delete_model_after_use=args.delete_model_after_use,
        log_file=getattr(args, "log_file", None),
        prompt_format=args.prompt_format,
    )
    run_pipeline(pipeline_args)


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
        ollama_url=args.ollama_url,
        timeout_seconds=args.filter_timeout_seconds,
        resume=args.resume,
        prompt_format=args.prompt_format,
        pull_missing=args.pull_missing,
        delete_model_after_use=args.delete_model_after_use,
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
        big_name = safe_name(model)
        classify_output = run_dir / "classify" / small_name / big_name / "classifications.jsonl"
        classify_checkpoint = run_dir / "classify" / small_name / big_name / "checkpoint.json"
        classify_args = argparse.Namespace(
            input=str(filter_output),
            output=str(classify_output),
            checkpoint=str(classify_checkpoint),
            model=model,
            ollama_url=args.ollama_url,
            timeout_seconds=args.classify_timeout_seconds,
            resume=args.resume,
            include_irrelevant=args.include_irrelevant,
            max_events_per_segment=args.max_events_per_segment,
            max_tokens=args.max_tokens,
            max_segments=args.max_classify_segments,
            prompt_format=args.prompt_format,
            pull_missing=args.pull_missing,
            delete_model_after_use=args.delete_model_after_use,
            log_file=getattr(args, "log_file", None),
            progress=args.progress,
        )
        classify_segments(classify_args)


def add_common_ollama_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--pull-missing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--delete-model-after-use", action="store_true")
    parser.add_argument("--log-file")
    parser.add_argument("--prompt-format", choices=("csv", "json"), default="csv")


def default_log_file(args: argparse.Namespace) -> Path | None:
    command = getattr(args, "command", "")
    if getattr(args, "log_file", None):
        return Path(args.log_file)
    if command in {"run", "run-dataset", "run-direct"}:
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
    add_common_ollama_args(filter_cmd)
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
    extract_filter_cmd.add_argument("--keep-dropped", action="store_true")
    extract_filter_cmd.add_argument("--metrics")
    extract_filter_cmd.add_argument("--metrics-by-segment")
    extract_filter_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_ollama_args(extract_filter_cmd)
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
    classify_cmd.add_argument("--max-tokens", type=int, default=1500)
    classify_cmd.add_argument("--max-segments", type=int)
    classify_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_ollama_args(classify_cmd)
    classify_cmd.set_defaults(func=classify_segments)

    run_cmd = sub.add_parser("run", help="Run one small filtering model and N large classifiers.")
    run_cmd.add_argument("--input", required=True)
    run_cmd.add_argument("--run-dir", required=True)
    run_cmd.add_argument("--small-model", required=True)
    run_cmd.add_argument("--big-model", action="append", required=True)
    run_cmd.add_argument("--resume", action="store_true")
    run_cmd.add_argument("--include-irrelevant", action="store_true")
    run_cmd.add_argument("--max-filter-lines", type=int)
    run_cmd.add_argument("--max-events-per-segment", type=int, default=1000)
    run_cmd.add_argument("--max-tokens", type=int, default=1500)
    run_cmd.add_argument("--max-segments", type=int)
    run_cmd.add_argument("--filter-timeout-seconds", type=int, default=120)
    run_cmd.add_argument("--classify-timeout-seconds", type=int, default=180)
    run_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_ollama_args(run_cmd)
    run_cmd.set_defaults(func=run_pipeline)

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
    run_dataset_cmd.add_argument("--max-tokens", type=int, default=1500)
    run_dataset_cmd.add_argument("--max-segments", type=int)
    run_dataset_cmd.add_argument("--filter-timeout-seconds", type=int, default=120)
    run_dataset_cmd.add_argument("--classify-timeout-seconds", type=int, default=180)
    run_dataset_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_ollama_args(run_dataset_cmd)
    run_dataset_cmd.set_defaults(func=run_dataset_pipeline)

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
    run_direct_cmd.add_argument("--keep-dropped", action="store_true")
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
    run_direct_cmd.add_argument("--max-tokens", type=int, default=1500)
    run_direct_cmd.add_argument("--max-classify-segments", type=int)
    run_direct_cmd.add_argument("--filter-timeout-seconds", type=int, default=120)
    run_direct_cmd.add_argument("--classify-timeout-seconds", type=int, default=180)
    run_direct_cmd.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    add_common_ollama_args(run_direct_cmd)
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
    setup_logging(args)
    args.func(args)


if __name__ == "__main__":
    main()
