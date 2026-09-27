from __future__ import annotations

import base64
import json
import unittest
import zlib

from adapters.obsidian.adapter_worker import _decode_js_literal, build_snapshot


class ObsidianAdapterWorkerTests(unittest.TestCase):
    def test_static_visible_dom_attributes_do_not_harvest_data_keys(self) -> None:
        bundle = "\n".join(
            [
                'const button=document.createElement("button");',
                'button.setAttribute("aria-label","Open choices");',
                'button.setAttribute("title","Show selected note");',
                'button.setAttribute("data-key","Internal configuration key");',
                'button.setAttribute("aria-label","PKMer 插件市场");',
                'button.setAttribute(dynamicName,"Dynamic internal value");',
                'config["setAttribute"]("aria-label","Unproven indexed call");',
            ]
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        sources = {row["source"] for row in snapshot["strings"]}
        self.assertTrue({"Open choices", "Show selected note"} <= sources)
        self.assertFalse(
            {"Internal configuration key", "PKMer 插件市场", "Dynamic internal value", "Unproven indexed call"}
            & sources
        )

    def test_bundled_jsx_native_visible_attributes_skip_unproven_factories(self) -> None:
        bundle = "\n".join(
            [
                '(0,Bo.jsx)("div", {className:"nn-shortcuts-resize-handle",role:"separator","aria-label":"Resize pinned shortcuts"});',
                '(0,Bo.jsxs)("button", {title:"Open pinned shortcuts",children:"Pinned shortcuts"});',
                '(0,Bo.jsx)("input", {placeholder:"Find files"});',
                '(0,Bo.jsx)("span", {"aria-hidden":"true",children:"Decorative glyph"});',
                '(0,Bo.jsx)("span", {"aria-hidden":true,children:"Decorative boolean"});',
                '(0,Bo.jsx)("span", {"aria-hidden":!0,children:"Decorative minified"});',
                '(0,Bo.jsx)(Dropdown, {title:"Component config title",name:"months"});',
                '(0,Bo.jsx)("script", {title:"Script payload"});',
                '(0,Bo.jsx)("div", {"aria-label":getLabel(),"data-title":"Internal metadata"});',
                'Bo.jsx("div", {"aria-label":"Unproven direct factory"});',
            ]
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        strings = {row["source"]: row for row in snapshot["strings"]}
        self.assertTrue(
            {"Resize pinned shortcuts", "Open pinned shortcuts", "Pinned shortcuts", "Find files"}
            <= strings.keys()
        )
        for rejected in (
            "Component config title", "months", "Script payload", "Internal metadata", "Unproven direct factory",
            "Decorative glyph", "Decorative boolean", "Decorative minified"
        ):
            self.assertNotIn(rejected, strings)
        self.assertEqual(strings["Resize pinned shortcuts"]["evidence"][0]["symbol"], "aria-label")

    def test_plugin_setting_tab_descriptors_and_proven_helper_labels(self) -> None:
        bundle = "\n".join(
            [
                'class Settings extends Obsidian.PluginSettingTab {',
                'getSettingDefinitions(){return [{name:"Help",desc:"Read the documentation.",render:setting=>setting.addButton()},',
                '{type:"group",heading:"Typography",items:[this.sliderSetting("Small font size","Text in sidebars and tabs.","fontSize"),this.internal("Internal network key","Private configuration value")]}]}',
                'sliderSetting(name,desc,key){return {name:name,desc:desc,render:setting=>setting.addSlider()}}',
                'internal(name,desc){return {name:name,desc:desc,key:"private"}}',
                '}',
                'class Other {getSettingDefinitions(){return [{name:"Internal title",desc:"Private description",render:setting=>setting.addButton()}]}}',
            ]
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        strings = {row["source"]: row for row in snapshot["strings"]}
        self.assertTrue(
            {"Help", "Read the documentation.", "Typography", "Small font size", "Text in sidebars and tabs."}
            <= strings.keys()
        )
        for excluded in (
            "Internal network key",
            "Private configuration value",
            "Internal title",
        ):
            self.assertNotIn(excluded, strings)
        self.assertEqual(
            strings["Small font size"]["evidence"][0]["symbol"],
            "pluginSettingTabHelper",
        )

    def test_plugin_setting_tab_rejects_nested_or_shadowed_helper_return(self) -> None:
        bundle = "\n".join(
            [
                'class Settings extends Obsidian.PluginSettingTab {',
                'getSettingDefinitions(){return [{type:"group",heading:"Private group",items:[this.sliderSetting("Private slider","Internal network value","key")]}]}',
                'sliderSetting(name,desc){const nested=()=>{return {name:name,desc:desc,render:x=>x}};return {key:name}}',
                'sliderSetting(name,desc){return {name:name,desc:desc,render:x=>x}}',
                '}',
            ]
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        sources = {row["source"] for row in snapshot["strings"]}
        self.assertFalse(
            {"Private group", "Private slider", "Internal network value"} & sources
        )
    def test_svelte_dom_attributes_require_a_proven_static_sink(self) -> None:
        bundle = "\n".join(
            [
                'function attr(node,attribute,value){if(value==null)node.removeAttribute(attribute);else node.setAttribute(attribute,value)}',
                'function create(){button=element("button");attr(button,"aria-label","Move Status Bar Item Down");attr(button,"title","Remove Status Bar Item");attr(button,"data-key","Internal configuration key")}',
                'function internal(node,attribute,value){store.set(attribute,value)}',
                'internal(button,"aria-label","Internal command key");',
                'attr(button,"aria-label",dynamicLabel);',
            ]
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        strings = {row["source"]: row for row in snapshot["strings"]}
        self.assertIn("Move Status Bar Item Down", strings)
        self.assertIn("Remove Status Bar Item", strings)
        self.assertNotIn("Internal configuration key", strings)
        self.assertNotIn("Internal command key", strings)
        self.assertEqual(
            strings["Move Status Bar Item Down"]["evidence"][0]["symbol"],
            "svelteDomAttribute",
        )

    def test_shadowed_svelte_attribute_helper_is_not_ui_proof(self) -> None:
        bundle = 'function attr(node,name,value){node.setAttribute(name,value)} function attr(node,name,value){store.set(name,value)} attr(button,"aria-label","Private workflow key");'
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                bundle.encode("utf-8"),
            )
        )
        self.assertNotIn(
            "Private workflow key", {row["source"] for row in snapshot["strings"]}
        )

    def test_svelte_return_labels_require_instance_slot_and_text_node(self) -> None:
        helper = 'function metricToString(kind){switch(kind){case 1:return "Words in Note";case 2:return "Chars in Note";case 3:return "Total Notes";default:return "Select Options"}}'
        bound = 'function instance(){return [plugin,items,altItems,metricToString]}'
        visible = 'function block(ctx){let label=/*metricToString*/ ctx[3](ctx[0])+"";let node;return {c(){node=text(label)}}}'

        def sources(bundle: str) -> dict[str, dict[str, object]]:
            snapshot = json.loads(
                build_snapshot(
                    b'{"id":"example-plugin","name":"Example Plugin","version":"1.0.0","description":"Example description."}',
                    bundle.encode("utf-8"),
                )
            )
            return {row["source"]: row for row in snapshot["strings"]}

        accepted = sources("\n".join((helper, bound, visible)))
        self.assertTrue(
            {"Words in Note", "Chars in Note", "Total Notes", "Select Options"}
            <= accepted.keys()
        )
        self.assertEqual(
            accepted["Words in Note"]["evidence"][0]["symbol"], "svelteReturnText"
        )
        for hidden in (
            'function block(ctx){let label=/*metricToString*/ ctx[3](ctx[0])+"";log(label)}',
            'function block(ctx){let label=/*metricToString*/ ctx[2](ctx[0])+"";let node;return {c(){node=text(label)}}}',
            'function block(ctx){let label=/*metricToString*/ ctx[3](ctx[0])+"";log(label)} function other(){let node=text(label)}',
        ):
            self.assertNotIn("Words in Note", sources("\n".join((helper, bound, hidden))))
        self.assertNotIn("Words in Note", sources("\n".join((helper, helper, bound, visible))))
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

    def test_locale_registry_follows_bounded_dictionary_aliases(self) -> None:
        bundle = (
            'var english={"Tag sort order":"Tag sort order","Set an explicit sort order for the specified tags.":"Set an explicit sort order for the specified tags.","Add tag":"Add tag"},enAlias=english;'
            'var zh={"Tag sort order":"标签排序","Set an explicit sort order for the specified tags.":"指定标签的排序顺序。","Add tag":"添加标签"},zhAlias=zh;'
            'var de={"Tag sort order":"Tag-Sortierung","Set an explicit sort order for the specified tags.":"Tags sortieren.","Add tag":"Tag hinzufügen"};'
            'var locales={de:de,en:enAlias,zh:zhAlias};'
            'var internal={"Debug worker":"Debug worker"};'
        )
        manifest = b'{"id":"obsidian-kanban","name":"Kanban","version":"2.0.51","description":"Boards."}'
        snapshot = json.loads(build_snapshot(manifest, bundle.encode()))
        strings = {row["source"]: row for row in snapshot["strings"]}
        self.assertIn("Tag sort order", strings)
        self.assertTrue(strings["Tag sort order"]["evidence"][0]["symbol"].startswith("locale:en"))
        self.assertNotIn("Debug worker", strings)
        self.assertTrue(any(row["locale"] == "zh-CN" for row in snapshot["native_locale_coverage"]))

        dates = 'var en={month:"April",day:"Monday",year:"Year"},fr={month:"Avril",day:"Lundi",year:"Année"},es={month:"Abril",day:"Lunes",year:"Año"},dateLocales={en:en,fr:fr,es:es};'
        combined = json.loads(build_snapshot(manifest, (bundle + dates).encode()))
        self.assertTrue({"Tag sort order", "April"} <= {row["source"] for row in combined["strings"]})

        cycle = bundle.replace("enAlias=english", "enAlias=other,other=enAlias")
        rejected = json.loads(build_snapshot(manifest, cycle.encode()))
        self.assertNotIn("Tag sort order", {row["source"] for row in rejected["strings"]})

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
        self.assertEqual(snapshot["contract_revision"], 38)
        self.assertEqual(snapshot["parser"], "obsidian-plugin-ui-structured-v38")
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

    def test_indexed_error_bag_requires_proven_dom_text_helper(self) -> None:
        warning = "Pandoc is not installed or accessible on your PATH. This plugin's functionality will be limited."
        positive = (
            f'this.errorMessages = {{ pandoc: {json.dumps(warning)}, latex: "LaTeX is not installed." }};'
            'const createError = (text) => containerEl.createEl("p", { cls: "plugin-error", text });'
            'createError(this.errorMessages[binary]);'
            'this.internalMessages = { secret: "Internal diagnostic message" };'
        )
        manifest = b'{"id":"obsidian-pandoc","name":"Pandoc Plugin","version":"0.4.1","description":"Export with Pandoc."}'
        strings = {row["source"]: row for row in json.loads(build_snapshot(manifest, positive.encode()))["strings"]}
        self.assertEqual(strings[warning]["evidence"][0]["symbol"], "indexedErrorMessage")
        self.assertIn("LaTeX is not installed.", strings)
        self.assertNotIn("Internal diagnostic message", strings)

        negative = (
            f'this.errorMessages = {{ pandoc: {json.dumps(warning)} }};'
            'const createError = (text) => console.error(text);'
            'createError(this.errorMessages[binary]);'
        )
        rejected = {row["source"] for row in json.loads(build_snapshot(manifest, negative.encode()))["strings"]}
        self.assertNotIn(warning, rejected)

    def test_long_settings_description_requires_proven_ui_context(self) -> None:
        description = 'List/object values from scripts are always written as proper Obsidian properties (a list becomes a List). This toggle additionally converts string values into typed properties: a comma or bullet-list string becomes a List, "42" becomes a Number, "true" becomes a Checkbox, etc. Disabled by default; the string conversion is a beta heuristic that may have edge cases.'
        self.assertGreater(len(description), 300)
        self.assertLess(len(description), 512)
        manifest = b'{"id":"quickadd","name":"QuickAdd","version":"2.27.0","description":"Add content."}'
        bundle = f'const group={{type:"group",heading:"Templates & properties",items:[{{name:"Convert values",desc:{json.dumps(description)}}}]}};'
        snapshot = json.loads(build_snapshot(manifest, bundle.encode()))
        self.assertIn(description, {row["source"] for row in snapshot["strings"]})
        unproven = json.loads(build_snapshot(manifest, f'const internal={{description:{json.dumps(description)}}};'.encode()))
        self.assertNotIn(description, {row["source"] for row in unproven["strings"]})
        over_limit = "A" * 513
        rejected = json.loads(build_snapshot(manifest, f'setting.setDesc({json.dumps(over_limit)});'.encode()))
        self.assertNotIn(over_limit, {row["source"] for row in rejected["strings"]})

    def test_linked_settings_copy_requires_text_node_and_link_label_sinks(self) -> None:
        lead = "Collect a choice's inputs in one form before it runs, instead of one prompt at a time."
        label = "Learn more about one-page inputs"
        manifest = b'{"id":"quickadd","name":"QuickAdd","version":"2.27.0","description":"Add content."}'
        bundle = ";".join((
            'function link(parent,url,label){let a=parent.createEl("a");a.textContent=label;a.href=url;parent.append(a);return a}',
            'function linked(lead,url,label="Learn more"){let fragment=createFragment();return fragment.append(document.createTextNode(lead)),link(fragment,url,label),fragment}',
            f'const group={{type:"group",heading:"Input",items:[{{name:"One-page input for choices",desc:linked({json.dumps(lead)},docs.onePage,{json.dumps(label)}),control:{{type:"toggle"}}}}]}}',
            'function internal(lead,url,label){return console.log(lead,url,label)}',
            'const other={type:"group",heading:"Other",items:[{name:"Internal",desc:internal("Internal key",docs.other,"Internal link"),control:{type:"toggle"}}]}',
        ))
        snapshot = json.loads(build_snapshot(manifest, bundle.encode()))
        strings = {row["source"]: row for row in snapshot["strings"]}
        self.assertIn(lead, strings)
        self.assertIn(label, strings)
        self.assertEqual(strings[lead]["evidence"][0]["symbol"], "settingsLinkedFragment")
        self.assertNotIn("Internal key", strings)
        self.assertNotIn("Internal link", strings)

    def test_es_unicode_code_point_escape_matches_client_and_rejects_invalid_scalars(self) -> None:
        self.assertEqual(
            _decode_js_literal(r'"Text and Highlight Colors \u{1F9EA}"'),
            "Text and Highlight Colors 🧪",
        )
        self.assertEqual(_decode_js_literal(r'"\uD83D\uDE80 Launch"'), "🚀 Launch")
        for literal in (r'"\u{}"', r'"\u{D800}"', r'"\u{110000}"', r'"\u{1234567}"', r'"\uD83D"'):
            self.assertIsNone(_decode_js_literal(literal))
        snapshot = json.loads(build_snapshot(
            b'{"id":"make-md","name":"make.md","version":"1.3.5","description":"Organize notes."}',
            r'setting.setName("Text and Highlight Colors \u{1F9EA}");'.encode(),
        ))
        self.assertIn("Text and Highlight Colors 🧪", {row["source"] for row in snapshot["strings"]})

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

    def test_immutable_linked_settings_description_has_complete_variants(self) -> None:
        bundle = "\n".join(
            [
                'var packageIntro="Bundle or import QuickAdd automations as reusable packages.";',
                'const group={type:"group",heading:"Choices & packages",items:[{name:"Packages",desc:packageIntro,render:x=>x}]};',
                'function packageDesc(empty){return this.descWithDocsLink(empty?`${packageIntro} Export becomes available once you have a choice. `:`${packageIntro} `,docs,"Learn more about packages")}',
                'var internal="Internal connection setting";',
                'function notVisible(empty){return this.descWithDocsLink(empty?`${internal} enabled`:`${internal} disabled`,docs)}',
                'var mutable="Mutable description";mutable="Changed description";',
                'const other={type:"group",heading:"Other",items:[{name:"Mutable",desc:mutable,render:x=>x}]};',
                'function changed(empty){return this.descWithDocsLink(empty?`${mutable} first`:`${mutable} second`,docs)}',
            ]
        )
        snapshot = json.loads(
            build_snapshot(
                b'{"id":"quickadd","name":"QuickAdd","version":"2.25.0","description":"Quickly add new pages or content to your vault."}',
                bundle.encode("utf-8"),
            )
        )
        strings = {row["source"]: row for row in snapshot["strings"]}
        full = "Bundle or import QuickAdd automations as reusable packages. Export becomes available once you have a choice."
        self.assertIn(full, strings)
        self.assertIn("Bundle or import QuickAdd automations as reusable packages.", strings)
        self.assertNotIn("Internal connection setting enabled", strings)
        self.assertNotIn("Mutable description first", strings)
        self.assertEqual(strings[full]["evidence"][0]["symbol"], "settingsComposedDocumentation")
        self.assertNotIn("literal_start", strings[full]["evidence"][0])

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
