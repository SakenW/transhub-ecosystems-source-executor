from __future__ import annotations

import re
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_is_protected_periodic_and_non_persistent(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "public-discovery-executor.yml"
        ).read_text(encoding="utf-8")
        lowered = workflow.lower()

        self.assertEqual(
            re.findall(r"(?m)^\s*runs-on:\s*(.+)$", workflow),
            ["[self-hosted, Linux, X64, docker, transhub-source-executor]"],
        )
        for hosted in ("ubuntu-latest", "windows-latest", "macos-latest"):
            self.assertNotIn(hosted, workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("schedule:", workflow)
        self.assertIn('cron: "7-59/5 * * * *"', workflow)
        for forbidden_trigger in (
            "push:",
            "pull_request:",
            "repository_dispatch:",
            "workflow_call:",
        ):
            self.assertNotIn(forbidden_trigger, workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("group: public-discovery-executor", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("github.ref_protected != true", workflow)
        self.assertIn("github.event.repository.default_branch", workflow)
        self.assertGreaterEqual(workflow.count("github.ref_protected == true"), 4)
        self.assertRegex(
            workflow,
            r"actions/checkout@[0-9a-f]{40}",
        )
        self.assertIn("persist-credentials: false", workflow)
        self.assertEqual(
            set(re.findall(r"vars[.]([A-Z0-9_]+)", workflow)),
            {
                "TRANSHUB_PUBLIC_DISCOVERY_API_BASE",
                "TRANSHUB_PUBLIC_DISCOVERY_OIDC_AUDIENCE",
            },
        )
        self.assertEqual(
            workflow.count("-m adapters.obsidian.build_public_executor"), 2
        )
        self.assertIn('cmp "$temporary/first.pyz" "$temporary/second.pyz"', workflow)
        self.assertIn(
            'cmp "$temporary/first.receipt" "$temporary/second.receipt"', workflow
        )
        self.assertIn(
            "adapters.obsidian.public_discovery_executor",
            workflow,
        )
        self.assertRegex(workflow, r"actions/attest@[0-9a-f]{40}")
        self.assertIn(
            "subject-path: ${{ runner.temp }}/public-discovery-executor.pyz", workflow
        )
        self.assertIn("inputs.attest_only == true", workflow)
        self.assertIn("inputs.attest_only != true", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s*[^#\n]*\+\s{2,}")
        for forbidden_channel in (
            "actions/cache",
            "actions/upload-artifact",
            "actions/download-artifact",
            "secrets.",
        ):
            self.assertNotIn(forbidden_channel, lowered)

    def test_job_docker_config_is_empty_private_and_inside_runner_temp(self) -> None:
        workflow = (
            ROOT / ".github/workflows/public-discovery-executor.yml"
        ).read_text()
        step = workflow.split("- name: Prepare job-scoped Docker config", 1)[1].split(
            "- name:", 1
        )[0]
        shell = step.split("        run: |\n", 1)[1]
        shell = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in shell.splitlines()
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment_file = root / "github-env"
            environment_file.touch()
            env = {
                **os.environ,
                "RUNNER_TEMP": str(root),
                "GITHUB_ENV": str(environment_file),
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2",
            }
            subprocess.run(["bash", "-euc", shell], env=env, check=True)
            config = root / "public-source-docker-123-2/config.json"
            self.assertEqual(json.loads(config.read_text()), {})
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                environment_file.read_text(), f"DOCKER_CONFIG={config.parent}\n"
            )


if __name__ == "__main__":
    unittest.main()
