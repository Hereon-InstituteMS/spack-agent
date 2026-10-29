import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from spack_agent import cli, runner, workflow
from spack_agent.config import AgentConfig, RunnerConfig, WorkspaceConfig, load_config
from spack_agent.copilot import CopilotResult
from spack_agent.session import (
    Attempt,
    Blocker,
    Session,
    SessionLockedError,
    SessionStore,
    evaluate_log,
    preflight_blocker,
    prerequisite_blocker,
)


def make_config(root: Path, task: str = "test") -> AgentConfig:
    source = root / "source"
    source.mkdir(exist_ok=True)
    writable = root / "checkout"
    (writable / "spack-repo").mkdir(parents=True, exist_ok=True)
    spack = root / "spack" / "bin" / "spack"
    spack.parent.mkdir(parents=True, exist_ok=True)
    spack.touch()
    return AgentConfig(
        workspace=WorkspaceConfig(
            source_repository=source,
            writable_repository=writable,
            spack_repository=Path("spack-repo"),
            recipe_path=Path("packages/example/package.py"),
            host_spack_executable=spack,
        ),
        goal_context=task,
        verification_spec="example@1.0",
        _config_directory=root,
    )


BLOCKED_LOG = """=== VERDICT ===
exit_code=1
goal_met=no
blocked=yes
blocker_reason=Required external CUDA is unavailable
blocker_action=Register a compatible external CUDA toolkit, then resume.
blocker_evidence=No compatible external CUDA was found in the concrete spec.
"""


