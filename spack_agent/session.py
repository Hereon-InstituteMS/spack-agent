"""Persistent state and build-log evaluation for agent verification sessions."""

from dataclasses import asdict, dataclass, field
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil


MAX_ATTEMPTS_PER_SIGNATURE = 2
_SIGNATURE_PATTERNS = (
    r"([^\n]+: No such file or directory)",
    r"(\d+ out of \d+ hunks FAILED[^\n]*)",
    r"fatal error:\s*(.+)",
    r"CMake Error[^:]*:\s*(.+)",
    r"error:\s*(.+)",
    r"==> Error:\s*(.+)",
    r"FAILED:\s*(.+)",
    r"ModuleNotFoundError:\s*(.+)",
    r"ImportError:\s*(.+)",
)
_NOISE = (
    (re.compile(r"/tmp/[^\s'\"]+"), "<tmp>"),
    (re.compile(r"\b[0-9a-f]{7,}\b"), "<hash>"),
    (re.compile(r":\d+:\d+"), ":<pos>"),
    (re.compile(r"\s+"), " "),
)


def _normalize_signature(signature: str) -> str:
    for regex, replacement in _NOISE:
        signature = regex.sub(replacement, signature)
    return signature[:200]


def _cmake_error_signature(lines: list[str], index: int) -> str:
    for message in lines[index + 1:index + 9]:
        message = message.strip()
        if message and not message.startswith(("Call Stack", "--")):
            return _normalize_signature(f"CMake Error: {message}")
    return _normalize_signature(lines[index].strip())


def failure_category(log_text: str) -> str:
    if re.search(r"concretiz|unsatisfiable|no valid.*spec", log_text, re.IGNORECASE):
        return "concretization"
    if re.search(r"hunks FAILED|corrupt patch|patch.*failed", log_text, re.IGNORECASE):
        return "patch"
    if re.search(r"CMake Error|configur.*error", log_text, re.IGNORECASE):
        return "configuration"
    if re.search(r"undefined reference|undefined symbol", log_text, re.IGNORECASE):
        return "link"
    if re.search(r"fatal error:|error:", log_text, re.IGNORECASE):
        return "compile"
    return "verification"


def error_signature(log_text: str) -> str:
    lines = log_text.splitlines()
    for index, line in enumerate(lines):
        if re.search(r"CMake Error", line, re.IGNORECASE):
            return _cmake_error_signature(lines, index)
        for pattern in _SIGNATURE_PATTERNS:
            match = re.search(pattern, line, re.IGNORECASE)
            if not match:
                continue
            signature = match.group(1).strip()
            if signature == "The following packages failed to install:":
                continue
            return _normalize_signature(signature)
    return "verification failed without a recognized error"


def dag_identity(spec_output: str) -> str | None:
    """Return a stable identity for a successfully concretized dependency DAG."""
    if not spec_output.strip():
        return None
    return hashlib.sha256(spec_output.encode("utf-8")).hexdigest()


def evaluate_log(log_text: str) -> tuple[bool, str | None]:
    marker = "=== VERDICT ==="
    marker_at = log_text.rfind(marker)
    if marker_at < 0:
        return False, "missing verdict block"
    verdict = {}
    for line in log_text[marker_at + len(marker):].splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in {"exit_code", "goal_met"}:
            verdict[key.strip()] = value.strip().lower()
    if verdict.get("exit_code") == "0" and verdict.get("goal_met") == "yes":
        return True, None
    return False, error_signature(log_text)


@dataclass
class Attempt:
    step: int
    script_path: str = ""
    log_path: str = ""
    error_signature: str | None = None
    failure_category: str | None = None
    causal_log_path: str | None = None
    result_path: str = ""
    dag_identity: str | None = None
    succeeded: bool = False
    summary: str = ""
    ai_credits: float = 0.0


@dataclass
class Session:
    goal: str = ""
    repo_path: str = ""
    model: str | None = None
    step: int = 0
    resume_id: str | None = None
    workspace_fingerprint: str = ""
    done: bool = False
    attempts: list[Attempt] = field(default_factory=list)

    def attempts_for(self, signature: str | None) -> int:
        if not signature:
            return 0
        return sum(attempt.error_signature == signature for attempt in self.attempts)

    def is_stuck_on(self, signature: str | None) -> bool:
        return self.attempts_for(signature) >= MAX_ATTEMPTS_PER_SIGNATURE

    def total_credits(self) -> float:
        return sum(attempt.ai_credits for attempt in self.attempts)

    def history_digest(self, limit: int = 6) -> str:
        if not self.attempts:
            return "(no previous attempts)"
        lines = []
        for attempt in self.attempts[-limit:]:
            status = "OK" if attempt.succeeded else f"FAILED [{attempt.error_signature or 'unknown'}]"
            lines.append(f"  step {attempt.step}: {status} - {attempt.summary[:200]}")
        return "\n".join(lines)


class SessionStore:
    """Session persistence scoped to one configured runtime directory."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.session_file = state_dir / "session.json"
        self.lock_file = state_dir / "session.lock"

    @contextmanager
    def lock(self):
        """Prevent two commands from running or updating one session at once."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_file.open("a+", encoding="utf-8") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionLockedError(
                    f"another spack-agent command is using {self.state_dir}"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def load(self) -> Session | None:
        if not self.session_file.is_file():
            return None
        with self.session_file.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
        raw.pop("config_fingerprint", None)
        attempts = [Attempt(**attempt) for attempt in raw.pop("attempts", [])]
        return Session(**raw, attempts=attempts)

    def save(self, session: Session) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.session_file.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(asdict(session), stream, indent=2)
        temporary.replace(self.session_file)

    def clear(self) -> None:
        self.session_file.unlink(missing_ok=True)

    def clear_artifacts(self) -> None:
        """Remove prior run state while retaining the lock held by this process."""
        if not self.state_dir.is_dir():
            return
        for path in self.state_dir.iterdir():
            if path == self.lock_file:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    def step_paths(self, step: int) -> tuple[str, str]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return (
            str(self.state_dir / f"step_{step:02d}.sh"),
            str(self.state_dir / f"step_{step:02d}.log"),
        )


class SessionLockedError(RuntimeError):
    pass