from __future__ import annotations

import base64
import json
import unittest
from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock

from adapters.obsidian.public_discovery_executor import (
    ExecutorError,
    _attach_license_evidence,
    _read_pinned_license_evidence,
)
from tests.test_executor_state import _plan


class LicenseEvidenceTests(unittest.TestCase):
    def reader(self, body: bytes, identifier: str = "NOASSERTION") -> Mock:
        reader = Mock()
        reader.json_object.return_value = {
            "license": {"spdx_id": identifier},
            "content": base64.encodebytes(body).decode("ascii"),
            "encoding": "base64",
            "path": "LICENSE.md",
        }
        return reader

    def test_unknown_license_records_exact_evidence_without_granting_permission(self) -> None:
        body = b"Custom license: redistribution prohibited."
        evidence = _read_pinned_license_evidence(self.reader(body), _plan())
        self.assertEqual(evidence.identifier, "LicenseRef-SHA256-" + sha256(body).hexdigest())
        result = json.loads(_attach_license_evidence(
            b'{"result":{"protocol":"trans-hub.public-discovery-result","revision":1},"source_catalog":{}}',
            evidence,
        ))
        self.assertEqual(set(result["license_evidence"]), {
            "immutable_source_revision", "license_digest", "license_identifier", "license_evidence_uri",
        })
        self.assertNotIn(body.decode(), json.dumps(result))
        self.assertNotIn("allows_fork", result["license_evidence"])

    def test_identity_depends_on_full_license_bytes_not_plugin_name(self) -> None:
        first = _read_pinned_license_evidence(self.reader(b"same license"), _plan())
        second = _read_pinned_license_evidence(
            self.reader(b"same license"), replace(_plan(), repository_name="other-plugin"),
        )
        changed = _read_pinned_license_evidence(self.reader(b"same license\n"), _plan())
        self.assertEqual(first.identifier, second.identifier)
        self.assertNotEqual(first.uri, second.uri)
        self.assertNotEqual(first.identifier, changed.identifier)

    def test_known_spdx_identifier_is_preserved(self) -> None:
        self.assertEqual(_read_pinned_license_evidence(self.reader(b"MIT body", "MIT"), _plan()).identifier, "MIT")

    def test_unknown_license_still_requires_valid_nonempty_bytes(self) -> None:
        for content in ["", "not base64!"]:
            reader = self.reader(b"license")
            reader.json_object.return_value["content"] = content
            with self.assertRaisesRegex(ExecutorError, "license_evidence_invalid"):
                _read_pinned_license_evidence(reader, _plan())

    def test_absent_license_is_terminal_and_network_failure_remains_retryable(self) -> None:
        reader = Mock()
        reader.json_object.side_effect = ExecutorError("registry_github_not_found")
        with self.assertRaisesRegex(ExecutorError, "executor_license_missing"):
            _read_pinned_license_evidence(reader, _plan())
        reader.json_object.side_effect = ExecutorError("registry_github_http_503", retryable=True)
        with self.assertRaises(ExecutorError) as caught:
            _read_pinned_license_evidence(reader, _plan())
        self.assertTrue(caught.exception.retryable)