class PrerequisiteTests(unittest.TestCase):
    def test_only_final_explicit_verdict_blocks(self):
        self.assertIsNone(prerequisite_blocker("compiler missing\nblocked=yes\n"))
        self.assertIsNone(prerequisite_blocker(
            BLOCKED_LOG + "=== VERDICT ===\nexit_code=0\ngoal_met=yes\n"
        ))
        self.assertIsNone(prerequisite_blocker(
            "=== VERDICT ===\nexit_code=1\ngoal_met=no\nerror=missing header\n"
        ))
        blocker = prerequisite_blocker(BLOCKED_LOG)
        self.assertEqual(blocker.reason, "Required external CUDA is unavailable")
        self.assertIn("Register", blocker.action)

    def test_malformed_blocker_stops_with_explicit_diagnostic(self):
        blocker = prerequisite_blocker("=== VERDICT ===\nblocked=yes\n")
        self.assertIn("Malformed", blocker.reason)
        self.assertIn("blocker_action", blocker.action)

    def test_explicit_environment_diagnostics_are_not_general_build_errors(self):
        for text in (
            "==> Error: No compilers found",
            "==> Error: No compilers available",
            "==> Error: No compilers match spec gcc@15",
            (
                "Cannot build cuda, since it is configured `buildable:false` and "
                "no externals satisfy the request"
            ),
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(preflight_blocker(text))
        for text in (
            "failed to concretize: package conflict",
            "fatal error: missing.h: No such file or directory",
            "CMake Error: The compiler identification is unknown",
            "Cannot build cuda from source",
        ):
            with self.subTest(text=text):
                self.assertIsNone(preflight_blocker(text))
        self.assertFalse(evaluate_log(
            BLOCKED_LOG + "exit_code=0\ngoal_met=yes\n"
        )[0])

    def test_blocker_overrides_success_and_does_not_start_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            store = SessionStore(config.state_dir)
            script, log = store.step_paths(1)
            Path(log).write_text(
                BLOCKED_LOG + "exit_code=0\ngoal_met=yes\n", encoding="utf-8"
            )
            store.save(Session(
                goal=config.goal_text,
                workspace_fingerprint=config.workspace_fingerprint,
                step=1,
                attempts=[Attempt(step=1, script_path=script, log_path=log)],
            ))
            with patch.object(workflow, "_ask") as ask:
                self.assertEqual(workflow.process_result(config, store), 3)
            ask.assert_not_called()
            session = store.load()
            self.assertFalse(session.done)
            self.assertIsInstance(session.blocker, Blocker)
            self.assertEqual(session.attempts[0].failure_category, "blocked")
            result = Path(session.attempts[0].result_path).read_text(encoding="utf-8")
            self.assertIn('"action": "Register', result)
            with patch("builtins.print") as output:
                workflow.show_status(store)
            self.assertIn("Register a compatible", str(output.call_args_list))

    def test_initial_preflight_block_is_saved_without_planning(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            with (
                patch.object(runner, "run_preflight", return_value=runner.PreflightResult(
                    BLOCKED_LOG, False, None,
                )),
                patch.object(runner, "start_goal") as start,
            ):
                self.assertEqual(runner.run_workflow(config, resume=False), 3)
            start.assert_not_called()
            session = SessionStore(config.state_dir).load()
            self.assertIsNotNone(session.blocker)
            self.assertFalse(session.attempts)

    def test_preflight_block_overrides_success_without_followup(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            store = SessionStore(config.state_dir)
            script, log = store.step_paths(1)
            Path(log).write_text(
                "=== VERDICT ===\nexit_code=0\ngoal_met=yes\n", encoding="utf-8",
            )
            store.save(Session(
                goal=config.goal_text,
                workspace_fingerprint=config.workspace_fingerprint,
                step=1,
                attempts=[Attempt(step=1, script_path=script, log_path=log)],
            ))
            with patch.object(workflow, "_ask") as ask:
                self.assertEqual(workflow.process_result(
                    config, store, preflight_succeeded=False,
                    preflight_output="==> Error: No compilers available",
                ), 3)
            ask.assert_not_called()
            self.assertIn("compilers", store.load().blocker.reason)
            self.assertFalse(store.load().attempts[-1].succeeded)

    def test_revised_blocked_goal_replans_and_preserves_blocker_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            store = SessionStore(config.state_dir)
            script, log = store.step_paths(1)
            Path(log).write_text(BLOCKED_LOG, encoding="utf-8")
            blocker = prerequisite_blocker(BLOCKED_LOG)
            store.save(Session(
                goal="previous goal",
                workspace_fingerprint=config.workspace_fingerprint,
                step=1,
                blocker=blocker,
                attempts=[Attempt(
                    step=1, script_path=script, log_path=log,
                    failure_category="blocked", error_signature=blocker.reason,
                )],
            ))

            def replan(*_args):
                next_script, _ = store.step_paths(2)
                Path(next_script).write_text("new checks", encoding="utf-8")
                return CopilotResult(text="Revised prerequisite checks")

            with (
                patch.object(runner, "run_preflight", return_value=runner.PreflightResult(
                    "", True, None,
                )),
                patch.object(workflow, "_ask", side_effect=replan) as ask,
                patch.object(runner, "run_step_script", return_value=130) as run_script,
            ):
                self.assertEqual(runner.run_workflow(config, resume=True), 130)
            ask.assert_called_once()
            self.assertEqual(run_script.call_args.args[1], store.step_paths(2)[0])
            self.assertIsNone(store.load().blocker)
            self.assertEqual(store.load().attempts[0].error_signature, blocker.reason)

    def test_blocked_resume_rechecks_without_goal_edit_and_can_succeed(self):
        for resolved in (False, True):
            with self.subTest(resolved=resolved), tempfile.TemporaryDirectory() as temporary:
                config = make_config(Path(temporary))
                store = SessionStore(config.state_dir)
                script, log = store.step_paths(1)
                Path(script).write_text("dynamic prerequisite checks", encoding="utf-8")
                store.save(Session(
                    goal=config.goal_text,
                    workspace_fingerprint=config.workspace_fingerprint,
                    step=1,
                    blocker=prerequisite_blocker(BLOCKED_LOG),
                    attempts=[Attempt(step=1, script_path=script, log_path=log)],
                ))

                def verify(*_args, log_path=log, fixed=resolved):
                    Path(log_path).write_text(
                        "=== VERDICT ===\nexit_code=0\ngoal_met=yes\n" if fixed
                        else BLOCKED_LOG, encoding="utf-8",
                    )
                    return 0 if fixed else 1

                with (
                    patch.object(runner, "run_preflight", return_value=runner.PreflightResult(
                        "", True, None,
                    )) as preflight,
                    patch.object(runner, "run_step_script", side_effect=verify),
                    patch.object(workflow, "_ask") as ask,
                ):
                    self.assertEqual(
                        runner.run_workflow(config, resume=True), 0 if resolved else 3,
                    )
                ask.assert_not_called()
                self.assertEqual(preflight.call_count, 2 if resolved else 1)
                session = store.load()
                self.assertEqual(session.done, resolved)
                self.assertEqual(session.blocker is None, resolved)
                self.assertEqual(len(session.attempts), 1)

    def test_resume_rechecks_initial_block_before_first_planning(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            store = SessionStore(config.state_dir)
            store.save(Session(
                goal=config.goal_text,
                workspace_fingerprint=config.workspace_fingerprint,
                blocker=prerequisite_blocker(BLOCKED_LOG),
            ))
            for output, expected_calls in ((BLOCKED_LOG, 0), ("", 1)):
                with (
                    patch.object(runner, "run_preflight", return_value=runner.PreflightResult(
                        output, not output, None,
                    )),
                    patch.object(runner, "start_goal", return_value=1) as start,
                ):
                    self.assertEqual(
                        runner.run_workflow(config, resume=True), 3 if output else 1,
                    )
                self.assertEqual(start.call_count, expected_calls)

    def test_real_script_stops_for_missing_compiler_and_resumes_after_restoration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_config(root)
            config = AgentConfig(
                workspace=base.workspace,
                goal_context=base.goal_context,
                verification_spec=base.verification_spec,
                runner=RunnerConfig(poll_seconds=0.01),
                _config_directory=root,
            )
            compiler = root / "required-compiler"
            store = SessionStore(config.state_dir)
            script, log = store.step_paths(1)
            blocked = (
                "=== VERDICT ===\nexit_code=1\ngoal_met=no\nblocked=yes\n"
                "blocker_reason=Required compiler executable is missing\n"
                f"blocker_action=Restore {compiler}, then resume.\n"
                f"blocker_evidence=Executable check failed: {compiler}\n"
            )
            Path(script).write_text(
                f"if [[ ! -x '{compiler}' ]]; then\n"
                f"cat <<'EOF'\n{blocked}EOF\nexit 1\nfi\n"
                f"'{compiler}' --version || exit 1\n"
                "printf '=== VERDICT ===\\nexit_code=0\\ngoal_met=yes\\n'\n",
                encoding="utf-8",
            )
            store.save(Session(
                goal=config.goal_text,
                workspace_fingerprint=config.workspace_fingerprint,
                step=1,
                attempts=[Attempt(step=1, script_path=script, log_path=log)],
            ))
            with (
                patch.object(runner, "run_preflight", return_value=runner.PreflightResult(
                    "", True, None,
                )),
                patch.object(workflow, "_ask") as ask,
            ):
                self.assertEqual(runner.run_workflow(config, resume=True), 3)
                self.assertIsNotNone(store.load().blocker)
                compiler.write_text("#!/bin/sh\necho 'test compiler restored'\n", encoding="utf-8")
                compiler.chmod(0o755)
                self.assertEqual(runner.run_workflow(config, resume=True), 0)
            ask.assert_not_called()
            self.assertTrue(store.load().done)
            self.assertIsNone(store.load().blocker)

    def test_missing_preflight_tool_returns_structured_blocker(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            with patch.dict(os.environ, {"PATH": temporary}):
                # Execute the shell directly so the deliberately empty PATH is usable.
                real_run = subprocess.run

                def run(command, **kwargs):
                    return real_run(["/bin/bash", *command[1:]], **kwargs)

                with patch.object(runner.subprocess, "run", side_effect=run):
                    preflight = runner.run_preflight(config)
            self.assertFalse(preflight.succeeded)
            self.assertIn("ruff", prerequisite_blocker(preflight.output).reason)


class PathSafetyTests(unittest.TestCase):
    def test_rejects_spack_repository_symlink_outside_writable_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").mkdir()
            (root / "checkout").mkdir()
            (root / "outside").mkdir()
            (root / "checkout" / "spack-repo").symlink_to(
                root / "outside", target_is_directory=True
            )
            spack = root / "spack"
            spack.touch()
            config_path = root / "agent.toml"
            config_path.write_text(
                """[workspace]
source_repository = "source"
writable_repository = "checkout"
spack_repository = "spack-repo"
recipe_path = "packages/example/package.py"
host_spack_executable = "spack"

[goal]
spec = "example@1.0"
context = "test"

[agent]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "resolves outside"):
                load_config(config_path)

    def test_agent_receives_source_spack_and_state_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            with patch.object(
                workflow, "ask_copilot", return_value=CopilotResult(text="ok")
            ) as ask:
                workflow._ask("prompt", config)

            self.assertEqual(
                ask.call_args.kwargs["add_dirs"],
                [
                    config.state_dir,
                    config.workspace.source_repository,
                    config.workspace.host_spack_executable.parent,
                ],
            )

    def test_container_agent_does_not_receive_host_spack_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            host_config = make_config(Path(temporary))
            config = AgentConfig(
                workspace=host_config.workspace,
                goal_context=host_config.goal_context,
                verification_spec=host_config.verification_spec,
                runner=RunnerConfig(backend="podman"),
                _config_directory=Path(temporary),
            )
            with patch.object(
                workflow, "ask_copilot", return_value=CopilotResult(text="ok")
            ) as ask:
                workflow._ask("prompt", config)

            self.assertEqual(
                ask.call_args.kwargs["add_dirs"],
                [config.state_dir, config.workspace.source_repository],
            )
            self.assertNotIn(
                str(config.workspace.host_spack_executable), workflow._workspace_context(config)
            )


class SessionSafetyTests(unittest.TestCase):
    def test_success_verdict_cannot_override_failed_verification_gates(self):
        for gate in ({"build_exit_code": 1}, {"preflight_succeeded": False}):
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as temporary:
                config = make_config(Path(temporary))
                store = SessionStore(config.state_dir)
                _, log_path = store.step_paths(1)
                Path(log_path).write_text(
                    "=== VERDICT ===\nexit_code=0\ngoal_met=yes\n", encoding="utf-8"
                )
                store.save(Session(
                    goal=config.goal_text,
                    repo_path=str(config.workspace.writable_repository),
                    workspace_fingerprint=config.workspace_fingerprint,
                    step=1,
                    attempts=[Attempt(step=1, log_path=log_path)],
                ))
                with patch.object(workflow, "_ask", return_value=CopilotResult(text="retry")) as ask:
                    self.assertEqual(workflow.process_result(config, store, **gate), 1)
                ask.assert_called_once()
                self.assertFalse(store.load().attempts[0].succeeded)

    def test_rejects_workspace_drift_before_processing_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            store = SessionStore(config.state_dir)
            store.save(
                Session(
                    goal="old goal",
                    repo_path=str(config.workspace.writable_repository),
                    workspace_fingerprint="old-fingerprint",
                    step=1,
                    attempts=[Attempt(step=1, log_path="unused")],
                )
            )
            with patch.object(workflow, "_ask") as ask:
                self.assertEqual(workflow.process_result(config, store), 1)
                ask.assert_not_called()

    def test_missing_initial_script_does_not_create_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            store = SessionStore(config.state_dir)
            script_path, _ = store.step_paths(1)
            Path(script_path).write_text("stale script", encoding="utf-8")
            with patch.object(
                workflow,
                "_ask",
                return_value=CopilotResult(text="forgot script", resume_id="resume-1"),
            ):
                self.assertEqual(workflow.start_goal(config, store), 1)
            self.assertIsNone(store.load())
            self.assertFalse(Path(script_path).exists())

    def test_missing_retry_script_does_not_advance_step(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            store = SessionStore(config.state_dir)
            _, log_path = store.step_paths(1)
            Path(log_path).write_text(
                "error: failed\n=== VERDICT ===\nexit_code=1\ngoal_met=no\n",
                encoding="utf-8",
            )
            store.save(
                Session(
                    goal=config.goal_text,
                    repo_path=str(config.workspace.writable_repository),
                    workspace_fingerprint=config.workspace_fingerprint,
                    step=1,
                    attempts=[Attempt(step=1, log_path=log_path)],
                )
            )
            with patch.object(
                workflow,
                "_ask",
                return_value=CopilotResult(text="forgot script", resume_id="resume-2"),
            ):
                self.assertEqual(workflow.process_result(config, store), 1)

            session = store.load()
            self.assertEqual(session.step, 1)
            self.assertEqual(len(session.attempts), 1)
            self.assertEqual(session.resume_id, "resume-2")
            self.assertEqual(session.attempts[0].error_signature, "failed")

    def test_session_lock_rejects_second_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionStore(Path(temporary) / ".spack-agent")
            with store.lock():
                with self.assertRaises(SessionLockedError):
                    with SessionStore(store.state_dir).lock():
                        self.fail("second lock unexpectedly acquired")


class RunnerTests(unittest.TestCase):
    def test_container_definitions_do_not_use_broad_compiler_discovery(self):
        container_directory = Path(runner.__file__).resolve().parent.parent / "container"
        gcc = (container_directory / "Containerfile.gcc").read_text(encoding="utf-8")
        oneapi = (container_directory / "Containerfile.oneapi").read_text(encoding="utf-8")
        self.assertIn("/usr/bin/gcc-${GCC_VERSION}", gcc)
        self.assertIn("/usr/bin/clang-${CLANG_VERSION}", gcc)
        self.assertIn("for compiler in icx icpx ifx", oneapi)
        for definition in (gcc, oneapi):
            self.assertIn("spack compiler find --scope=user /opt/spack-agent-compilers/bin", definition)
            self.assertIn("test -s /opt/spack-config/packages.yaml", definition)
        self.assertNotIn("spack compiler find --scope=user\n", gcc + oneapi)

    def test_linux_profile_passes_exact_build_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            host_config = make_config(Path(temporary))
            config = AgentConfig(
                workspace=host_config.workspace,
                goal_context="test",
                verification_spec="example %gcc@15",
                runner=RunnerConfig(backend="podman", toolchain="ubuntu26.04-gcc15"),
            )
            with patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertTrue(runner._build_podman_image(config))
            command = run.call_args.args[0]
            self.assertIn("SPACK_REF=v1.2.2", command)
            self.assertIn("UBUNTU_VERSION=26.04", command)
            self.assertIn("GCC_VERSION=15", command)
            self.assertTrue(command[command.index("--file") + 1].endswith("Containerfile.gcc"))

    def test_preflight_fails_when_lint_fails_before_successful_spack(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            tools = root / "tools"
            tools.mkdir()
            lint = tools / "ruff"
            lint.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            lint.chmod(0o755)
            spack = config.workspace.host_spack_executable
            spack.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            spack.chmod(0o755)
            with patch.dict(os.environ, {"PATH": f"{tools}:{os.environ['PATH']}"}):
                result = runner.run_preflight(config)

            self.assertFalse(result.succeeded)
            self.assertIsNone(result.dag_identity)
            self.assertNotIn("=== CONCRETIZED DAG ===", result.output)

    def test_preflight_audits_and_concretizes_configured_spec(self):
        with tempfile.TemporaryDirectory() as temporary:
            host_config = make_config(Path(temporary))
            config = AgentConfig(
                workspace=host_config.workspace,
                goal_context=host_config.goal_context,
                verification_spec="example@1.0 +feature %gcc",
                _config_directory=Path(temporary),
            )
            completed = subprocess.CompletedProcess(
                [], 0, stdout="=== CONCRETIZED DAG ===\n- example@1.0 hash\n"
            )
            with patch.object(runner.subprocess, "run", return_value=completed) as run:
                result = runner.run_preflight(config)

            command = run.call_args.args[0]
            self.assertIn("audit packages example", command[-1])
            self.assertIn("spec -Il 'example@1.0 +feature %gcc'", command[-1])
            self.assertTrue(result.succeeded)
            self.assertIsNotNone(result.dag_identity)

    def test_missing_podman_fails_before_starting_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            host_config = make_config(Path(temporary))
            config = AgentConfig(
                workspace=host_config.workspace,
                goal_context=host_config.goal_context,
                verification_spec=host_config.verification_spec,
                runner=RunnerConfig(backend="podman"),
                _config_directory=Path(temporary),
            )
            with (
                patch.object(runner.shutil, "which", return_value=None),
                patch.object(runner, "start_goal", return_value=0) as start,
            ):
                self.assertEqual(runner.run_workflow(config, resume=False), 1)
            start.assert_not_called()

    def test_missing_podman_image_is_built_before_starting_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            host_config = make_config(Path(temporary))
            config = AgentConfig(
                workspace=host_config.workspace,
                goal_context=host_config.goal_context,
                verification_spec=host_config.verification_spec,
                runner=RunnerConfig(backend="podman"),
                _config_directory=Path(temporary),
            )
            image_check = subprocess.CompletedProcess([], returncode=1)
            image_build = subprocess.CompletedProcess([], returncode=0)
            with (
                patch.object(runner.shutil, "which", return_value="/usr/bin/podman"),
                patch.object(
                    runner.subprocess,
                    "run",
                    side_effect=[image_check, image_build],
                ) as podman,
                patch.object(
                    runner,
                    "run_preflight",
                    return_value=runner.PreflightResult("", True, None),
                ),
                patch.object(runner, "start_goal", return_value=0) as start,
            ):
                self.assertEqual(runner.run_workflow(config, resume=False), 1)
            self.assertEqual(podman.call_args_list[1].args[0][:2], ["podman", "build"])
            start.assert_called_once()

    def test_podman_runtime_failure_is_not_sent_to_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_config = make_config(root)
            config = AgentConfig(
                workspace=host_config.workspace,
                goal_context=host_config.goal_context,
                verification_spec=host_config.verification_spec,
                runner=RunnerConfig(backend="podman"),
                _config_directory=root,
            )
            store = SessionStore(config.state_dir)
            script_path, log_path = store.step_paths(1)
            Path(script_path).write_text("true\n", encoding="utf-8")
            store.save(
                Session(
                    goal=config.goal_text,
                    repo_path=str(config.workspace.writable_repository),
                    workspace_fingerprint=config.workspace_fingerprint,
                    step=1,
                    attempts=[Attempt(step=1, script_path=script_path, log_path=log_path)],
                )
            )
            with (
                patch.object(runner, "_podman_image_exists", return_value=True),
                patch.object(runner, "run_step_script", return_value=125),
                patch.object(runner, "process_result") as process_result,
            ):
                self.assertEqual(runner.run_workflow(config, resume=True), 1)
            process_result.assert_not_called()

    def test_terminal_processing_failure_is_returned(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            store = SessionStore(config.state_dir)
            script_path, log_path = store.step_paths(1)
            Path(script_path).write_text("true\n", encoding="utf-8")
            store.save(
                Session(
                    goal=config.goal_text,
                    repo_path=str(config.workspace.writable_repository),
                    workspace_fingerprint=config.workspace_fingerprint,
                    step=1,
                    attempts=[Attempt(step=1, script_path=script_path, log_path=log_path)],
                )
            )
            with (
                patch.object(runner, "run_step_script", return_value=1),
                patch.object(runner, "run_preflight", return_value=runner.PreflightResult("", True, None)),
                patch.object(runner, "process_result", return_value=2),
            ):
                self.assertEqual(runner.run_workflow(config, resume=True), 2)

    def test_successful_script_still_passes_post_build_checks_to_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            store = SessionStore(config.state_dir)
            script_path, log_path = store.step_paths(1)
            Path(script_path).write_text("true\n", encoding="utf-8")
            store.save(
                Session(
                    goal=config.goal_text,
                    repo_path=str(config.workspace.writable_repository),
                    workspace_fingerprint=config.workspace_fingerprint,
                    step=1,
                    attempts=[Attempt(step=1, script_path=script_path, log_path=log_path)],
                )
            )
            with (
                patch.object(runner, "run_step_script", return_value=0),
                patch.object(
                    runner, "run_preflight",
                    return_value=runner.PreflightResult("lint failed", False, None),
                ) as preflight,
                patch.object(runner, "process_result", return_value=2) as process_result,
            ):
                self.assertEqual(runner.run_workflow(config, resume=True), 2)
            preflight.assert_called_once_with(config)
            self.assertEqual(process_result.call_args.kwargs["build_exit_code"], 0)
            self.assertFalse(process_result.call_args.kwargs["preflight_succeeded"])
            self.assertEqual(process_result.call_args.kwargs["preflight_output"], "lint failed")

    def test_iteration_cap_returns_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_config = make_config(root)
            config = AgentConfig(
                workspace=host_config.workspace,
                goal_context=host_config.goal_context,
                verification_spec=host_config.verification_spec,
                runner=RunnerConfig(max_iterations=1),
                _config_directory=root,
            )
            store = SessionStore(config.state_dir)
            script_path, log_path = store.step_paths(1)
            Path(script_path).write_text("true\n", encoding="utf-8")
            store.save(
                Session(
                    goal=config.goal_text,
                    repo_path=str(config.workspace.writable_repository),
                    workspace_fingerprint=config.workspace_fingerprint,
                    step=1,
                    attempts=[Attempt(step=1, script_path=script_path, log_path=log_path)],
                )
            )
            with (
                patch.object(runner, "run_step_script", return_value=1),
                patch.object(runner, "run_preflight", return_value=runner.PreflightResult("", True, None)),
                patch.object(runner, "process_result", return_value=0),
            ):
                self.assertEqual(runner.run_workflow(config, resume=True), 2)

    def test_run_starts_fresh_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            store = SessionStore(config.state_dir)
            store.save(Session(goal="old", step=1, attempts=[]))
            artifact = config.state_dir / "step_01.log"
            artifact.write_text("old output", encoding="utf-8")
            nested_artifact = config.state_dir / "step_01_failed_stages" / "build.log"
            nested_artifact.parent.mkdir()
            nested_artifact.write_text("old failure", encoding="utf-8")
            with (
                patch.object(runner, "run_preflight", return_value=runner.PreflightResult("", True, None)),
                patch.object(runner, "start_goal", return_value=1) as start,
            ):
                self.assertEqual(runner.run_workflow(config, resume=False), 1)
            start.assert_called_once()
            self.assertTrue(start.call_args.kwargs["force"])
            self.assertFalse(artifact.exists())
            self.assertFalse(nested_artifact.parent.exists())

    def test_resume_preserves_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            store = SessionStore(config.state_dir)
            script_path, log_path = store.step_paths(1)
            Path(script_path).write_text("true\n", encoding="utf-8")
            artifact = config.state_dir / "step_01_first_failure.log"
            artifact.write_text("preserve me", encoding="utf-8")
            store.save(Session(
                goal=config.goal_text,
                repo_path=str(config.workspace.writable_repository),
                workspace_fingerprint=config.workspace_fingerprint,
                step=1,
                attempts=[Attempt(step=1, script_path=script_path, log_path=log_path)],
            ))

            with patch.object(runner, "run_step_script", return_value=130):
                self.assertEqual(runner.run_workflow(config, resume=True), 130)

            self.assertEqual(artifact.read_text(encoding="utf-8"), "preserve me")

    def test_resume_requires_existing_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            with patch.object(runner, "start_goal") as start:
                self.assertEqual(runner.run_workflow(config, resume=True), 1)
            start.assert_not_called()

    def test_resume_replans_revised_goal_before_running_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root, task="revised goal")
            store = SessionStore(config.state_dir)
            old_script, old_log = store.step_paths(1)
            Path(old_script).write_text("old verification", encoding="utf-8")
            Path(old_log).write_text("partial compiler output\n", encoding="utf-8")
            store.save(
                Session(
                    goal="original goal",
                    repo_path=str(config.workspace.writable_repository),
                    workspace_fingerprint=config.workspace_fingerprint,
                    resume_id="resume-1",
                    step=1,
                    attempts=[Attempt(step=1, script_path=old_script, log_path=old_log)],
                )
            )

            def write_revised_script(*_args, **_kwargs):
                revised_script, _ = store.step_paths(2)
                Path(revised_script).write_text("new verification", encoding="utf-8")
                return CopilotResult(text="replanned", resume_id="resume-2")

            with (
                patch.object(workflow, "_ask", side_effect=write_revised_script),
                patch.object(runner, "run_step_script", return_value=130) as run_script,
                patch.object(
                    runner, "run_preflight",
                    return_value=runner.PreflightResult("", True, None),
                ),
            ):
                self.assertEqual(runner.run_workflow(config, resume=True), 130)

            revised_script, revised_log = store.step_paths(2)
            run_script.assert_called_once_with(
                config, revised_script, revised_log, config.runner.poll_seconds
            )
            session = store.load()
            self.assertEqual(session.goal, config.goal_text)
            self.assertEqual(session.step, 2)
            self.assertEqual(session.resume_id, "resume-2")
            self.assertEqual(len(session.attempts), 2)
            self.assertEqual(
                session.attempts[0].error_signature,
                "interrupted or superseded by revised goal",
            )

    def test_resume_reopens_finished_session_for_revised_goal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root, task="review follow-up")
            store = SessionStore(config.state_dir)
            old_script, old_log = store.step_paths(1)
            Path(old_script).write_text("verified", encoding="utf-8")
            Path(old_log).write_text(
                "=== VERDICT ===\nexit_code=0\ngoal_met=yes\n", encoding="utf-8"
            )
            store.save(
                Session(
                    goal="original goal",
                    repo_path=str(config.workspace.writable_repository),
                    workspace_fingerprint=config.workspace_fingerprint,
                    resume_id="resume-1",
                    step=1,
                    done=True,
                    attempts=[
                        Attempt(
                            step=1,
                            script_path=old_script,
                            log_path=old_log,
                            succeeded=True,
                        )
                    ],
                )
            )

            def write_revised_script(*_args, **_kwargs):
                revised_script, _ = store.step_paths(2)
                Path(revised_script).write_text("follow-up", encoding="utf-8")
                return CopilotResult(text="replanned", resume_id="resume-2")

            with (
                patch.object(workflow, "_ask", side_effect=write_revised_script),
                patch.object(runner, "run_step_script", return_value=130),
                patch.object(
                    runner, "run_preflight",
                    return_value=runner.PreflightResult("", True, None),
                ),
            ):
                self.assertEqual(runner.run_workflow(config, resume=True), 130)

            session = store.load()
            self.assertFalse(session.done)
            self.assertEqual(session.goal, config.goal_text)
            self.assertEqual(session.step, 2)
            self.assertTrue(session.attempts[0].succeeded)
            self.assertEqual(len(session.attempts), 2)

    def test_interruption_preserves_session_and_partial_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            store = SessionStore(config.state_dir)
            script_path, log_path = store.step_paths(1)
            Path(script_path).write_text("verification", encoding="utf-8")
            Path(log_path).write_text("partial output\n", encoding="utf-8")
            original = Session(
                goal=config.goal_text,
                repo_path=str(config.workspace.writable_repository),
                workspace_fingerprint=config.workspace_fingerprint,
                resume_id="resume-1",
                step=1,
                attempts=[Attempt(step=1, script_path=script_path, log_path=log_path)],
            )
            store.save(original)

            with patch.object(runner, "run_step_script", return_value=130):
                self.assertEqual(runner.run_workflow(config, resume=True), 130)

            preserved = store.load()
            self.assertEqual(preserved, original)
            self.assertEqual(Path(log_path).read_text(encoding="utf-8"), "partial output\n")

    def test_termination_escalates_for_entire_process_group(self):
        process = MagicMock(pid=1234)
        process.wait.side_effect = [subprocess.TimeoutExpired("build", 10), 0]
        with patch.object(os, "killpg") as killpg:
            runner.terminate_process_group(process)
        self.assertEqual(
            killpg.call_args_list,
            [unittest.mock.call(1234, signal.SIGTERM), unittest.mock.call(1234, signal.SIGKILL)],
        )

    def test_podman_command_uses_ephemeral_container_and_persistent_volumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            config = AgentConfig(
                workspace=config.workspace,
                goal_context=config.goal_context,
                verification_spec=config.verification_spec,
                runner=RunnerConfig(
                    backend="podman",
                    toolchain="clang",
                    cpus=4,
                    memory="8g",
                    network="none",
                    download_volume="spack-downloads",
                ),
                _config_directory=Path(temporary),
            )

            command = runner._podman_command(
                config, str(config.state_dir / "step_01.sh")
            )

            self.assertEqual(command[:3], ["podman", "run", "--rm"])
            self.assertIn(f"{config.workspace.source_repository}:/workspace/source:ro", command)
            self.assertIn(
                f"{config.workspace.writable_repository}:/workspace/packages:rw", command
            )
            self.assertIn("spack-downloads:/var/cache/spack:rw", command)
            self.assertIn(f"{config.runner.install_volume}:/opt/spack-store:rw", command)
            self.assertEqual(
                command[-3:],
                [config.runner.image, "bash", "/workspace/state/step_01.sh"],
            )


class CliTests(unittest.TestCase):
    def test_goal_command_is_not_public(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["goal"])

    def test_run_rejects_removed_runner_overrides(self):
        parser = cli.build_parser()
        for option in ("--poll", "--max-iterations", "--container", "--host"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                parser.parse_args(["run", option])