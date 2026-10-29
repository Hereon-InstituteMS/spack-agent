# spack-agent

An installable, just-in-time coding agent for Spack work and other projects
whose real verification takes minutes or hours.

The Copilot agent runs only to inspect and edit the target repository and emit
a verification script. It exits before that script starts. The runner then
executes the script with no timeout and prints a heartbeat. A successful
machine-readable verdict, zero process exit status, and passing recipe checks
end the workflow without another model call; a
failed verdict starts Copilot again with a bounded excerpt of the real log.

## Requirements

- Linux or WSL with Python 3.11+, Bash, and Git
- The official GitHub Copilot CLI installed and authenticated
- A writable clone of the target project and `spack/spack-packages`
- For `runner.backend = "podman"`: Podman
- For `runner.backend = "host"`: Spack and Ruff available on `PATH`, plus
	the target project's required build tools

There are no third-party Python dependencies. The Snap Copilot CLI is rejected
because its AppArmor confinement prevents access to project and Spack paths.

## Install

From this repository:

On Linux or WSL, create and activate a virtual environment first. This avoids
installing into the OS-managed Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Then install the package:

```bash
python -m pip install -e .
spack-agent --help
```

You can also run the package without installing it:

```bash
python -m spack_agent --help
```

## Configure

Copy the tracked template to create your machine-specific local configuration:

```bash
cp spack-agent.example.toml spack-agent.toml
```

The local `spack-agent.toml` file is intentionally ignored by Git.
Its path roles are intentionally separate:

- `source_repository`: required project for which the recipe is being written;
	the agent inspects it but does not modify it.
- `writable_repository`: writable clone of
	`https://github.com/spack/spack-packages`, where the agent writes recipe
	changes. It is not the Spack installation itself.
- `spack_repository`: package-repository root inside the writable checkout.
	For a standard `spack/spack-packages` clone, leave this as
	`repos/spack_repo/builtin`.
- `recipe_path`: exact recipe destination relative to the Spack repository;
	the agent adapts an existing file or creates a missing one.
- `host_spack_executable`: Spack used only by the `host` runner; it is not
	modified. It may be omitted for the Podman runner, which uses image-local
	`/opt/spack/bin/spack`.

```toml
[workspace]
source_repository = "/path/to/project-being-packaged"
writable_repository = "/path/to/spack-packages"
spack_repository = "repos/spack_repo/builtin"
recipe_path = "packages/example/package.py"
host_spack_executable = "/path/to/spack/bin/spack" # Required only for backend = "host"

[goal]
spec = "example@1.0"
# Add only package-specific goals or review feedback here. spack-agent always
# supplies the general recipe-editing and final-verification requirements.
context = """
Describe the package-specific issue or requested change.
"""

[agent]
model = "gpt-5.6-sol" # optional
```

Paths may be absolute or relative to the configuration file. The loader
rejects missing paths and any `recipe_path` that escapes the writable repo.
Runtime state, generated scripts, and full logs are always stored in
`.spack-agent` beside the configuration file.
`goal.spec` is required. It is the exact Spack specification the runner audits,
concretizes, and requires the generated script to install. `goal.context` is
the user-controlled task-specific input, such as review feedback or package
constraints. The general workflow contract is built into spack-agent and is
not configured in TOML. The resulting DAG fingerprint is stored with the
attempt. A DAG (dependency graph) represents the fully resolved package
dependencies, so recipe edits that do not alter concretization are visible in
the session record.
Inspect the fully resolved topology before running an agent:

```bash
spack-agent config
```

See `spack-agent.example.toml` for the complete portable configuration.

`spack-agent` is designed for one active project configuration at a time. It
uses the single adjacent `.spack-agent` directory for temporary session state,
generated scripts, and logs. Sequential commands such as `run --resume`,
`status`, and `result` operate on that same session; switching between projects
and retaining multiple resumable sessions is not supported.

## Run

```bash
spack-agent run
spack-agent run --resume
```

