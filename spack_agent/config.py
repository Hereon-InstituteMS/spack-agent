"""TOML configuration for spack-agent."""

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import tomllib


DEFAULT_CONFIG_PATH = Path("spack-agent.toml")
TOOLCHAINS = {
    "gcc": ("spack-agent-builder:gcc", "spack-agent-store-gcc"),
    "clang": ("spack-agent-builder:clang", "spack-agent-store-clang"),
    "intel": ("spack-agent-builder:intel", "spack-agent-store-intel"),
}


@dataclass(frozen=True)
class RunnerConfig:
    """Execution backend for generated verification scripts."""

    backend: str = "host"
    poll_seconds: float = 10.0
    max_iterations: int = 5
    toolchain: str = "gcc"
    cpus: float | None = None
    memory: str | None = None
    network: str | None = None
    download_volume: str = "spack-agent-downloads"

    @property
    def image(self) -> str:
        return TOOLCHAINS[self.toolchain][0]

    @property
    def install_volume(self) -> str:
        return TOOLCHAINS[self.toolchain][1]


@dataclass(frozen=True)
class WorkspaceConfig:
    """The source being packaged and the repository receiving its recipe."""

    writable_repository: Path
    spack_repository: Path
    recipe_path: Path
    host_spack_executable: Path | None
    source_repository: Path

    @property
    def spack_repository_dir(self) -> Path:
        return (self.writable_repository / self.spack_repository).resolve()

    @property
    def recipe_file(self) -> Path:
        return (self.spack_repository_dir / self.recipe_path).resolve()


@dataclass(frozen=True)
class AgentConfig:
    workspace: WorkspaceConfig
    goal_context: str
    verification_spec: str
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    model: str | None = None
    agent_timeout_seconds: float = 900
    allow_tools: list[str] = field(default_factory=lambda: ["write", "shell"])
    deny_tools: list[str] = field(
        default_factory=lambda: [
            "shell(git push*)",
            "shell(git reset*)",
            "shell(git clean*)",
            "shell(rm*)",
        ]
    )
    allow_all_paths: bool = False
    allow_all_urls: bool = False
    _config_directory: Path = field(default_factory=Path.cwd, repr=False)

    @property
    def state_dir(self) -> Path:
        return self._config_directory / ".spack-agent"

    @property
    def goal_text(self) -> str:
        return f"Verification spec: {self.verification_spec}\n\nUser context:\n{self.goal_context}"

    @property
    def workspace_fingerprint(self) -> str:
        """Stable identity of paths that must not change during a session."""
        payload = {
            "source_repository": str(self.workspace.source_repository),
            "writable_repository": str(self.workspace.writable_repository),
            "spack_repository": str(self.workspace.spack_repository_dir),
            "recipe_file": str(self.workspace.recipe_file),
            "host_spack_executable": (
                str(self.workspace.host_spack_executable)
                if self.runner.backend == "host"
                else None
            ),
            "runner": {
                "backend": self.runner.backend,
                "toolchain": self.runner.toolchain,
                "network": self.runner.network,
                "download_volume": self.runner.download_volume,
            },
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AgentConfig:
    """Load role-based Spack workspace, goal, and agent settings."""
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ValueError(f"configuration file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {config_path}: {exc}") from exc

    base_dir = config_path.parent
    workspace_values = _table(raw, "workspace", config_path)
    goal_values = _table(raw, "goal", config_path)
    agent_values = _table(raw, "agent", config_path)
    runner_values = raw.get("runner", {})
    if not isinstance(runner_values, dict):
        raise ValueError(f"{config_path} [runner] must be a table")
    _reject_unknown(
        workspace_values,
        {
            "writable_repository",
            "spack_repository",
            "recipe_path",
            "host_spack_executable",
            "source_repository",
        },
        "workspace",
    )
    _reject_unknown(goal_values, {"context", "spec"}, "goal")
    for setting in ("spec", "context"):
        if setting not in goal_values:
            raise ValueError(f"goal.{setting} is required")
    _reject_unknown(
        runner_values,
        {
            "backend",
            "poll_seconds",
            "max_iterations",
            "toolchain",
            "cpus",
            "memory",
            "network",
            "download_volume",
        },
        "runner",
    )
    _reject_unknown(
        agent_values,
        {
            "model",
            "agent_timeout_seconds",
            "allow_tools",
            "deny_tools",
            "allow_all_paths",
            "allow_all_urls",
        },
        "agent",
    )

    try:
        writable_repository = _resolve(base_dir, workspace_values["writable_repository"])
        recipe_path = Path(workspace_values["recipe_path"])
        host_spack_value = workspace_values.get("host_spack_executable")
        host_spack_executable = (
            _resolve(base_dir, host_spack_value)
            if host_spack_value is not None
            else None
        )
        workspace = WorkspaceConfig(
            writable_repository=writable_repository,
            spack_repository=Path(workspace_values["spack_repository"]),
            recipe_path=recipe_path,
            host_spack_executable=host_spack_executable,
            source_repository=_resolve(base_dir, workspace_values["source_repository"]),
        )
        config = AgentConfig(
            workspace=workspace,
            goal_context=goal_values["context"],
            verification_spec=goal_values["spec"],
            runner=RunnerConfig(**runner_values),
            _config_directory=base_dir,
            **agent_values,
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"missing or invalid setting in {config_path}: {exc}") from exc

    if not isinstance(config.goal_context, str):
        raise ValueError("goal.context must be a string")
    if not config.goal_context.strip():
        raise ValueError("goal.context must not be empty")
    if not isinstance(config.verification_spec, str) or not config.verification_spec.strip():
        raise ValueError("goal.spec must be a non-empty string")
    if not workspace.writable_repository.is_dir():
        raise ValueError(
            "workspace.writable_repository is not a directory: "
            f"{workspace.writable_repository}"
        )
    if workspace.spack_repository.is_absolute() or ".." in workspace.spack_repository.parts:
        raise ValueError(
            "workspace.spack_repository must stay within writable_repository"
        )
    if not workspace.spack_repository_dir.is_dir():
        raise ValueError(
            f"configured Spack repository does not exist: {workspace.spack_repository_dir}"
        )
    if not workspace.spack_repository_dir.is_relative_to(workspace.writable_repository):
        raise ValueError(
            "workspace.spack_repository resolves outside writable_repository"
        )
    if workspace.recipe_path.is_absolute() or ".." in workspace.recipe_path.parts:
        raise ValueError("workspace.recipe_path must stay within spack_repository")
    if not workspace.recipe_file.is_relative_to(workspace.spack_repository_dir):
        raise ValueError("workspace.recipe_path resolves outside spack_repository")
    if config.runner.backend == "host" and workspace.host_spack_executable is None:
        raise ValueError("workspace.host_spack_executable is required for host runner")
    if (
        workspace.host_spack_executable is not None
        and not workspace.host_spack_executable.is_file()
    ):
        raise ValueError(
            "workspace.host_spack_executable is not a file: "
            f"{workspace.host_spack_executable}"
        )
    if not workspace.source_repository.is_dir():
        raise ValueError(
            "workspace.source_repository is not a directory: "
            f"{workspace.source_repository}"
        )
    if config.model is not None and (
        not isinstance(config.model, str) or not config.model.strip()
    ):
        raise ValueError("agent.model must be a non-empty string")
    if (
        isinstance(config.agent_timeout_seconds, bool)
        or not isinstance(config.agent_timeout_seconds, (int, float))
        or config.agent_timeout_seconds <= 0
    ):
        raise ValueError("agent.agent_timeout_seconds must be greater than zero")
    for setting, values in (
        ("allow_tools", config.allow_tools),
        ("deny_tools", config.deny_tools),
    ):
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(f"agent.{setting} must be an array of non-empty strings")
    for setting, value in (
        ("allow_all_paths", config.allow_all_paths),
        ("allow_all_urls", config.allow_all_urls),
    ):
        if not isinstance(value, bool):
            raise ValueError(f"agent.{setting} must be a boolean")
    if not isinstance(config.runner.backend, str) or config.runner.backend not in {
        "host",
        "podman",
    }:
        raise ValueError("runner.backend must be 'host' or 'podman'")
    if (
        isinstance(config.runner.poll_seconds, bool)
        or not isinstance(config.runner.poll_seconds, (int, float))
        or config.runner.poll_seconds <= 0
    ):
        raise ValueError("runner.poll_seconds must be greater than zero")
    if (
        isinstance(config.runner.max_iterations, bool)
        or not isinstance(config.runner.max_iterations, int)
        or config.runner.max_iterations <= 0
    ):
        raise ValueError("runner.max_iterations must be a positive integer")
    if config.runner.cpus is not None and (
        isinstance(config.runner.cpus, bool)
        or not isinstance(config.runner.cpus, (int, float))
        or config.runner.cpus <= 0
    ):
        raise ValueError("runner.cpus must be greater than zero")
    if (
        not isinstance(config.runner.toolchain, str)
        or config.runner.toolchain not in TOOLCHAINS
    ):
        choices = ", ".join(sorted(TOOLCHAINS))
        raise ValueError(f"runner.toolchain must be one of: {choices}")
    if config.runner.memory is not None and (
        not isinstance(config.runner.memory, str) or not config.runner.memory.strip()
    ):
        raise ValueError("runner.memory must be a non-empty string")
    if config.runner.network is not None and (
        not isinstance(config.runner.network, str) or not config.runner.network.strip()
    ):
        raise ValueError("runner.network must not be empty")
    for setting, value in (("download_volume", config.runner.download_volume),):
        if not isinstance(value, str) or not value or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
            for character in value
        ):
            raise ValueError(f"runner.{setting} must be a Podman volume name")
    return config


