import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from spack_agent.config import AgentConfig, RunnerConfig, WorkspaceConfig, load_config
from spack_agent.copilot import CopilotResult
from spack_agent import cli, runner, workflow
from spack_agent.session import Attempt, Session, SessionLockedError, SessionStore


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
                patch.object(runner, "process_result", return_value=2),
            ):
                self.assertEqual(runner.run_workflow(config, resume=True), 2)

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
            self.assertIn("spack-agent-store-clang:/opt/spack-store:rw", command)
            self.assertEqual(
                command[-3:],
                ["spack-agent-builder:clang", "bash", "/workspace/state/step_01.sh"],
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