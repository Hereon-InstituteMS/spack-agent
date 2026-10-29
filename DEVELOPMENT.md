# Developer guide

This document describes the runner and verification-script contract for
contributors. User setup and operation instructions are in the [README](README.md).

## Verification lifecycle

The agent plans and edits the writable package repository, then writes a
self-contained Bash verification script. The runner executes that script
separately, without a timeout. It runs recipe preflight checks before planning
and after verification: Ruff, Python syntax compilation, Spack package audit,
and concretization of the configured verification spec.

Each planning phase receives the configured verification spec and immutable
workflow contract; user context cannot weaken the required install and
validation behavior. Preflight output is also supplied as diagnostic context.
Preflight runs even when the recipe is missing or invalid; ordinary recipe
check failures do not prevent planning, while recognized environment blockers
stop the workflow.
A successful run requires all of the following:

- The script exits with status zero.
- Its final verdict contains `exit_code=0` and `goal_met=yes`.
- Post-verification preflight checks pass.

The verdict alone cannot override a nonzero script exit or failed preflight.
Missing or incomplete verdicts are failures. After two attempts with the same
failure signature, the runner stops rather than starting another agent call.
Once verification succeeds, no follow-up agent call is made.
`spack-agent result` can import a verdict from a log, but cannot recover the
original process status or rerun these checks; use `spack-agent run` for
runner-observed verification.

## Verification-script contract

Generated scripts must be self-contained, non-interactive Bash scripts. They
must preserve command failures, install the exact configured spec, and
programmatically verify the requested goal and installed root prefix. They
must not modify the source checkout, unrelated packages, or Spack
configuration, and must not run `spack compiler find`.

On success, the final block is:

```text
=== VERDICT ===
exit_code=0
goal_met=yes
```

Scripts may include additional evidence lines. `goal_met=yes` is valid only
after programmatic verification of the full goal.

## Prerequisite blockers

When verification cannot proceed without user intervention, the script must
check the prerequisite dynamically and finish with a blocker verdict instead
of attempting a workaround:

```text
=== VERDICT ===
exit_code=1
goal_met=no
blocked=yes
blocker_reason=<specific missing or incompatible prerequisite>
blocker_action=<specific user action required>
blocker_evidence=<failed check and observed result>
```

The values must be specific, nonempty, and single-line, and the script must
exit nonzero. The runner reads a blocker only from the final verdict block.
A blocker takes precedence over a success-shaped verdict, stops the repair
loop, and returns exit code 3. Malformed explicit blocker verdicts are also
treated as blockers.

Preflight additionally recognizes missing required tools, explicit
no-compiler diagnostics, and Spack errors for dependencies configured as
external-only with no compatible external installation. Ordinary
Spack-buildable dependencies and repairable recipe/build failures are not
blockers. For a blocked session, `spack-agent run --resume` rechecks preflight
and, if a verification script had already been created, reruns its dynamic
checks; if initial preflight blocked before planning, it starts planning after
preflight succeeds. No goal edit is required.

## Failure handling and artifacts

Ordinary verification failures may start another agent iteration with a
bounded excerpt of the failure log. Full logs remain in the session state
directory; the agent receives the final verdict, selected error lines (up to
40), and the last 80 log lines. When a script preserves a causal stage log, the
runner classifies that log instead of relying on Spack's aggregate failure
summary.

Each verification attempt writes a `step_NN_result.json` artifact containing
the outcome, failure category and signature when applicable, causal-log path,
and concretized DAG identity. Blocker artifacts also include the reason,
evidence, and required action. These details are implementation diagnostics;
users should use `spack-agent status` for the actionable blocker summary.

## Local checks

Run the same lint and unit-test checks used in CI:

```bash
ruff check .
python -m unittest discover -s tests -v
```