def describe_config(config: AgentConfig) -> str:
    """Render resolved path roles for inspection before an agent run."""
    recipe_action = "adapt existing" if config.workspace.recipe_file.is_file() else "create new"
    return "\n".join(
        [
            "Spack workspace:",
            f"  Project being packaged: {config.workspace.source_repository}",
            f"  Writable repository : {config.workspace.writable_repository}",
            f"  Spack repository    : {config.workspace.spack_repository_dir}",
            f"  Recipe destination  : {config.workspace.recipe_file}",
            f"  Recipe action       : {recipe_action}",
            f"  Host Spack          : {config.workspace.host_spack_executable or '(not used)'}",
            f"  Runtime state       : {config.state_dir}",
            f"  Runner backend      : {config.runner.backend}",
            f"  Poll interval       : {config.runner.poll_seconds}s",
            f"  Maximum iterations  : {config.runner.max_iterations}",
            f"  Toolchain           : {config.runner.toolchain if config.runner.backend == 'podman' else '(not used)'}",
            f"  Runner image        : {config.runner.image if config.runner.backend == 'podman' else '(not used)'}",
            f"  Verification spec   : {config.verification_spec or '(not configured)'}",
            f"  Model               : {config.model or 'Copilot default'}",
        ]
    )


def _table(raw: dict, name: str, config_path: Path) -> dict:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{config_path} must contain a [{name}] table")
    return value


def _reject_unknown(values: dict, known: set[str], table: str) -> None:
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"unknown [{table}] setting(s): {', '.join(sorted(unknown))}")


def _resolve(base_dir: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return (base_dir / path).resolve() if not path.is_absolute() else path.resolve()