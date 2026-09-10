"""Subprocess adapter for the GitHub Copilot CLI."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess


_LOCAL_COPILOT = os.path.expanduser("~/.local/copilot-cli/bin/copilot")
COPILOT_BIN = os.environ.get("COPILOT_CLI_PATH") or (
    _LOCAL_COPILOT if os.access(_LOCAL_COPILOT, os.X_OK) else "copilot"
)
_LOCAL_NODE_BIN = os.path.expanduser("~/.local/node/bin")
_STATS_RE = re.compile(
    r"AI Credits\s+(?P<credits>[\d.,]+)\s*\((?P<duration>[^)]+)\)\s*\n"
    r"Tokens\s+\u2191\s*(?P<tin>[\d.,]+k?)\s*"
    r"\(\s*(?P<cached>[\d.,]+k?)\s*cached,\s*"
    r"(?P<written>[\d.,]+k?)\s*written\)\s*"
    r"\u2022\s*\u2193\s*(?P<tout>[\d.,]+k?)"
)
_RESUME_RE = re.compile(r"--resume[= ]([0-9a-fA-F][0-9a-fA-F-]{7,})")


def _subprocess_env() -> dict[str, str]:
    environment = os.environ.copy()
    if os.path.isdir(_LOCAL_NODE_BIN):
        environment["PATH"] = os.pathsep.join([_LOCAL_NODE_BIN, environment.get("PATH", "")])
    return environment


def is_confined_snap_copilot(binary: str = COPILOT_BIN) -> bool:
    resolved = shutil.which(binary) or binary
    return "/snap/" in resolved or os.path.realpath(resolved).endswith("/usr/bin/snap")


@dataclass
class CopilotResult:
    text: str
    ai_credits: str | None = None
    duration: str | None = None
    error: str | None = None
    resume_id: str | None = None

    @property
    def ai_credits_value(self) -> float | None:
        try:
            return float(self.ai_credits.replace(",", "")) if self.ai_credits else None
        except ValueError:
            return None

    def usage_summary(self) -> str:
        if self.error:
            return f"call failed ({self.error})"
        if self.ai_credits is None:
            return "usage stats unavailable"
        return f"{self.ai_credits} AI credits in {self.duration}"


def ask_copilot(
    prompt: str,
    *,
    model: str | None,
    cwd: Path,
    allow_tools: list[str],
    deny_tools: list[str],
    add_dirs: list[Path],
    allow_all_paths: bool,
    allow_all_urls: bool,
    resume: str | None = None,
    timeout: float,
) -> CopilotResult:
    command = [COPILOT_BIN, "-p", prompt]
    if model:
        command += ["--model", model]
    for tool in allow_tools:
        command += ["--allow-tool", tool]
    for tool in deny_tools:
        command += ["--deny-tool", tool]
    for directory in add_dirs:
        command += ["--add-dir", str(directory)]
    if allow_all_paths:
        command.append("--allow-all-paths")
    if allow_all_urls:
        command.append("--allow-all-urls")
    if resume:
        command.append(f"--resume={resume}")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            env=_subprocess_env(),
            timeout=timeout,
        )
    except FileNotFoundError:
        return CopilotResult(text="", error=f"Copilot CLI not found: {COPILOT_BIN}")
    except subprocess.TimeoutExpired:
        return CopilotResult(text="", error=f"timed out after {timeout}s")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else f"exit code {exc.returncode}"
        return CopilotResult(text="", error=detail)

    stats = _STATS_RE.search(result.stderr)
    resume_match = _RESUME_RE.search(result.stderr)
    return CopilotResult(
        text=result.stdout.strip(),
        ai_credits=stats.group("credits") if stats else None,
        duration=stats.group("duration") if stats else None,
        resume_id=resume_match.group(1) if resume_match else None,
    )