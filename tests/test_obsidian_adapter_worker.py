from __future__ import annotations

import base64
import json
import unittest
import zlib

from adapters.obsidian.adapter_worker import build_snapshot


class ObsidianAdapterWorkerTests(unittest.TestCase):
    def test_large_function_reference_is_not_mistaken_for_settings_entry(self) -> None:
        functions = ",".join(f"f{index}:{{name:'function {index}'}}" for index in range(140))
        bundle = (
            'const docs={'
            'date:{name:"date",description:"This module contains date helpers."},'
            'system:{name:"system",description:"This module contains system helpers."},'
            'web:{name:"web",description:"This module contains web helpers."},'
            'file:{name:"file",description:"This module contains every internal function related to files.",'
            f'functions:{{{functions}}}}}'
            '};'
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        sources = {row["source"] for row in snapshot["strings"]}
        self.assertIn("date", sources)
        self.assertNotIn("file", sources)
        self.assertNotIn("This module contains every internal function related to files.", sources)

    def test_static_wrapper_in_ui_sink_keeps_punctuation_but_rejects_html(self) -> None:
        bundle = (
            'setting.setName(R("Date & Time"));'
            'setting.setName(R("Add archive date/time after card title"));'
            'setting.setDesc(R("When toggled, dates link to daily notes. Eg. [[2021-04-26]]"));'
            'setting.setDesc(R("<div>Unsafe HTML description</div>"));'
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        sources = {row["source"] for row in snapshot["strings"]}
        self.assertIn("Date & Time", sources)
        self.assertIn("Add archive date/time after card title", sources)
        self.assertNotIn("<div>Unsafe HTML description</div>", sources)

    def test_locale_catalog_skips_function_return_templates_without_static_client_slot(self) -> None:
        bundle = (
            'var en={title:t=>`Move ${t} files`,static:"Static label"};'
            'var de={title:t=>`Verschiebe ${t} Dateien`,static:"Statische Beschriftung"};'
            'var zh={title:t=>`移动 ${t} 个文件`,static:"静态标签"};'
            'var locales={de:de,en:en,zh:zh};'
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        sources = {row["source"] for row in snapshot["strings"]}
        self.assertIn("Static label", sources)
        self.assertNotIn("Move {{th:expr:0}} files", sources)

    def test_settings_schema_accepts_qualified_siblings_after_metadata(self) -> None:
        bundle = (
            'const settings={version:1,'
            'hover:{name:"Colourless hover",desc:"Disable colour while hovering."},'
            'drag:{name:"Colourless drag",desc:"Disable colour while dragging."},'
            'select:{name:"Colourless selection",desc:"Disable colour while selected."}};'
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        sources = {row["source"] for row in snapshot["strings"]}
        self.assertTrue({"Colourless hover", "Colourless drag", "Colourless selection"} <= sources)

    def test_lazy_commonjs_locale_switch_binds_english_without_running_loaders(self) -> None:
        bundle = """
var baseline={settings:{name:"Larger symbols",desc:"Show bigger symbols",hint:"Keep %s backups"}};
var english=commonJS((unused,module)=>{module.exports={settings:{name:"Larger symbols",desc:"Show bigger symbols",hint:"Keep %s backups"}}});
var chinese=commonJS((unused,module)=>{module.exports={settings:{name:"大号符号",desc:"显示更大的符号",hint:"保留 %s 份备份"}}});
var german=commonJS((unused,module)=>{module.exports={settings:{name:"Große Symbole",desc:"Größere Symbole anzeigen",hint:"%s Backups behalten"}}});
function getText(locale){let promise;switch(locale){
case "en-GB":promise=Promise.resolve().then(()=>interop(english(),1));break;
case "zh":promise=Promise.resolve().then(()=>interop(chinese(),1));break;
case "de":promise=Promise.resolve().then(()=>interop(german(),1));break;
default:return baseline}
return promise;}
"""
        manifest = b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}'
        snapshot = json.loads(build_snapshot(manifest, bundle.encode("utf-8")))
        strings = {row["source"]: row for row in snapshot["strings"]}
        self.assertIn("Larger symbols", strings)
        self.assertIn("Keep %s backups", strings)
        self.assertNotIn("Große Symbole", strings)
        self.assertTrue(any(row["locale"] == "zh-CN" for row in snapshot["native_locale_coverage"]))
        self.assertTrue(
            any(evidence["symbol"].startswith("locale:en") for evidence in strings["Larger symbols"]["evidence"])
        )
        invalid = bundle.replace("module.exports", "other.exports")
        rejected = json.loads(build_snapshot(manifest, invalid.encode("utf-8")))
        self.assertEqual(rejected["native_locale_coverage"], [])

    def test_bounded_packed_native_catalog_exposes_english_source(self) -> None:
        chinese = 'var zh={title:"目录标题",help:"目录帮助",button:"保存更改"};'
        spanish = 'var es={title:"Título del catálogo",help:"Ayuda del catálogo",button:"Guardar cambios"};'
        packed = base64.b64encode(zlib.compress(chinese.encode("utf-8"))).decode("ascii")
        packed_es = base64.b64encode(zlib.compress(spanish.encode("utf-8"))).decode("ascii")
        bundle = (
            f'var PLUGIN_LANGUAGES={{zh:"{packed}",es:"{packed_es}"}};'
            'var en={title:"Catalog title",help:"Catalog help",button:"Save changes"};'
            'let locale=null;'
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        strings = {row["source"]: row for row in snapshot["strings"]}
        for source in ("Catalog title", "Catalog help", "Save changes"):
            self.assertIn(source, strings)
            self.assertTrue(
                any(evidence["symbol"].startswith("locale:en") for evidence in strings[source]["evidence"])
            )
        self.assertNotIn("目录标题", strings)
        self.assertEqual(
            {row["locale"] for row in snapshot["native_locale_coverage"]},
            {"es", "zh-CN"},
        )

    def test_packed_catalog_rejects_corrupt_or_oversized_native_payload(self) -> None:
        oversized = base64.b64encode(zlib.compress(b"A" * 2_000_001)).decode("ascii")
        for packed in ("not-base64", oversized):
            bundle = (
                f'var PLUGIN_LANGUAGES={{zh:"{packed}"}};'
                'var en={title:"Catalog title",help:"Catalog help",button:"Save changes"};'
            )
            snapshot = json.loads(
                build_snapshot(
                    b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                    bundle.encode("utf-8"),
                )
            )
            self.assertNotIn("Catalog title", {row["source"] for row in snapshot["strings"]})

    def test_extracts_quickadd_style_declarative_and_svelte_ui_copy(self) -> None:
        bundle = "\n".join(
            [
                'function choiceName(kind) { switch (kind) { case "Template": return "New template"; case "Capture": return "New capture"; case "Macro": return "New macro"; } }',
                'button.onClick(() => editor.onAddChoice(choiceName(kind), kind));',
                "const markup = q('<div><h4>Location</h4><!></div>');",
                'const group = { type: "group", heading: "Choice picker", items: [{ name: "New note from template", desc: this.descWithDocsLink("Collect a choice\\\'s inputs in one form before it runs.", ref), control: { type: "dropdown", options: { bottom: "Show at the bottom (keeps your top choice first)", top: "Show at the top", off: "Hide" } } }] };',
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
        self.assertEqual(snapshot["contract_revision"], 29)
        self.assertEqual(snapshot["parser"], "obsidian-plugin-ui-structured-v29")
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

    def test_choice_factory_does_not_borrow_unrelated_switch_brace(self) -> None:
        bundle = 'switch (kind); const options = { case "Template": return "New template", case "Capture": return "New capture", case "Macro": return "New macro" };'
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"quickadd","name":"QuickAdd","version":"2.25.0","description":"Quickly add new pages or content to your vault."}',
                bundle.encode("utf-8"),
            )
        )
        self.assertNotIn("New template", {row["source"] for row in snapshot["strings"]})

    def test_static_reactive_choice_labels_require_svelte_text_node_sink(self) -> None:
        bundle = "\n".join(
            [
                'let choiceLabel=P(()=>compact()?"Add choice":"New choice"),folderLabel=P(()=>compact()?"Add folder":"New folder");',
                'var choiceText=de(choiceNode,!0),folderText=de(folderNode,!0);',
                'ae(()=>{ue(choiceText,h(choiceLabel));ue(folderText,h(folderLabel))});',
                'let hidden=P(()=>compact()?"Internal enabled":"Internal disabled");',
                'var wrong=other(node,!0);log(h(hidden));',
                'function first(){let cross=P(()=>flag()?"Private yes":"Private no");}',
                'function second(){var text=de(node,!0);ae(()=>{ue(text,h(cross))})}',
            ]
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"quickadd","name":"QuickAdd","version":"2.25.0","description":"Quickly add new pages or content to your vault."}',
                bundle.encode("utf-8"),
            )
        )
        strings = {row["source"]: row for row in snapshot["strings"]}
        self.assertTrue(
            {"Add choice", "New choice", "Add folder", "New folder"}.issubset(strings)
        )
        self.assertNotIn("Internal enabled", strings)
        self.assertNotIn("Internal disabled", strings)
        self.assertNotIn("Private yes", strings)
        self.assertNotIn("Private no", strings)
        evidence = strings["New choice"]["evidence"][0]
        self.assertEqual(evidence["symbol"], "svelteReactiveText")
        self.assertNotIn("literal_start", evidence)

    def test_internal_new_prefixed_switch_is_not_choice_ui_copy(self) -> None:
        bundle = 'function internalName(kind) { switch (kind) { case "Template": return "New template"; case "Capture": return "New capture"; case "Macro": return "New macro"; } } const internal = internalName("Template");'
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"quickadd","name":"QuickAdd","version":"2.25.0","description":"Quickly add new pages or content to your vault."}',
                bundle.encode("utf-8"),
            )
        )
        self.assertNotIn("New template", {row["source"] for row in snapshot["strings"]})

    def test_children_only_ast_or_jsx_name_is_not_a_setting_label(self) -> None:
        bundle = (
            'new MathNode({ name: "mi", attributes: {}, children: [], value: "x" });'
            'jsx(Dropdown, { name: "months", children: options, value: currentMonth });'
            'mount(row, { name: "Visible setting", control: value => value });'
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        sources = {row["source"] for row in snapshot["strings"]}
        self.assertNotIn("mi", sources)
        self.assertNotIn("months", sources)
        self.assertIn("Visible setting", sources)

    def test_name_and_control_need_a_component_call(self) -> None:
        bundle = (
            'const internal = { name: "Internal control key", control: value => value };'
            'const hidden = { name: "Network config key", control: { type: "network", key: "network" } };'
            'const items = [{ name: "Typed setting", desc: "Visible setting description", control: { type: "toggle", key: "enabled" } }];'
            'mount(row, { name: "Visible setting", control: value => value });'
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        sources = {row["source"] for row in snapshot["strings"]}
        self.assertNotIn("Internal control key", sources)
        self.assertNotIn("Network config key", sources)
        self.assertIn("Typed setting", sources)
        self.assertIn("Visible setting description", sources)
        self.assertIn("Visible setting", sources)

    def test_arbitrary_helper_first_literal_is_not_settings_copy(self) -> None:
        bundle = 'const group = { type: "group", heading: "Visible group", items: [{ name: "Visible setting", desc: internalHelper("Internal configuration key", ref), control: { type: "toggle" } }] };'
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        sources = {row["source"] for row in snapshot["strings"]}
        self.assertIn("Visible setting", sources)
        self.assertNotIn("Internal configuration key", sources)

    def test_shadowed_choice_factory_name_is_not_ui_proof(self) -> None:
        bundle = 'function choiceName(kind) { switch (kind) { case "Template": return "New template"; case "Capture": return "New capture"; case "Macro": return "New macro"; } } function choiceName(other) { return other; } editor.onAddChoice(choiceName(kind), kind);'
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"quickadd","name":"QuickAdd","version":"2.25.0","description":"Quickly add new pages or content to your vault."}',
                bundle.encode("utf-8"),
            )
        )
        self.assertNotIn("New template", {row["source"] for row in snapshot["strings"]})

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
