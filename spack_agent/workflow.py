"""Iterate agent planning and build verification for long-running checks."""

from pathlib import Path
import json
import re

from .config import AgentConfig
from .copilot import ask_copilot
from .session import (
    MAX_ATTEMPTS_PER_SIGNATURE,
    Attempt,
    Session,
    SessionStore,
    error_signature,
    evaluate_log,
    failure_category,
)


_TAIL_LINES = 80
_MAX_ERROR_LINES = 40
_ERROR_LINE_RE = re.compile(
    r"(fatal error|CMake Error|\berror:|==> Error|FAILED:|Traceback|"
    r"ModuleNotFoundError|ImportError|No such file)",
    re.IGNORECASE,
)
_SCRIPT_CONTRACT = """
Write the script to EXACTLY this path: {script_path}

Script rules:
- It must be a self-contained bash script that runs non-interactively.
- Put only long-running, privileged, or high-output work in it. You may do
  quick inspections yourself now.
- Do not run `spack compiler find` or alter Spack configuration. In container
    mode, the selected toolchain is already configured by the image.
- It must not require user input. stdin will be /dev/null, so pass the
  appropriate non-interactive flags to commands that can prompt.
- Use the configured exact spec as the final install target. Do not add
    version-specific dependency assertions unless the goal explicitly requires
    them; the runner records standard recipe and concretization evidence.
- It must preserve command failures and end with this machine-readable block:

    echo
    echo "=== VERDICT ==="
    echo "exit_code=$rc"
    echo "goal_met=<yes|no>"
    # Add 2-5 short evidence lines.

Set goal_met=yes only after programmatically verifying the full goal. The
agent will not be started again after a successful verdict.
"""
_GOAL_CONTRACT = """
The configured verification spec is the exact final install target. Make only
recipe changes in the writable repository that are required for that spec and
that remain valid across relevant compilers. Do not modify the source checkout
or unrelated packages. Use the runner-provided preflight and causal stage logs
as evidence; do not treat concretization alone as success. The generated
script must install the configured spec and verify its installed root prefix.
The runner preflight already performs Ruff, Python syntax, package audit, and
concretization. Do not invent exact dependency-version assertions unless the
user context explicitly requires them.
"""


def clean_summary(text: str, limit: int = 600) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return (" ".join(lines[-6:]).strip() or text)[:limit]


def summarize_log(log_text: str) -> str:
    lines = log_text.splitlines()
    parts = [f"(log: {len(lines)} lines total)"]
    verdict_at = next(
        (index for index, line in reversed(list(enumerate(lines))) if "=== VERDICT ===" in line),
        None,
    )
    if verdict_at is not None:
        parts.extend(["--- final verdict block ---", *lines[verdict_at:verdict_at + 25]])
    errors = [line for line in lines if _ERROR_LINE_RE.search(line)]
    if errors:
        parts.append(
            f"--- error lines ({len(errors)} total, first "
            f"{min(len(errors), _MAX_ERROR_LINES)} shown) ---"
        )
        parts.extend(errors[:_MAX_ERROR_LINES])
    parts.extend([f"--- last {min(_TAIL_LINES, len(lines))} lines ---", *lines[-_TAIL_LINES:]])
    return "\n".join(parts)


def _causal_log_path(log_path: Path, step: int) -> Path | None:
    candidate = log_path.parent / f"step_{step:02d}_first_failure.log"
    return candidate if candidate.is_file() else None


def _write_result_artifact(
    attempt: Attempt, *, succeeded: bool, signature: str | None, category: str | None
) -> None:
    result_path = Path(attempt.log_path).with_name(f"step_{attempt.step:02d}_result.json")
    payload = {
        "schema_version": 1,
        "step": attempt.step,
        "succeeded": succeeded,
        "failure_signature": signature,
        "failure_category": category,
        "causal_log_path": attempt.causal_log_path,
        "dag_identity": attempt.dag_identity,
    }
    result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    attempt.result_path = str(result_path)


