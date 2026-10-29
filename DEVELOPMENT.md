# spack-agent internals

This guide is a self-contained overview of the architecture, workflow, and
implementation choices in spack-agent. It is intended for contributors and
for explaining the system to a technical audience. User setup and operation
instructions are in the [README](README.md).

## What spack-agent does

spack-agent coordinates a coding agent with a real, potentially long-running
Spack build:

1. The Copilot CLI inspects the project and package repository, edits the
   package recipe, and writes a verification script.
2. The CLI process exits. spack-agent runs the script separately.
3. The runner checks the actual process result, the script's verdict, and
   recipe validation. On a repairable failure, it sends a bounded summary to
   Copilot for another edit-and-verify iteration.
4. A successful verification ends the session without a follow-up agent call.

The split is deliberate: the model does not own the long build process. The
runner executes it, observes its exit status, retains its output, and applies
the final success gates.

## Architecture map

| Component | Responsibility |
| --- | --- |
| `spack_agent/cli.py` | Parses commands, loads configuration, serializes access to a session, and dispatches operations. |
| `spack_agent/config.py` | Loads and validates TOML, resolves workspace paths, derives the session identity, and renders `spack-agent config`. |
| `spack_agent/copilot.py` | Calls the Copilot CLI, passes prompt text over stdin, and extracts resume IDs and usage data. |
| `spack_agent/runner.py` | Runs preflight, builds/starts Podman containers, streams verification output, handles interruption, and controls the run loop. |
| `spack_agent/workflow.py` | Builds prompts, starts or revises plans, evaluates results, manages retries and blockers, and displays session status. |
| `spack_agent/session.py` | Parses verdicts and failure signatures and persists sessions, attempts, and locks. |
| `container/` | Defines reproducible Spack/compiler images and the container entrypoint. |
| `tests/` | Covers configuration, session safety, prompts/workflow behavior, runners, and prerequisite handling. |
| `.github/workflows/tests.yml` | Runs Ruff and the unit tests in CI. |

## Main execution flow

`spack-agent run` starts a fresh session; `spack-agent run --resume` continues
an existing one.

### Start or resume

- The CLI loads and validates the selected TOML file, then obtains the
  configuration's non-blocking session lock.
- For Podman, the runner checks that Podman exists and builds the selected
  image if it is missing.
- A fresh run removes old contents of `.spack-agent` except the held lock,
  performs preflight, and asks Copilot to plan.
- A resume checks that the saved workspace identity still matches. If the goal
  changed, the agent receives the previous and current request plus a bounded
  excerpt of the prior log and writes a new script. If the session is blocked,
  prerequisites are rechecked first.
- The agent must create the script at the exact path given in its prompt.
  spack-agent records the attempt and Copilot resume ID, then makes the script
  executable.

### Iterate

For each configured iteration, the runner executes the current script and
streams combined stdout/stderr to both the terminal and that step's log. It
then runs post-build preflight and evaluates the outcome:

- **Success:** the process exits zero, the final verdict says
  `exit_code=0` and `goal_met=yes`, and post-build preflight succeeds. The
  session is marked done.
- **Repairable failure:** the runner records a signature and category, saves
  the result artifact, and asks Copilot to diagnose the failure and create
  the next script.
- **Prerequisite blocker:** the runner records the blocker, skips the repair
  agent, and exits with code 3.
- **Repeated failure or iteration cap:** the workflow stops rather than
  continuing indefinitely. Two matching failure signatures stop retries;
  `runner.max_iterations` also limits work per invocation.

The configured iteration cap applies to each `run` invocation, including a
resume. A later resume can continue a session that reached the per-invocation
cap.

## Workspace and agent boundaries

The three workspace paths have different roles:

- **Source repository:** inspected to understand the software; never modified.
- **Writable repository:** the only repository the agent may edit.
- **Spack repository and recipe path:** identify the package tree and exact
  recipe destination inside the writable checkout. The recipe may be created
  if it does not exist.

Configuration paths may be absolute or relative to the TOML file. The loader
resolves them and rejects a Spack repository or recipe path that escapes its
parent checkout, including through resolved symlinks.

The prompt contract reinforces these boundaries and supplies the exact
verification spec. User context adds task-specific requirements; it cannot
replace the general install and validation contract. The agent is told not to
run long builds itself, alter Spack configuration, run
`spack compiler find`, or modify the source checkout or unrelated packages.

