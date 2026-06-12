from __future__ import annotations

import sys
import time


class ProgressBar:
    def __init__(self, label: str, total: int | None = None, enabled: bool = True, width: int = 28) -> None:
        self.label = label
        self.total = total if total and total > 0 else None
        self.enabled = enabled
        self.width = width
        self.current = 0
        self.started = time.perf_counter()
        self.last_render = 0.0

    def update(self, current: int | None = None, step: int = 0, suffix: str = "", force: bool = False) -> None:
        if not self.enabled:
            return
        if current is not None:
            self.current = current
        else:
            self.current += step
        now = time.perf_counter()
        if not force and now - self.last_render < 0.2:
            return
        self.last_render = now
        elapsed = max(now - self.started, 0.001)
        rate = self.current / elapsed

        if self.total:
            ratio = min(max(self.current / self.total, 0.0), 1.0)
            filled = int(self.width * ratio)
            bar = "#" * filled + "-" * (self.width - filled)
            percent = ratio * 100
            message = f"\r{self.label} [{bar}] {percent:6.2f}% {self.current}/{self.total}"
        else:
            message = f"\r{self.label} {self.current}"

        if rate:
            message += f" {rate:,.0f}/s"
        if suffix:
            message += f" {suffix}"
        sys.stderr.write(message)
        sys.stderr.flush()

    def close(self, suffix: str = "") -> None:
        if not self.enabled:
            return
        self.update(force=True, suffix=suffix)
        sys.stderr.write("\n")
        sys.stderr.flush()