def _ask(prompt: str, config: AgentConfig, session: Session | None = None):
    add_dirs = [config.state_dir, config.workspace.source_repository]
    if config.runner.backend == "host":
        if config.workspace.host_spack_executable is not None:
            add_dirs.append(config.workspace.host_spack_executable.parent)
    return ask_copilot(
        prompt,
        model=session.model if session else config.model,
        cwd=Path(session.repo_path) if session else config.workspace.writable_repository,
        resume=session.resume_id if session else None,
        timeout=config.agent_timeout_seconds,
        allow_tools=config.allow_tools,
        deny_tools=config.deny_tools,
        add_dirs=add_dirs,
        allow_all_paths=(
            config.allow_all_paths if config.runner.backend == "host" else False
        ),
        allow_all_urls=config.allow_all_urls,
    )


def session_matches_workspace(session: Session, config: AgentConfig) -> bool:
    """Reject resumes after the workspace topology changed."""
    if session.workspace_fingerprint == config.workspace_fingerprint:
        return True
    print(
        "[spack-agent] Workspace paths changed since this session started. "
        "Run without --resume to start fresh."
    )
    return False


def _workspace_context(config: AgentConfig) -> str:
    spack_context = f"""- HOST SPACK EXECUTABLE: {config.workspace.host_spack_executable}
    Use it only to inspect and validate; do not modify its installation."""
    if config.runner.backend == "podman":
        recipe_relative = config.workspace.recipe_file.relative_to(
            config.workspace.writable_repository
        )
        spack_context = f"""- VERIFICATION SPACK EXECUTABLE: /opt/spack/bin/spack
    Use this path in generated verification scripts. The host Spack
    installation is not available to the agent in container mode.

VERIFICATION CONTAINER (use these paths inside the generated script):
- SOURCE REPOSITORY: /workspace/source (read-only)
- WRITABLE REPOSITORY: /workspace/packages
- RECIPE DESTINATION: /workspace/packages/{recipe_relative}
- SPACK EXECUTABLE: /opt/spack/bin/spack
- SCRIPT STATE: /workspace/state
- SPACK STORE: /opt/spack-store (persistent across iterations)
- DOWNLOAD CACHE: /var/cache/spack (persistent across iterations)
The selected {config.runner.toolchain} toolchain is already registered in
Spack. The container itself is deleted after each iteration. Do not put host
paths in commands executed by the generated script or select a compiler in
the script. Translate any host Spack path written in the goal to
/opt/spack/bin/spack for verification.
"""
    return f"""SPACK WORKSPACE (path roles are strict):
- PROJECT BEING PACKAGED: {config.workspace.source_repository}
    Inspect this source repository to understand the software and its build.
    It is read-only: do not modify it.
- WRITABLE REPOSITORY: {config.workspace.writable_repository}
    Write the Spack recipe for the source project here. This is the only
    repository you may modify.
- SPACK PACKAGE REPOSITORY: {config.workspace.spack_repository_dir}
    This is the package repository containing the configured recipe.
- RECIPE DESTINATION: {config.workspace.recipe_file}
    {"Adapt this existing recipe." if config.workspace.recipe_file.is_file() else "No recipe exists yet; create it at this exact destination."}
    Keep task-specific changes there unless the goal requires otherwise.
{spack_context}"""


def _goal_context(config: AgentConfig) -> str:
    return f"""VERIFICATION SPEC:
{config.verification_spec}

USER CONTEXT:
{config.goal_context}

WORKFLOW CONTRACT:
{_GOAL_CONTRACT}"""


