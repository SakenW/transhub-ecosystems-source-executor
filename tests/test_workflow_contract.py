from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_is_manual_protected_and_non_persistent(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "public-discovery-executor.yml"
        ).read_text(encoding="utf-8")
        lowered = workflow.lower()

        self.assertIn("workflow_dispatch:", workflow)
        for forbidden_trigger in (
            "schedule:",
            "push:",
            "pull_request:",
            "repository_dispatch:",
            "workflow_call:",
        ):
            self.assertNotIn(forbidden_trigger, workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("id-token: write", workflow)
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
        self.assertNotRegex(workflow, r"(?m)^\s*[^#\n]*\+\s{2,}")
        for forbidden_channel in (
            "actions/cache",
            "actions/upload-artifact",
            "actions/download-artifact",
            "secrets.",
        ):
            self.assertNotIn(forbidden_channel, lowered)


if __name__ == "__main__":
    unittest.main()
