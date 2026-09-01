from __future__ import annotations

import hashlib
import logging
import os
import platform
import subprocess
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelRef:
    repo_id: str
    filename: str
    revision: str | None = None


def parse_model_ref(model: str) -> ModelRef:
    try:
        repository, filename = model.rsplit(":", 1)
    except ValueError as exc:
        raise ValueError(
            "Model must use 'repository[@revision]:filename.gguf', e.g. "
            "'bartowski/Llama-3.2-3B-Instruct-GGUF@main:Llama-3.2-3B-Instruct-Q4_K_M.gguf'."
        ) from exc
    if "@" in repository:
        repo_id, revision = repository.rsplit("@", 1)
    else:
        repo_id, revision = repository, None
    if not repo_id or not filename.endswith(".gguf") or revision == "":
        raise ValueError("Model must use 'repository[@revision]:filename.gguf'.")
    return ModelRef(repo_id, filename, revision)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolved_snapshot_revision(path: Path) -> str | None:
    parts = path.parts
    try:
        return parts[parts.index("snapshots") + 1]
    except (ValueError, IndexError):
        return None


def system_manifest(system_info: str) -> dict[str, Any]:
    memory = None
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError):
        pass
    nvidia = None
    try:
        nvidia = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "memory_bytes": memory,
        "llama_cpp_python": version("llama-cpp-python"),
        "llama_system_info": system_info,
        "nvidia_smi": nvidia,
    }


class LlamaCppGateway:
    def __init__(
        self,
        n_ctx: int,
        n_gpu_layers: int,
        n_batch: int,
        seed: int = 2026,
        max_output_tokens: int = 512,
        structured_output: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._n_batch = n_batch
        self._seed = seed
        self._max_output_tokens = max_output_tokens
        self._structured_output = structured_output
        self._models: dict[str, Any] = {}
        self._manifests: dict[str, dict[str, Any]] = {}
        self._last_response_metadata: dict[str, Any] = {}
        self._logger = logger

    def _load(self, model: str) -> Any:
        if model in self._models:
            return self._models[model]
        try:
            import llama_cpp
            from huggingface_hub import hf_hub_download
            from llama_cpp import Llama
        except ImportError as exc:
            raise SystemExit("Missing dependencies. Run 'uv sync'.") from exc

        ref = parse_model_ref(model)
        if self._logger:
            self._logger.info("Resolving/downloading %s:%s from Hugging Face.", ref.repo_id, ref.filename)
        started = time.perf_counter()
        path = Path(hf_hub_download(repo_id=ref.repo_id, filename=ref.filename, revision=ref.revision))
        resolve_download_seconds = time.perf_counter() - started

        started = time.perf_counter()
        file_sha256 = sha256_file(path)
        hash_seconds = time.perf_counter() - started

        started = time.perf_counter()
        llm = Llama(
            model_path=path.as_posix(),
            n_ctx=self._n_ctx,
            n_gpu_layers=self._n_gpu_layers,
            n_batch=self._n_batch,
            seed=self._seed,
            verbose=False,
        )
        load_seconds = time.perf_counter() - started
        system_info = llama_cpp.llama_print_system_info().decode(errors="replace")
        metadata = getattr(llm, "metadata", {}) or {}
        chat_template = metadata.get("tokenizer.chat_template")
        selected_metadata = {
            key: metadata.get(key)
            for key in (
                "general.name",
                "general.architecture",
                "general.file_type",
                "general.quantization_version",
            )
            if key in metadata
        }
        if isinstance(chat_template, str):
            selected_metadata["tokenizer.chat_template_sha256"] = hashlib.sha256(chat_template.encode()).hexdigest()
        self._models[model] = llm
        self._manifests[model] = {
            "reference": model,
            "repository": ref.repo_id,
            "filename": ref.filename,
            "requested_revision": ref.revision,
            "resolved_revision": resolved_snapshot_revision(path),
            "sha256": file_sha256,
            "size_bytes": path.stat().st_size,
            "metadata": selected_metadata,
            "configuration": {
                "n_ctx": self._n_ctx,
                "n_gpu_layers": self._n_gpu_layers,
                "n_batch": self._n_batch,
                "seed": self._seed,
                "max_output_tokens": self._max_output_tokens,
                "structured_output": self._structured_output,
                "temperature": 0,
            },
            "setup_timing": {
                "resolve_download_seconds": resolve_download_seconds,
                "sha256_seconds": hash_seconds,
                "load_seconds": load_seconds,
                "warmup_runs": 0,
                "warmup_seconds": [],
            },
            "environment": system_manifest(system_info),
        }
        return llm

    def prepare(self, model: str, warmup_runs: int = 1) -> dict[str, Any]:
        llm = self._load(model)
        timing = self._manifests[model]["setup_timing"]
        if timing["warmup_runs"] == 0 and warmup_runs:
            for _ in range(warmup_runs):
                started = time.perf_counter()
                llm.create_chat_completion(
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    temperature=0,
                    seed=self._seed,
                    max_tokens=2,
                )
                timing["warmup_seconds"].append(time.perf_counter() - started)
            timing["warmup_runs"] = warmup_runs
        return self.model_manifest(model)

    def model_manifest(self, model: str) -> dict[str, Any]:
        if model not in self._manifests:
            self._load(model)
        return dict(self._manifests[model])

    def chat(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: int | None = None,
    ) -> str:
        del timeout_seconds  # In-process inference cannot be interrupted by the former HTTP timeout.
        request = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "seed": self._seed,
            "max_tokens": self._max_output_tokens,
        }
        if self._structured_output:
            request["response_format"] = {"type": "json_object"}
        response = self._load(model).create_chat_completion(
            **request,
        )
        try:
            choice = response["choices"][0]
            self._last_response_metadata = {
                "finish_reason": choice.get("finish_reason"),
                "usage": response.get("usage", {}),
            }
            return str(choice["message"]["content"] or "")
        except (IndexError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Unexpected llama.cpp response shape: {response!r}") from exc

    @property
    def last_response_metadata(self) -> dict[str, Any]:
        return dict(self._last_response_metadata)

    def prompt_token_count(self, model: str, system_prompt: str, user_prompt: str) -> int:
        """Conservatively count the prompt before reserving output tokens.

        The chat template adds a small amount of model-specific overhead. Callers
        reserve a safety margin in addition to this count before inference.
        """
        llm = self._load(model)
        prompt = f"{system_prompt}\n{user_prompt}"
        return len(llm.tokenize(prompt.encode("utf-8"), add_bos=True, special=True))