def start_goal(
    config: AgentConfig,
    store: SessionStore,
    force: bool = False,
    preflight_output: str = "",
    preflight_dag_identity: str | None = None,
) -> int:
    existing = store.load()
    if existing is not None and not existing.done and not force:
        print(f"[spack-agent] Session already in progress at step {existing.step}.")
        return 1
    if existing is not None and force:
        store.clear()

    session = Session(
        goal=config.goal_text,
        repo_path=str(config.workspace.writable_repository),
        model=config.model,
        workspace_fingerprint=config.workspace_fingerprint,
        step=1,
    )
    script_path, log_path = store.step_paths(session.step)
    Path(script_path).unlink(missing_ok=True)
    prompt = f"""You are a senior Spack packaging engineer.

{_workspace_context(config)}

{_goal_context(config)}

RUNNER PREFLIGHT:
{preflight_output or "(preflight was not run)"}

Plan the next change. Do not run long builds, installs, or test suites.
The runner executes those later with no timeout and returns a bounded real log.

Inspect the repository, make the edits that move toward the goal, and write
the long-running verification script.
{_SCRIPT_CONTRACT.format(script_path=script_path)}

Reply with a 2-4 sentence summary of the edits and what the script verifies.
"""
    print(f"[spack-agent] Starting agent for step {session.step}...")
    result = _ask(prompt, config)
    if result.error:
        print(f"[spack-agent] Copilot call failed: {result.error}")
        return 1
    print(result.text)
    print(f"[spack-agent] Usage: {result.usage_summary()}.")
    if not Path(script_path).is_file():
        print(f"[spack-agent] Agent did not create the required script: {script_path}")
        return 1
    session.resume_id = result.resume_id
    session.attempts.append(
        Attempt(
            step=1,
            script_path=script_path,
            log_path=log_path,
            summary=clean_summary(result.text),
            ai_credits=result.ai_credits_value or 0.0,
            dag_identity=preflight_dag_identity,
        )
    )
    store.save(session)
    Path(script_path).chmod(0o755)
    return 0


def revise_goal(
    config: AgentConfig,
    store: SessionStore,
    preflight_output: str = "",
    preflight_dag_identity: str | None = None,
) -> int:
    """Replan a session after the configured goal was edited."""
    session = store.load()
    if session is None or not session.attempts:
        print("[spack-agent] No active session to revise.")
        return 1
    if not session_matches_workspace(session, config):
        return 1

    new_goal = config.goal_text
    if session.goal == new_goal:
        return 0

    was_done = session.done
    last = session.attempts[-1]
    partial_log = Path(last.log_path)
    if partial_log.is_file():
        log_context = summarize_log(
            partial_log.read_text(encoding="utf-8", errors="replace")
        )
    else:
        log_context = "(no partial build log was found)"
    if not was_done:
        last.error_signature = "interrupted or superseded by revised goal"
        last.succeeded = False
    store.save(session)

    next_step = session.step + 1
    script_path, log_path = store.step_paths(next_step)
    Path(script_path).unlink(missing_ok=True)
    session_state = (
        "The previous goal was verified, and the user supplied follow-up work."
        if was_done
        else "The user interrupted the previous build and revised the goal."
    )
    prompt = f"""{session_state}

{_workspace_context(config)}

PREVIOUS REQUEST:
{session.goal}

{_goal_context(config)}

RUNNER PREFLIGHT:
{preflight_output or "(preflight was not run)"}

Previous attempts:
{session.history_digest()}

    Preserved output from the previous verification:
{log_context}

Re-evaluate the existing repository changes against the revised goal. Make
any necessary edits and write a new verification script. Do not rerun the old
script or run long commands yourself.
{_SCRIPT_CONTRACT.format(script_path=script_path)}

Reply with a 2-4 sentence summary of the revised plan and edits.
"""
    print(f"[spack-agent] Goal changed; replanning as step {next_step}...")
    result = _ask(prompt, config, session)
    if result.error:
        print(f"[spack-agent] Copilot call failed: {result.error}")
        return 1
    print(result.text)
    print(f"[spack-agent] Usage: {result.usage_summary()}.")
    if not Path(script_path).is_file():
        session.resume_id = result.resume_id or session.resume_id
        store.save(session)
        print(f"[spack-agent] Agent did not create the required script: {script_path}")
        return 1

    session.goal = new_goal
    session.step = next_step
    session.done = False
    session.resume_id = result.resume_id or session.resume_id
    session.attempts.append(
        Attempt(
            step=next_step,
            script_path=script_path,
            log_path=log_path,
            summary=clean_summary(result.text),
            ai_credits=result.ai_credits_value or 0.0,
            dag_identity=preflight_dag_identity,
        )
    )
    store.save(session)
    Path(script_path).chmod(0o755)
    return 0


