from __future__ import annotations

import json
import unittest

from adapters.obsidian.adapter_worker import build_snapshot


class ObsidianAdapterWorkerTests(unittest.TestCase):
    def test_extracts_quickadd_style_declarative_and_svelte_ui_copy(self) -> None:
        bundle = "\n".join(
            [
                'function choiceName(kind) { switch (kind) { case "Template": return "New template"; case "Capture": return "New capture"; case "Macro": return "New macro"; } }',
                "const markup = q('<div><h4>Location</h4><!></div>');",
                'const group = { type: "group", heading: "Choice picker", items: [{ name: "New note from template", desc: docs("Collect a choice\\\'s inputs in one form before it runs.", ref), control: { type: "dropdown", options: { bottom: "Show at the bottom (keeps your top choice first)", top: "Show at the top", off: "Hide" } } }] };',
                'mount(node, { name: "Capture to active file", desc: "Capture into whichever note is open when the choice runs, instead of a fixed target.", control: value => value });',
                'mount(node, { name: "Create file if it doesn\\\'t exist", control: value => value });',
                'mount(node, { name: "Behavior", heading: !0 });',
            ]
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"quickadd","name":"QuickAdd","version":"2.25.0","description":"Quickly add new pages or content to your vault."}',
                bundle.encode("utf-8"),
            )
        )
        strings = {row["source"]: row for row in snapshot["strings"]}
        self.assertEqual(snapshot["contract_revision"], 17)
        self.assertEqual(snapshot["parser"], "obsidian-plugin-ui-structured-v17")
        self.assertTrue(
            {
                "New template",
                "New capture",
                "New macro",
                "Location",
                "Collect a choice's inputs in one form before it runs.",
                "Show at the bottom (keeps your top choice first)",
                "Show at the top",
                "Hide",
                "Capture to active file",
                "Capture into whichever note is open when the choice runs, instead of a fixed target.",
                "Create file if it doesn't exist",
                "Behavior",
            }.issubset(strings)
        )
        self.assertEqual(strings["Show at the top"]["evidence"][0]["symbol"], "settingsDropdownOption")
        self.assertEqual(strings["Capture to active file"]["evidence"][0]["symbol"], "svelteForm")