`spack-agent run` always starts a fresh session and deletes prior artifacts in
`.spack-agent` before planning. Use `--resume` explicitly to continue an
unfinished session and retain its scripts, logs, causal stage logs, and result
artifacts. To reopen a completed session for external review follow-up, edit
`[goal].context` and use `spack-agent run --resume`; the prior successful
verification stays in the session history. Resuming a completed session with
an unchanged goal is rejected. Workspace path changes are rejected because
they could send a resumed agent to the wrong repositories. Only one command
may use a session at a time.

For Podman runs, a missing selected toolchain image is built automatically
before the agent starts. The first Intel oneAPI run downloads the large base
image and can take several minutes; build progress is streamed to the terminal.

The compile/install/test process has no timeout. `[runner].poll_seconds`
controls the heartbeat interval, while `[runner].max_iterations` limits agent
planning/build iterations per `run` invocation, including a resumed follow-up.
Ctrl+C
terminates the complete build process group so a restart cannot leave or
duplicate a Spack/compiler subprocess. Session JSON, the generated script,
attempt history, Copilot resume ID, and the partial log are preserved.

After an interruption, you may edit `[goal].context` and run:

```bash
spack-agent run --resume
```

The agent receives the previous request, current request, and preserved partial
log, then writes a new verification script. The interrupted script is not rerun.

`status` displays the current session. To process a completed log from an
unfinished or interrupted session, use `result`:

```bash
spack-agent result /path/to/completed-build.log
spack-agent status
spack-agent reset
```

`result` does not reprocess a session that is already marked complete. It imports
the verdict from the supplied log; it cannot recover the original process exit
status or rerun the recipe checks. Use `run` for runner-observed verification.

Generated scripts use paths for the configured backend. Do not run a
Podman-mode script directly with host Bash; use `spack-agent run --resume` so
the configured mounts, image, and persistent volumes are applied.

Every generated script must finish with:

```text
=== VERDICT ===
exit_code=0
goal_met=yes
```

Both success values are required. During `run`, the actual script process must
also exit zero, and post-build Ruff, Python syntax, Spack package audit, and
concretization checks must all pass. A success verdict cannot override these
checks. The same checks run before planning to provide diagnostic context, even
when the recipe is initially missing or invalid. Missing or incomplete verdicts
are treated as failures, and repeated identical failures stop the loop after
two attempts.
The runner provides each planning phase with the immutable verification spec
and workflow contract; user context cannot weaken the required final install
and validation behavior.
Large logs remain on disk; only the final verdict, selected error lines, and
the last 80 lines are sent to Copilot. When a generated script preserves a
first causal stage log, the runner classifies that log rather than Spack's
generic aggregate failure footer. It writes `step_NN_result.json` with the
failure category, signature, causal-log path, and DAG fingerprint.

### Missing prerequisites

If a required tool or other prerequisite is missing or incompatible,
`spack-agent` stops and tells you what needs attention. Check the details with
`spack-agent status`, fix the prerequisite in your environment, then resume:

```bash
spack-agent run --resume
```

You do not need to change your goal. `spack-agent` does not install system
software or change your Spack configuration. Dependencies that Spack can
build are handled as usual.

## Container runner

On the first `spack-agent run` with `runner.backend = "podman"`, the runner
automatically builds the selected toolchain image when it is not already
available. GCC and Clang profiles use the parameterized `Containerfile.gcc`;
`Containerfile.oneapi` builds the standalone Intel oneAPI image.

`runner.spack_ref` selects the Spack checkout (default: `v1.2.2`). Use a release
tag or commit SHA for repeatability; a moving branch name does not pin its
contents. Image and install-volume names use only the profile, for example
`spack-agent-builder:ubuntu24.04-gcc13` and
`spack-agent-store-ubuntu24.04-gcc13`. Switching profiles isolates installed
packages. Changing the profile or Spack reference rejects resuming the previous
environment. Changing only the Spack reference leaves image and store names
unchanged. Images are built only when
missing: changing `spack_ref` does not update an existing image. To apply a new
reference, remove the selected image (shown by `spack-agent config`) and start
a new run; the runner rebuilds it. The install store remains shared across
references unless deliberately removed.
All builder images include Ruff 0.16.0 for recipe validation.

