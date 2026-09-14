import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import json

from spack_agent.config import AgentConfig, RunnerConfig, WorkspaceConfig, load_config
from spack_agent import workflow
from spack_agent.copilot import CopilotResult
from spack_agent.session import (
    Attempt,
    Session,
    SessionStore,
    dag_identity,
    error_signature,
    evaluate_log,
    failure_category,
)


class ConfigTests(unittest.TestCase):
    def test_requires_goal_spec_and_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "checkout" / "spack-repo").mkdir(parents=True)
            (root / "source").mkdir()
            (root / "spack").touch()
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

[agent]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "goal.*context"):
                load_config(config_path)

    def test_rejects_non_string_goal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "checkout" / "spack-repo").mkdir(parents=True)
            (root / "source").mkdir()
            (root / "spack").touch()
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
context = 42

[agent]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "goal.context must be a string"):
                load_config(config_path)

    def test_rejects_invalid_agent_setting_types(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "checkout" / "spack-repo").mkdir(parents=True)
            (root / "source").mkdir()
            (root / "spack").touch()
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
allow_tools = "shell"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "agent.allow_tools"):
                load_config(config_path)

    def test_loads_podman_runner_and_includes_it_in_session_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "checkout" / "spack-repo").mkdir(parents=True)
            (root / "source").mkdir()
            (root / "spack").touch()
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

[runner]
backend = "podman"
poll_seconds = 30
max_iterations = 7
toolchain = "clang"
cpus = 8
memory = "16g"
network = "none"
download_volume = "downloads"