Copilot tool permissions are configurable. Defaults allow writing and shell
use while denying selected destructive Git and `rm` command patterns.
The host backend grants the agent the source and state directories and the
configured host Spack directory as additional paths. In Podman mode, the agent
still runs as the host Copilot CLI, but does not receive host-wide path access
or the host Spack directory. It edits the host-side writable checkout and
state directory; generated scripts use the corresponding container mount
paths when verification runs. `allow_all_paths` is intentionally ignored for
the Podman backend. The Snap packaged Copilot CLI is rejected because its
confinement prevents access to project and Spack paths.

## Configuration and session identity

`AgentConfig` combines workspace paths, the required Spack spec, user context,
Copilot options, and runner settings. The TOML loader rejects unknown keys,
invalid types, missing required values, and invalid paths early.

The session stores a SHA-256 workspace fingerprint. It covers the resolved
source, writable repository, package repository, recipe and host Spack paths,
plus relevant runner identity: backend, toolchain/image, Podman Spack
reference, network setting, and download-volume name. A mismatch prevents
resume to avoid applying a saved session to a different workspace or build
environment. Goal changes are handled separately as a request to replan.

Other state includes the current step, Copilot resume ID, model, attempt
history, completion state, optional prerequisite blocker, and usage totals.
Session JSON is written to a temporary file and atomically replaced, reducing
the chance of a partially written state file.

Only one command may operate on a configuration's `.spack-agent` state at a
time. `SessionStore.lock()` uses a non-blocking file lock. Consequently,
`status` cannot inspect the session during an active `run`; it reports that
another command owns the lock rather than interrupting the run.

`run` without `--resume` starts over and removes old session artifacts.
`reset` removes session JSON but retains logs and other artifacts.
`status` is read-only. `result <log>` imports a log into an unfinished session:
it can inform a repair iteration, but has no original process exit status and
does not rerun the build or the runner's preflight checks.

## Preflight and success gates

Preflight runs before planning and after each verification script. It checks:

1. Ruff against the configured recipe.
2. Python syntax compilation of the recipe.
3. Spack package audit.
4. Concretization of the exact configured verification spec.

Preflight output is capped at the last 12,000 characters and provided to the
agent as context, including when the recipe is not yet present or has ordinary
lint, syntax, audit, or concretization errors. Those repairable errors do not
prevent initial planning. Recognized environment blockers do stop planning.

The concretized DAG output is hashed and stored as an attempt's DAG identity.
This gives the session a compact identity for the resolved dependency graph;
it can show whether a recipe change altered concretization without storing the
whole DAG in the session summary.

The final verdict is parsed from the last `=== VERDICT ===` block. A missing
or incomplete success verdict is a failure. A success-shaped verdict cannot
override a nonzero script process or a failed post-build check.

## Verification-script contract

The agent produces a self-contained, non-interactive Bash script at the exact
state-directory path supplied in its prompt. The script must:

- preserve command failures and avoid interactive prompts (stdin is
  `/dev/null`);
- install the exact configured spec, not a separately invented target;
- verify the requested goal programmatically, including the installed root
  prefix; and
- finish successful work with a machine-readable verdict such as:

  ```text
  === VERDICT ===
  exit_code=0
  goal_met=yes
  ```

`goal_met=yes` is meaningful only if the script actually checked the full
goal. The runner separately checks process exit and preflight, so printing a
success verdict is not enough on its own.

## Prerequisite blockers

Some failures require user intervention rather than another recipe edit. The
script must check essential prerequisites dynamically before starting
expensive work. Examples include a required compiler executable, an
external-only package with no usable installation, or a goal-specific external
tool that is unavailable. Ordinary Spack-buildable dependencies and
repairable recipe/build failures are not blockers.

The script reports a blocker in its final verdict and exits nonzero:

```text
=== VERDICT ===
exit_code=1
goal_met=no
blocked=yes
blocker_reason=<specific missing or incompatible prerequisite>
blocker_action=<specific user action required>
blocker_evidence=<failed check and observed result>
```

Blocker values must be nonempty and single-line. The parser reads the final
verdict block only; malformed explicit blocker verdicts are also treated as
blockers. A valid blocker takes precedence over a success-shaped verdict.
Preflight additionally recognizes missing required tools, explicit
no-compiler diagnostics, and specific Spack diagnostics for external-only
dependencies with no compatible installation.

The runner saves reason, evidence, and action in session state and in the
step's result artifact when one exists. It returns exit code 3 and does not
spend another repair iteration. On resume, it rechecks preflight and reruns
the blocked script's dynamic checks; if preflight originally blocked before
planning, it starts planning once preflight passes. No goal edit is required.

## Log handling and failure repair

Full step logs stay on disk. For an agent follow-up, the runner sends a bounded
summary rather than the whole build log:

- the final verdict block, when present;
- up to 40 lines matching known error patterns; and
- the last 80 log lines.

When a generated script preserves a first causal stage log at
`step_NN_first_failure.log`, the runner uses that log to classify and explain
the failure instead of Spack's often-generic aggregate install footer.

Failure classification distinguishes concretization, patch, configuration,
link, compile, preflight, and generic verification failures. Failure signatures
are selected from common compiler/build diagnostics, normalized to reduce
noise from temporary paths, hashes, source positions, and whitespace, then
truncated. This lets the retry guard recognize substantially identical
failures across attempts without treating every different CMake error as the
same failure.

Each attempt has a script and log named `step_NN.sh` and `step_NN.log`. The
runner also writes `step_NN_result.json` with the schema version, outcome,
failure signature/category, causal-log path, and DAG identity. Blocker results
include reason, evidence, and action. The session JSON contains the attempt
history, concise summaries, Copilot resume ID, blockers, and accumulated
usage data.

## Host and Podman runners

The host backend executes each script with Bash on the host. It uses the
configured host Spack executable and the host's compiler/build environment.
Host mode is direct and convenient, but does not isolate package builds from
the host.

The Podman backend uses a reproducible image for one selected toolchain.
Missing images are built on demand from `container/Containerfile.gcc` or
`container/Containerfile.oneapi`, using the configured Spack Git reference.
GCC and Clang profiles share the parameterized GCC Containerfile; oneAPI has
its own image. Images are built only when missing, so changing `spack_ref`
does not update an existing image automatically.

Each verification iteration starts `podman run --rm`. The container itself
and its writable layer are temporary, while named volumes persist:

| Container path | Host/Podman resource | Purpose |
| --- | --- | --- |
| `/workspace/source` | Source checkout, read-only bind mount | Project source inspection/build input |
| `/workspace/packages` | Writable package checkout, read-write bind mount | Recipe edits and package repository |
| `/workspace/state` | `.spack-agent`, read-write bind mount | Scripts, logs, and session artifacts |
| `/var/cache/spack` | Configured download volume | Reusable source archives and Spack caches |
| `/opt/spack-store` | Toolchain-specific install volume | Reusable installed dependencies |

The container entrypoint restores oneAPI environment variables when
applicable, validates the configured package-repository mount, registers that
repository in the container's user Spack scope, and then executes the
requested command.

The selected image registers its compiler during image construction. Compiler
discovery deliberately uses the configured executable symlinks, not a broad
`PATH` search or automatic external-package discovery. This makes the
toolchain available to Spack without silently treating arbitrary host
software as an external package.

Network access defaults to Podman's normal network. Setting `runner.network`
can restrict it, for example to validate a build after required sources have
been cached. Download caches are shared by default; install stores are
separate by toolchain profile. The container is transient, but named volumes
are not removed when a run ends.

## Process control and safety details

Verification scripts run with stdin disconnected and stdout/stderr piped
through `tee`; `pipefail` preserves the script/container exit status through
that logging pipeline. The parent polls at `runner.poll_seconds` and prints
elapsed-time heartbeats. There is intentionally no build timeout.

The build is started in a new process session. On Ctrl+C, the runner sends
SIGTERM to the complete process group, waits up to ten seconds, then sends
SIGKILL if needed. This prevents a Spack or compiler descendant from being
left behind and accidentally duplicated by a resume.

Podman infrastructure exit codes are handled separately from package failures:
when the container runtime fails before verification completes, the runner
does not send that failure to Copilot as if it were a recipe error.

## Copilot CLI integration

`spack_agent/copilot.py` is a narrow subprocess adapter. It constructs the
Copilot CLI arguments for model, tool permissions, accessible directories,
optional resume ID, and timeout. Prompt text is sent on stdin rather than
passed as a command-line argument, avoiding argument-size limits and keeping
the prompt out of the process argument list.

The default Copilot timeout is 900 seconds and is separate from the
unbounded build duration. The adapter returns explicit errors for a missing
CLI, timeout, or nonzero Copilot exit. It parses resume IDs and usage data from
CLI stderr when present; usage statistics are informational and may be
unavailable.

## Local development and tests

The project targets Python 3.11 or newer and has no runtime third-party Python
dependencies. Ruff is installed separately for CI and local linting.

Run the same checks as CI:

```bash
ruff check .
python -m unittest discover -s tests -v
```

Tests use isolated temporary workspaces and mocks for external services and
runtime commands. The suite covers configuration/path validation, CLI and
session locking, verdict and blocker parsing, retries and resume behavior,
host and Podman command construction, interruption, and result artifacts.