def process_result(
    config: AgentConfig,
    store: SessionStore,
    log_file: str | None = None,
    preflight_output: str = "",
    preflight_dag_identity: str | None = None,
) -> int:
    session = store.load()
    if session is None or not session.attempts:
        print("[spack-agent] No active session.")
        return 1
    if session.done:
        print("[spack-agent] Session is already finished.")
        return 0
    if not session_matches_workspace(session, config):
        return 1
    if session.goal != config.goal_text:
        return revise_goal(config, store)
    log_path = Path(log_file or session.attempts[-1].log_path)
    if not log_path.is_file():
        print(f"[spack-agent] Log file not found: {log_path}")
        return 1
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    last = session.attempts[-1]
    last.log_path = str(log_path)
    causal_log = _causal_log_path(log_path, last.step)
    causal_text = (
        causal_log.read_text(encoding="utf-8", errors="replace")
        if causal_log is not None
        else log_text
    )
    succeeded, signature = evaluate_log(log_text)
    if not succeeded and causal_log is not None:
        signature = error_signature(causal_text)
        last.causal_log_path = str(causal_log)
    last.error_signature = signature
    last.failure_category = None if succeeded else failure_category(causal_text)
    last.dag_identity = preflight_dag_identity
    last.succeeded = succeeded
    _write_result_artifact(
        last,
        succeeded=succeeded,
        signature=signature,
        category=last.failure_category,
    )
    print(f"[spack-agent] Step {session.step}: {len(log_text.splitlines())} log lines")
    if succeeded:
        session.done = True
        store.save(session)
        print("[spack-agent] Goal verified. No follow-up agent was started.")
        return 0

    print(f"[spack-agent] Verification failed: {signature}")
    if session.is_stuck_on(signature):
        session.done = True
        store.save(session)
        print(f"[spack-agent] Stopping after {MAX_ATTEMPTS_PER_SIGNATURE} identical failures.")
        return 2
    store.save(session)

    next_step = session.step + 1
    script_path, next_log_path = store.step_paths(next_step)
    Path(script_path).unlink(missing_ok=True)
    prompt = f"""Continue this agent/build iteration.

{_workspace_context(config)}

{_goal_context(config)}

FAILURE CATEGORY: {last.failure_category}
CAUSAL STAGE LOG: {last.causal_log_path or '(not preserved by the script)'}

RUNNER PREFLIGHT:
{preflight_output or "(preflight was not run)"}

Previous attempts:
{session.history_digest()}

The last long-running verification failed. Here is the bounded first causal
failure output, not the aggregate install summary:
{summarize_log(causal_text)}

Diagnose the first causal failure, make the next edits, and write the next
verification script. Do not run the long command yourself.
{_SCRIPT_CONTRACT.format(script_path=script_path)}

Reply with a 2-4 sentence summary of the diagnosis and edits.
"""
    print(f"[spack-agent] Starting agent for failed step {session.step}...")
    result = _ask(prompt, config, session)
    if result.error:
        print(f"[spack-agent] Copilot call failed: {result.error}")
        return 1
    print(result.text)
    print(f"[spack-agent] Usage: {result.usage_summary()}.")
    if not Path(script_path).is_file():
        session.resume_id = result.resume_id or session.resume_id
        store.save(session)
        print(f"[spack-agent] Agent did not create the required script: {script_path}")
        return 1
    session.step = next_step
    session.resume_id = result.resume_id or session.resume_id
    session.attempts.append(
        Attempt(
            step=next_step,
            script_path=script_path,
            log_path=next_log_path,
            summary=clean_summary(result.text),
            ai_credits=result.ai_credits_value or 0.0,
        )
    )
    store.save(session)
    Path(script_path).chmod(0o755)
    return 0


def show_status(store: SessionStore) -> int:
    session = store.load()
    if session is None:
        print("[spack-agent] No session.")
        return 0
    print(f"Goal    : {session.goal}")
    print(f"Repo    : {session.repo_path}")
    print(f"Model   : {session.model or 'Copilot default'}")
    print(f"Step    : {session.step}")
    print(f"Done    : {session.done}")
    print(f"Credits : {session.total_credits():.2f}")
    print("\nAttempts:")
    print(session.history_digest(limit=50))
    return 0