[agent]
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.runner.backend, "podman")
            self.assertEqual(config.runner.poll_seconds, 30)
            self.assertEqual(config.runner.max_iterations, 7)
            self.assertEqual(config.runner.toolchain, "clang")
            self.assertEqual(config.runner.image, "spack-agent-builder:clang")
            self.assertEqual(config.runner.install_volume, "spack-agent-store-clang")
            self.assertEqual(config.runner.cpus, 8)
            self.assertNotEqual(
                config.workspace_fingerprint,
                AgentConfig(
                    workspace=config.workspace,
                    goal_context=config.goal_context,
                    verification_spec=config.verification_spec,
                    _config_directory=root,
                ).workspace_fingerprint,
            )

    def test_podman_runner_does_not_require_host_spack(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "checkout" / "spack-repo").mkdir(parents=True)
            (root / "source").mkdir()
            config_path = root / "agent.toml"
            config_path.write_text(
                """[workspace]
source_repository = "source"
writable_repository = "checkout"
spack_repository = "spack-repo"
recipe_path = "packages/example/package.py"

[runner]
backend = "podman"

[goal]
spec = "example@1.0"
context = "test"

[agent]
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertIsNone(config.workspace.host_spack_executable)

    def test_host_runner_requires_host_spack(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "checkout" / "spack-repo").mkdir(parents=True)
            (root / "source").mkdir()
            config_path = root / "agent.toml"
            config_path.write_text(
                """[workspace]
source_repository = "source"
writable_repository = "checkout"
spack_repository = "spack-repo"
recipe_path = "packages/example/package.py"

[goal]
spec = "example@1.0"
context = "test"

[agent]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "required for host runner"):
                load_config(config_path)

    def test_oneapi_toolchain_uses_its_own_image_and_store(self):
        runner = RunnerConfig(toolchain="oneapi")

        self.assertEqual(runner.image, "spack-agent-builder:oneapi")
        self.assertEqual(runner.install_volume, "spack-agent-store-oneapi")

    def test_resolves_explicit_spack_path_roles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recipe = root / "checkout" / "spack-repo" / "packages" / "example" / "package.py"
            recipe.parent.mkdir(parents=True)
            recipe.touch()
            spack = root / "spack" / "bin" / "spack"
            spack.parent.mkdir(parents=True)
            spack.touch()
            source = root / "source"
            source.mkdir()
            config_path = root / "agent.toml"
            config_path.write_text(
                """[workspace]
writable_repository = "checkout"
spack_repository = "spack-repo"
recipe_path = "packages/example/package.py"
host_spack_executable = "spack/bin/spack"
source_repository = "source"

[goal]
spec = "example@1.0"
context = "Build example"

[agent]
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.workspace.writable_repository, root / "checkout")
            self.assertEqual(config.workspace.spack_repository_dir, root / "checkout" / "spack-repo")
            self.assertEqual(config.workspace.recipe_file, recipe)
            self.assertEqual(config.workspace.host_spack_executable, spack)
            self.assertEqual(config.workspace.source_repository, source)
            self.assertEqual(config.state_dir, root / ".spack-agent")

    def test_allows_missing_recipe_as_creation_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "checkout" / "spack-repo").mkdir(parents=True)
            spack = root / "spack"
            spack.touch()
            (root / "source").mkdir()
            config_path = root / "agent.toml"
            config_path.write_text(
                """[workspace]
source_repository = "source"
writable_repository = "checkout"
spack_repository = "spack-repo"
recipe_path = "packages/new-package/package.py"
host_spack_executable = "spack"

[goal]
spec = "example@1.0"
context = "Create the recipe"

[agent]
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(
                config.workspace.recipe_file,
                root / "checkout" / "spack-repo" / "packages" / "new-package" / "package.py",
            )
            self.assertFalse(config.workspace.recipe_file.exists())

    def test_source_repository_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "agent.toml"
            config_path.write_text(
                """[workspace]
writable_repository = "checkout"
spack_repository = "spack-repo"
recipe_path = "packages/example/package.py"
host_spack_executable = "spack/bin/spack"

[goal]
spec = "example@1.0"
context = "Build example"

[agent]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "source_repository"):
                load_config(config_path)

    def test_state_directory_is_not_configurable(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "agent.toml"
            config_path.write_text(
                """[workspace]
source_repository = "source"
writable_repository = "checkout"
spack_repository = "spack-repo"
recipe_path = "packages/example/package.py"
host_spack_executable = "spack/bin/spack"

[goal]
spec = "example@1.0"
context = "Build example"

[agent]
state_dir = "somewhere-else"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unknown.*state_dir"):
                load_config(config_path)


class VerdictTests(unittest.TestCase):
    def test_failure_signature_prefers_causal_error_to_spack_summary(self):
        missing_executable = error_signature(
            "/usr/bin/llvm-config: No such file or directory\n"
            "==> Error: The following packages failed to install:\n"
        )
        failed_patch = error_signature(
            "1 out of 3 hunks FAILED -- saving rejects to file package.py.rej\n"
            "==> Error: The following packages failed to install:\n"
        )

        self.assertEqual(
            missing_executable, "/usr/bin/llvm-config: No such file or directory"
        )
        self.assertEqual(
            failed_patch,
            "1 out of 3 hunks FAILED -- saving rejects to file package.py.rej",
        )

    def test_distinct_cmake_failures_do_not_trigger_retry_stop(self):
        taskflow_signature = error_signature(
            "CMake Error at CMakeLists.txt:100 (message):\n\n"
            "  Taskflow currently supports the following compilers:\n\n"
            "==> Error: The following packages failed to install:\n"
        )
        dealii_signature = error_signature(
            "CMake Error at cmake/macros/macro_configure_feature.cmake:111 (message):\n\n"
            "  Could not find the cgal library!\n\n"
            "==> Error: The following packages failed to install:\n"
        )
        session = Session(
            attempts=[
                Attempt(step=1, error_signature=taskflow_signature),
                Attempt(step=2, error_signature=dealii_signature),
            ]
        )

        self.assertEqual(
            taskflow_signature,
            "CMake Error: Taskflow currently supports the following compilers:",
        )
        self.assertEqual(dealii_signature, "CMake Error: Could not find the cgal library!")
        self.assertFalse(session.is_stuck_on(dealii_signature))

    def test_failure_category_and_dag_identity_are_stable(self):
        dag = "- 4c-multiphysics@main abcdef\n^dealii@9.6.2 fedcba\n"

        self.assertEqual(failure_category("CMake Error at CMakeLists.txt:1"), "configuration")
        self.assertEqual(failure_category("undefined reference to `symbol'"), "link")
        self.assertEqual(dag_identity(dag), dag_identity(dag))
        self.assertIsNone(dag_identity(""))

    def test_requires_explicit_success_verdict(self):
        self.assertEqual(
            evaluate_log("=== VERDICT ===\nexit_code=0\ngoal_met=yes\n"),
            (True, None),
        )
        self.assertEqual(evaluate_log("build completed\n"), (False, "missing verdict block"))
        succeeded, signature = evaluate_log(
            "=== VERDICT ===\nexit_code=1\ngoal_met=no\n"
        )
        self.assertFalse(succeeded)
        self.assertIsNotNone(signature)


class JustInTimeTests(unittest.TestCase):
    def test_successful_build_does_not_start_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / ".spack-agent"
            log_path = state_dir / "step_01.log"
            state_dir.mkdir()
            log_path.write_text(
                "=== VERDICT ===\nexit_code=0\ngoal_met=yes\n",
                encoding="utf-8",
            )
            config = AgentConfig(
                workspace=WorkspaceConfig(
                    writable_repository=root,
                    spack_repository=Path("spack-repo"),
                    recipe_path=Path("package.py"),
                    host_spack_executable=root / "spack",
                    source_repository=root,
                ),
                goal_context="test",
                verification_spec="example@1.0",
            )
            store = SessionStore(state_dir)

            with patch.object(workflow, "_ask") as ask:
                store.save(Session(
                    goal=config.goal_text,
                    repo_path=temporary,
                    workspace_fingerprint=config.workspace_fingerprint,
                    step=1,
                    attempts=[
                        Attempt(
                            step=1,
                            script_path=str(state_dir / "step_01.sh"),
                            log_path=str(log_path),
                        )
                    ],
                ))

                self.assertEqual(workflow.process_result(config, store), 0)
                ask.assert_not_called()
                self.assertTrue(store.load().done)

    def test_causal_log_produces_structured_result_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / ".spack-agent"
            state_dir.mkdir()
            log_path = state_dir / "step_01.log"
            log_path.write_text(
                "==> Error: The following packages failed to install:\n"
                "=== VERDICT ===\nexit_code=1\ngoal_met=no\n",
                encoding="utf-8",
            )
            causal_path = state_dir / "step_01_first_failure.log"
            causal_path.write_text(
                "CMake Error at CMakeLists.txt:100 (message):\n\n"
                "  Taskflow currently supports the following compilers:\n",
                encoding="utf-8",
            )
            config = AgentConfig(
                workspace=WorkspaceConfig(
                    writable_repository=root,
                    spack_repository=Path("spack-repo"),
                    recipe_path=Path("package.py"),
                    host_spack_executable=root / "spack",
                    source_repository=root,
                ),
                goal_context="test",
                verification_spec="example@1.0",
            )
            store = SessionStore(state_dir)
            store.save(Session(
                goal=config.goal_text,
                repo_path=temporary,
                workspace_fingerprint=config.workspace_fingerprint,
                step=1,
                attempts=[Attempt(step=1, log_path=str(log_path))],
            ))

            with patch.object(workflow, "_ask", return_value=CopilotResult(text="")):
                self.assertEqual(
                    workflow.process_result(config, store, preflight_dag_identity="dag-id"),
                    1,
                )

            artifact = json.loads((state_dir / "step_01_result.json").read_text())
            self.assertEqual(artifact["failure_category"], "configuration")
            self.assertEqual(artifact["dag_identity"], "dag-id")
            self.assertEqual(
                artifact["failure_signature"],
                "CMake Error: Taskflow currently supports the following compilers:",
            )


if __name__ == "__main__":
    unittest.main()