All runner settings are optional. Add only the overrides needed for a given
workspace:

```toml
[runner]
backend = "podman"
poll_seconds = 10
max_iterations = 5
toolchain = "ubuntu24.04-gcc13"
spack_ref = "v1.2.2"
cpus = 12
memory = "32g"
# Optional: disable network access after required sources are cached.
# network = "none"
# Optional named Podman volume that retains downloaded source archives.
download_volume = "spack-agent-downloads"
```

Set `runner.backend = "podman"` to use the container runner, or `"host"` to
run verification directly on the host. Every container verification iteration
uses `podman run --rm`, so its filesystem and build processes are discarded
afterward. Podman creates the configured named volumes automatically:
downloads persist at `/var/cache/spack`, and installed packages persist at
`/opt/spack-store`. Neither volume is stored in the Git checkout. This is the
fast development path: a new session or container does not force dependencies
to rebuild. The runner does not run an automatic matrix or package-specific
runtime checks.
The configured spec controls package verification.

The source repository is mounted read-only at `/workspace/source`; the recipe
checkout and agent state are mounted at `/workspace/packages` and
`/workspace/state`. The image registers the configured Spack package
repository before running each script. Network access uses Podman's default
unless `runner.network` is set, for example to `"none"` after sources have
been cached.

### Offline verification

To prove an install can build without network access:

1. With normal network access, run the exact specification once. This populates
	the persistent `download_volume` with Spack-managed source archives.
2. Run `spack-agent config` to find the selected install volume. Remove that
	volume, while retaining the download volume. For the default GCC profile:

	```bash
	podman volume rm spack-agent-store-ubuntu24.04-gcc13
	```

3. Set `[runner].network = "none"` in `spack-agent.toml` and start a new
	`spack-agent run`.

Do not use `--resume`: the changed runner setting is intentionally rejected for
a resumed session. A fresh run clears only the adjacent `.spack-agent` session
state; the named download volume remains available. This covers Spack-managed
source archives only: the recipe and its build system must also avoid
downloading dependencies during the build.

In Podman mode, Copilot receives only the writable repository, source
repository, and session-state paths. Host-wide path access and the host Spack
directory are not granted; verification scripts use `/opt/spack/bin/spack`.

Select one environment at a time using `runner.toolchain`:

| Profile | Base | Registered compilers |
| --- | --- | --- |
| `ubuntu24.04-gcc13` (default) | Ubuntu 24.04 | GCC/G++/GFortran 13 |
| `ubuntu26.04-gcc15` | Ubuntu 26.04 | GCC/G++/GFortran 15 |
| `clang` | Ubuntu 24.04 | Clang 18 C/C++, GCC 13 for Fortran/runtime support |
| `oneapi` | Intel oneAPI 2025.0.1 on Ubuntu 24.04 | `icx`, `icpx`, `ifx` |

Discovery uses only configured executable symlinks, not the full `PATH` or
external-package discovery. The Clang compiler external does not establish that
system LLVM development libraries satisfy package requirements. Match the root
compiler constraint in `goal.spec` to the profile, for example `%gcc@15`,
`%clang@18`, or `%oneapi`.
The download volume remains configurable and shared by default; installed
packages are isolated by profile. `spack_ref` applies only
to Podman builds; the host backend uses the configured host Spack executable.

Inspect or remove external storage with:

```bash
# Show disk space used by Podman images, containers, volumes, and build cache.
podman system df
# Remove installed GCC packages but keep downloaded sources for an offline rebuild.
podman volume rm spack-agent-store-ubuntu24.04-gcc13
# Remove the shared source-download cache when it is no longer needed.
podman volume rm spack-agent-downloads
```

## Test

```bash
python -m unittest discover -s tests -v
```