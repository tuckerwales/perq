"""Stream responses from the Claude Code CLI."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from collections.abc import AsyncIterator

STREAM_LIMIT = 10 * 1024 * 1024  # generous line buffer for big JSON events


class ClaudeNotFoundError(Exception):
    pass


class ClaudeError(Exception):
    pass


class ClaudeRunner:
    """Runs `claude -p` and yields assistant text as it streams."""

    def __init__(self, executable: str = "claude") -> None:
        self.executable = executable
        self._process: asyncio.subprocess.Process | None = None

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        path = shutil.which(self.executable)
        if path is None:
            raise ClaudeNotFoundError(
                "The `claude` CLI was not found on PATH. Install Claude Code first."
            )

        self._process = await asyncio.create_subprocess_exec(
            path,
            "-p",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--max-turns",
            "1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
            start_new_session=True,
        )
        process = self._process
        assert process.stdin and process.stdout and process.stderr

        process.stdin.write(prompt.encode())
        process.stdin.write_eof()

        saw_partial = False
        result_error: str | None = None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                kind = event.get("type")
                if kind == "stream_event":
                    delta = event.get("event", {}).get("delta", {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        saw_partial = True
                        yield delta["text"]
                elif kind == "assistant" and not saw_partial:
                    # Fallback when partial events aren't emitted.
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            yield block["text"]
                elif kind == "result" and event.get("is_error"):
                    result_error = str(event.get("result", "unknown error"))

            stderr = (await process.stderr.read()).decode(errors="replace").strip()
            returncode = await process.wait()
            if result_error:
                raise ClaudeError(result_error)
            if returncode != 0:
                raise ClaudeError(stderr or f"claude exited with code {returncode}")
        finally:
            self.stop()
            self._process = None

    def stop(self) -> None:
        """Kill the claude process (and its children) if still running."""
        process = self._process
        if process is not None and process.returncode is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
