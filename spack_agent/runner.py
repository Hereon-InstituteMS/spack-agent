"""Run generated build-verification scripts without a timeout."""

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import signal
import shutil
import subprocess
import time

from .config import AgentConfig
from .session import SessionStore, dag_identity
from .workflow import (
    process_result,
    revise_goal,
    session_matches_workspace,
    show_status,
    start_goal,
)


_PODMAN_INFRASTRUCTURE_EXIT_CODES = {125, 126, 127, 137}
_CONTAINER_DIRECTORY = Path(__file__).resolve().parent.parent / "container"


@dataclass(frozen=True)
class PreflightResult:
    output: str
    succeeded: bool
    dag_identity: str | None


def _package_name(spec: str) -> str:
    return spec.split(maxsplit=1)[0].split("@", maxsplit=1)[0].split("+", maxsplit=1)[0]


def run_preflight(config: AgentConfig) -> PreflightResult:
    """Run cheap recipe checks and concretization before asking the agent to plan."""
    spack = "/opt/spack/bin/spack" if config.runner.backend == "podman" else str(
        config.workspace.host_spack_executable
    )
    recipe = (
        "/workspace/packages/"
        f"{config.workspace.recipe_file.relative_to(config.workspace.writable_repository)}"
        if config.runner.backend == "podman"
        else str(config.workspace.recipe_file)
    )
    package = _package_name(config.verification_spec)
    command_text = "\n".join(
        [
            "set -o pipefail",
            f"ruff check {shlex.quote(recipe)}",
            f"python3 -m py_compile {shlex.quote(recipe)}",
            f"{shlex.quote(spack)} audit packages {shlex.quote(package)}",
            "echo '=== CONCRETIZED DAG ==='",
            f"{shlex.quote(spack)} spec -Il {shlex.quote(config.verification_spec)}",
        ]
    )
    if config.runner.backend == "podman":
        command = _podman_command(config, "preflight.sh")[:-2] + ["bash", "-c", command_text]
    else:
        command = ["bash", "-c", command_text]
    result = subprocess.run(
        command,
        cwd=config.workspace.writable_repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = result.stdout[-12000:]
    dag_output = output.partition("=== CONCRETIZED DAG ===\n")[2] if result.returncode == 0 else ""
    return PreflightResult(output, result.returncode == 0, dag_identity(dag_output))


def terminate_process_group(process: subprocess.Popen, grace_seconds: float = 10.0) -> None:
    """Terminate a build and every descendant, then escalate if necessary."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _podman_command(config: AgentConfig, script_path: str) -> list[str]:
    runner = config.runner
    command = ["podman", "run", "--rm"]
    if runner.cpus is not None:
        command.extend(["--cpus", str(runner.cpus)])
    if runner.memory is not None:
        command.extend(["--memory", runner.memory])
    if runner.network is not None:
        command.extend(["--network", runner.network])
    command.extend(
        [
            "--volume",
            f"{config.workspace.source_repository}:/workspace/source:ro",
            "--volume",
            f"{config.workspace.writable_repository}:/workspace/packages:rw",
            "--volume",
            f"{config.state_dir}:/workspace/state:rw",
            "--volume",
            f"{runner.download_volume}:/var/cache/spack:rw",
            "--volume",
            f"{runner.install_volume}:/opt/spack-store:rw",
            "--workdir",
            "/workspace/packages",
            "--env",
            f"SPACK_AGENT_REPOSITORY=/workspace/packages/{config.workspace.spack_repository}",
            runner.image,
            "bash",
            f"/workspace/state/{Path(script_path).name}",
        ]
    )
    return command


def _podman_image_exists(image: str) -> bool:
    result = subprocess.run(
        ["podman", "image", "exists", image],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _build_podman_image(config: AgentConfig) -> bool:
    """Build the selected image from the bundled toolchain definition."""
    containerfile = _CONTAINER_DIRECTORY / f"Containerfile.{config.runner.toolchain}"
    if not containerfile.is_file():
        print(f"[spack-agent] Toolchain definition not found: {containerfile}")
        return False
    command = [
        "podman",
        "build",
        "--file",
        str(containerfile),
        "--tag",
        config.runner.image,
        str(_CONTAINER_DIRECTORY.parent),
    ]
    print(
        f"[spack-agent] Podman image not found: {config.runner.image}. "
        f"Building the {config.runner.toolchain} toolchain image now; this may "
        "download a base image and take several minutes.",
        flush=True,
    )
    return subprocess.run(command, check=False).returncode == 0


def run_step_script(
    config: AgentConfig, script_path: str, log_path: str, poll: float
) -> int:
    if config.runner.backend == "podman":
        step_command = _podman_command(config, script_path)
    else:
        step_command = ["bash", script_path]
    rendered = shlex.join(step_command)
    command = f"set -o pipefail; {rendered} 2>&1 | tee {shlex.quote(log_path)}"
    print(f"\n$ bash -c {shlex.quote(command)}", flush=True)
    started = time.monotonic()
    process = subprocess.Popen(
        ["bash", "-c", command],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    while process.poll() is None:
        try:
            time.sleep(poll)
        except KeyboardInterrupt:
            terminate_process_group(process)
            print("\n[spack-agent] Interrupted; current build was terminated.")
            return 130
        elapsed = int(time.monotonic() - started)
        print(f"[spack-agent] Build still running ({elapsed}s elapsed)...", flush=True)
    return process.returncode


def run_workflow(config: AgentConfig, resume: bool) -> int:
    if config.runner.backend == "podman" and shutil.which("podman") is None:
        print("[spack-agent] Podman is not installed or not on PATH.")
        return 1
    if config.runner.backend == "podman" and not _podman_image_exists(
        config.runner.image
    ) and not _build_podman_image(config):
        print("[spack-agent] Podman image build failed; the agent was not started.")
        return 1
    store = SessionStore(config.state_dir)
    session = store.load()
    if not resume:
        store.clear_artifacts()
        preflight = run_preflight(config)
        result = start_goal(
            config,
            store,
            force=True,
            preflight_output=preflight.output,
            preflight_dag_identity=preflight.dag_identity,
        )
        if result != 0:
            return result
    else:
        if session is None:
            print("[spack-agent] No session to resume. Run without --resume to start fresh.")
            return 1
        if not session_matches_workspace(session, config):
            return 1
        if session.goal != config.goal_text:
            preflight = run_preflight(config)
            if revise_goal(
                config,
                store,
                preflight.output,
                preflight.dag_identity,
            ) != 0:
                return 1
            session = store.load()
        elif session.done:
            print(
                "[spack-agent] Session is already finished. Edit [goal].context "
                "before resuming, or run without --resume to start fresh."
            )
            return 1
        print(f"[spack-agent] Resuming at step {session.step}.")

    for _ in range(config.runner.max_iterations):
        session = store.load()
        if session is None or not session.attempts:
            print("[spack-agent] Session state is incomplete.")
            return 1
        if session.done:
            break
        attempt = session.attempts[-1]
        if not Path(attempt.script_path).is_file():
            print(f"[spack-agent] Script not found: {attempt.script_path}")
            return 1
        exit_code = run_step_script(
            config,
            attempt.script_path,
            attempt.log_path,
            config.runner.poll_seconds,
        )
        if exit_code == 130:
            return 130
        if (
            config.runner.backend == "podman"
            and exit_code in _PODMAN_INFRASTRUCTURE_EXIT_CODES
        ):
            print(
                "[spack-agent] Podman failed before verification completed "
                f"(exit code {exit_code}); the agent was not restarted."
            )
            return 1
        preflight = run_preflight(config) if exit_code != 0 else None
        result = process_result(
            config,
            store,
            preflight_output=preflight.output if preflight else "",
            preflight_dag_identity=preflight.dag_identity if preflight else None,
        )
        if result in {1, 2}:
            show_status(store)
            return result
    else:
        print(
            "[spack-agent] Reached the "
            f"{config.runner.max_iterations}-iteration safety cap."
        )
        show_status(store)
        return 2
    return show_status(store)