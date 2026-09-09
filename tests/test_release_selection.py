import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from adapters.obsidian.public_discovery_executor import (
    _select_release, HttpGitHubMetadataReader, ExecutorError,
)

class ReleaseSelectionTests(unittest.TestCase):
    def test_mislabeled_beta_uses_stable_history(self):
        reader = Mock()
        beta = dict(tag_name='2.0.0-beta.2', draft=False, prerelease=False)
        stable = dict(tag_name='1.5.10', draft=False, prerelease=False)
        reader.json_object.return_value = beta
        reader.releases.return_value = [beta, stable]
        self.assertEqual(_select_release(reader, '/repos/a/b'), stable)

    def test_stable_latest_needs_no_history_request(self):
        reader = Mock()
        reader.json_object.return_value = dict(tag_name='1.5.10', draft=False, prerelease=False)
        _select_release(reader, '/repos/a/b')
        reader.releases.assert_not_called()

    def test_beta_only_is_supplement_after_exhausting_history(self):
        reader = Mock()
        beta = dict(tag_name='2.0.0-beta.2', draft=False, prerelease=True)
        reader.json_object.side_effect = ExecutorError('registry_github_not_found')
        reader.releases.return_value = [beta]
        self.assertEqual(_select_release(reader, '/repos/a/b'), beta)

    def test_history_limit_does_not_guess_no_stable_release_exists(self):
        reader = Mock()
        beta = dict(tag_name='2.0.0-beta.2', draft=False, prerelease=False)
        reader.json_object.return_value = beta
        reader.releases.return_value = [beta] * 100
        with self.assertRaisesRegex(ExecutorError, 'search_limit'):
            _select_release(reader, '/repos/a/b')
        self.assertEqual(reader.releases.call_count, 3)

    def test_not_found_is_not_a_transient_network_error(self):
        with patch('adapters.obsidian.public_discovery_executor.urlopen', side_effect=HTTPError('https://api.github.com/repos/a/b/license',404,'Not Found',{},None)):
            with self.assertRaises(ExecutorError) as caught:
                HttpGitHubMetadataReader().json_object('/repos/a/b/license')
        self.assertEqual(caught.exception.code, 'registry_github_not_found')
        self.assertFalse(caught.exception.retryable)
