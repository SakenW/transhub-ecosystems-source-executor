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
        self.assertEqual(snapshot["contract_revision"], 18)
        self.assertEqual(snapshot["parser"], "obsidian-plugin-ui-structured-v18")
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
        self.assertEqual(
            strings["Show at the top"]["evidence"][0]["symbol"],
            "settingsDropdownOption",
        )
        self.assertEqual(
            strings["Capture to active file"]["evidence"][0]["symbol"], "svelteForm"
        )

    def test_svelte_template_text_uses_real_static_nodes_without_html_patch_span(
        self,
    ) -> None:
        bundle = "const markup = q('<div><span>Alpha</span><span>Beta</span><!><p>Save &amp; Close</p><p>Before <!> After</p><span title=\"Hidden > attribute\">Visible</span><span>Unknown &custom;</span><code>Code internals</code><script>Script internals</script><style>Style internals</style></div>');"
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"quickadd","name":"QuickAdd","version":"2.25.0","description":"Quickly add new pages or content to your vault."}',
                bundle.encode("utf-8"),
            )
        )
        strings = {row["source"]: row for row in snapshot["strings"]}
        for source in ("Alpha", "Beta", "Save & Close", "Before", "After", "Visible"):
            self.assertIn(source, strings)
        for source in (
            "Alpha Beta",
            "Save &amp; Close",
            "Unknown &custom;",
            "Code internals",
            "Script internals",
            "Style internals",
            "Hidden > attribute",
        ):
            self.assertNotIn(source, strings)
        self.assertEqual(
            strings["Save & Close"]["evidence"][0]["symbol"], "svelteTemplate"
        )
        self.assertNotIn("literal_start", strings["Save & Close"]["evidence"][0])
