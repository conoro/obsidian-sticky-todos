import importlib.util
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("obsidian_todos_sync.py")
SPEC = importlib.util.spec_from_file_location("obsidian_todos_sync", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(sync)


class MarkdownTests(unittest.TestCase):
    def test_renders_basic_inline_markdown(self):
        spans = sync.parse_inline_markdown(
            "Use **bold**, *italic*, `code`, and ~~old~~."
        )
        rendered = [(span.text, span.styles) for span in spans]
        self.assertIn(("bold", ("bold",)), rendered)
        self.assertIn(("italic", ("italic",)), rendered)
        self.assertIn(("code", ("code",)), rendered)
        self.assertIn(("old", ("strike",)), rendered)
        self.assertEqual(
            "".join(span.text for span in spans),
            "Use bold, italic, code, and old.",
        )

    def test_renders_markdown_wiki_and_bare_links(self):
        spans = sync.parse_inline_markdown(
            "[site](https://example.com/a#part), "
            "[[Projects/Plan#Next steps|the plan]], "
            "and https://example.org/docs#intro."
        )
        links = [(span.text, span.target) for span in spans if span.target]
        self.assertEqual(links, [
            ("site", "https://example.com/a#part"),
            ("the plan", "Projects/Plan#Next steps"),
            ("https://example.org/docs#intro", "https://example.org/docs#intro"),
        ])

    def test_relative_link_and_fragment_become_encoded_obsidian_url(self):
        source = Path("/vault/lists/TODO.md")
        url = sync.obsidian_url("../Projects/Plan.md#Next steps", source)
        self.assertEqual(
            url,
            "obsidian://open?path=%2Fvault%2FProjects%2FPlan.md%23Next%20steps",
        )

    def test_fragment_only_link_targets_the_todo_file(self):
        source = Path("/vault/TODO.md")
        url = sync.obsidian_url("#Urgent", source)
        self.assertEqual(
            url,
            "obsidian://open?path=%2Fvault%2FTODO.md%23Urgent",
        )

    def test_external_urls_are_unchanged_and_unknown_schemes_are_rejected(self):
        self.assertEqual(
            sync.obsidian_url("https://example.com/a#b"),
            "https://example.com/a#b",
        )
        self.assertIsNone(sync.obsidian_url("javascript:alert(1)"))

    @patch.object(sync.webbrowser, "open", return_value=True)
    def test_open_link_opens_resolved_url(self, browser_open):
        self.assertTrue(sync.open_link("Note.md#Heading", Path("/vault/TODO.md")))
        browser_open.assert_called_once_with(
            "obsidian://open?path=%2Fvault%2FNote.md%23Heading"
        )


class TodoMovementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        vault = Path(self.directory.name)
        self.todo = vault / "TODO.md"
        self.done = vault / "DONE.md"

    def tearDown(self):
        self.directory.cleanup()

    def test_moves_checked_tasks_and_dates_only_undated_entries(self):
        self.todo.write_text(
            "---\ncreated: earlier\n---\n\n"
            "- [ ] Keep me\n"
            "- [x] Archive me\n"
            "- [X] Already dated ✅ 2026-08-01\n"
            "ordinary text\n",
            encoding="utf-8",
        )
        self.done.write_text("# Done\n- [x] Existing ✅ 2026-07-01\n", encoding="utf-8")

        count = sync.move_completed_todos(
            self.todo, self.done, date(2026, 8, 5)
        )

        self.assertEqual(count, 2)
        self.assertEqual(
            self.todo.read_text(encoding="utf-8"),
            "---\ncreated: earlier\n---\n\n- [ ] Keep me\nordinary text\n",
        )
        self.assertEqual(
            self.done.read_text(encoding="utf-8"),
            "# Done\n- [x] Existing ✅ 2026-07-01\n"
            "- [x] Archive me ✅ 2026-08-05\n"
            "- [X] Already dated ✅ 2026-08-01\n",
        )

    def test_creates_done_file_and_separates_missing_final_newline(self):
        self.todo.write_text("- [x] First", encoding="utf-8")

        self.assertEqual(
            sync.move_completed_todos(self.todo, self.done, date(2026, 8, 5)), 1
        )
        self.assertEqual(self.todo.read_text(encoding="utf-8"), "")
        self.assertEqual(
            self.done.read_text(encoding="utf-8"),
            "- [x] First ✅ 2026-08-05\n",
        )

    def test_no_checked_tasks_leaves_files_untouched(self):
        self.todo.write_text("- [ ] Pending\n", encoding="utf-8")
        before = self.todo.stat().st_mtime_ns

        self.assertEqual(sync.move_completed_todos(self.todo, self.done), 0)

        self.assertEqual(self.todo.stat().st_mtime_ns, before)
        self.assertFalse(self.done.exists())

    def test_preserves_crlf_line_endings(self):
        with self.todo.open("w", encoding="utf-8", newline="") as file:
            file.write("- [ ] Pending\r\n- [x] Finished\r\n")
        with self.done.open("w", encoding="utf-8", newline="") as file:
            file.write("# Done\r\n")

        sync.move_completed_todos(self.todo, self.done, date(2026, 8, 5))

        self.assertEqual(sync._read_text(self.todo), "- [ ] Pending\r\n")
        self.assertEqual(
            sync._read_text(self.done),
            "# Done\r\n- [x] Finished ✅ 2026-08-05\r\n",
        )

    def test_widget_completion_moves_exact_task_as_checked(self):
        self.todo.write_text(
            "- [ ] First\n- [ ] **Second** [note](Plan.md#Next)\n",
            encoding="utf-8",
        )
        self.done.write_text("# Done\n", encoding="utf-8")
        document = sync.read_todo_document(self.todo)

        result = sync.complete_todo(
            document.unchecked[1], document.source,
            self.todo, self.done, date(2026, 8, 5),
        )

        self.assertIsNone(result)
        self.assertEqual(self.todo.read_text(encoding="utf-8"), "- [ ] First\n")
        self.assertEqual(
            self.done.read_text(encoding="utf-8"),
            "# Done\n- [x] **Second** [note](Plan.md#Next) ✅ 2026-08-05\n",
        )

    def test_widget_completion_distinguishes_duplicate_tasks(self):
        self.todo.write_text(
            "intro\n- [ ] Duplicate\nmiddle\n- [ ] Duplicate\n",
            encoding="utf-8",
        )
        document = sync.read_todo_document(self.todo)

        sync.complete_todo(
            document.unchecked[1], document.source,
            self.todo, self.done, date(2026, 8, 5),
        )

        self.assertEqual(
            self.todo.read_text(encoding="utf-8"),
            "intro\n- [ ] Duplicate\nmiddle\n",
        )
        self.assertEqual(
            self.done.read_text(encoding="utf-8"),
            "- [x] Duplicate ✅ 2026-08-05\n",
        )

    def test_widget_completion_refuses_stale_obsidian_snapshot(self):
        self.todo.write_text("- [ ] Original\n", encoding="utf-8")
        self.done.write_text("# Done\n", encoding="utf-8")
        document = sync.read_todo_document(self.todo)
        self.todo.write_text(
            "- [ ] Original\n- [ ] Added in Obsidian\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(sync.TodoConflictError, "changed in Obsidian"):
            sync.complete_todo(
                document.unchecked[0], document.source,
                self.todo, self.done, date(2026, 8, 5),
            )

        self.assertEqual(
            self.todo.read_text(encoding="utf-8"),
            "- [ ] Original\n- [ ] Added in Obsidian\n",
        )
        self.assertEqual(self.done.read_text(encoding="utf-8"), "# Done\n")


class ExtractionTests(unittest.TestCase):
    def test_extracts_task_text_for_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            todo = Path(directory) / "TODO.md"
            todo.write_text(
                "- [ ] **Read** [docs](Guide.md#Start)\n"
                "- [x] Finished ✅ 2026-08-05\n",
                encoding="utf-8",
            )
            self.assertEqual(
                sync.extract_todos(todo),
                (["• **Read** [docs](Guide.md#Start)"], ["• Finished"]),
            )

    def test_document_tracks_exact_source_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            todo = Path(directory) / "TODO.md"
            todo.write_text(
                "heading\n  - [ ] Nested task\n- [x] Finished\n",
                encoding="utf-8",
            )

            document = sync.read_todo_document(todo)

            self.assertEqual(document.source, todo.read_text(encoding="utf-8"))
            self.assertEqual(
                document.unchecked,
                (sync.TodoItem("Nested task", "  - [ ] Nested task\n", 1),),
            )
            self.assertEqual(
                document.checked,
                (sync.TodoItem("Finished", "- [x] Finished\n", 2),),
            )


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "config.toml"

    def tearDown(self):
        self.directory.cleanup()

    def test_missing_default_config_gives_defaults(self):
        with patch.dict(sync.os.environ, {"OBSIDIAN_STICKY_CONFIG": str(self.path)}):
            config = sync.load_config()
        self.assertEqual(config, sync.Config())
        self.assertEqual(config.done_path, config.todo_path.with_name("DONE.md"))

    def test_missing_explicit_config_is_an_error(self):
        with self.assertRaisesRegex(sync.ConfigError, "not found"):
            sync.load_config(self.path)

    def test_loads_values_and_nested_tables(self):
        self.path.write_text(
            'todo_file = "~/vault/Tasks.md"\n'
            "width = 400\nopacity = 1\nanchor = \"bottom-left\"\n"
            "[colors]\nbackground = \"#ffffff\"\n"
            "[fonts]\nsize = 14\n",
            encoding="utf-8",
        )
        config = sync.load_config(self.path)
        self.assertEqual(config.todo_path, Path.home() / "vault/Tasks.md")
        self.assertEqual(config.done_path, Path.home() / "vault/DONE.md")
        self.assertEqual(config.width, 400)
        self.assertEqual(config.opacity, 1.0)
        self.assertEqual(config.colors.background, "#ffffff")
        self.assertEqual(config.colors.header, sync.Colors().header)
        self.assertEqual(config.fonts.size, 14)

    def test_rejects_unknown_and_invalid_settings(self):
        for body, message in (
            ("colour = 1\n", "unknown"),
            ("[colors]\nbg = \"#fff\"\n", "unknown"),
            ("width = \"wide\"\n", "number"),
            ("anchor = \"middle\"\n", "anchor"),
            ("opacity = 3\n", "opacity"),
            ("todo_file = \n", "config.toml"),
        ):
            with self.subTest(body=body):
                self.path.write_text(body, encoding="utf-8")
                with self.assertRaisesRegex(sync.ConfigError, message):
                    sync.load_config(self.path)

    def test_example_config_is_valid_and_matches_defaults(self):
        example = SCRIPT.with_name("config.example.toml")
        config = sync.load_config(example)
        defaults = sync.Config()
        self.assertEqual(config.width, defaults.width)
        self.assertEqual(config.colors, defaults.colors)


class PositionTests(unittest.TestCase):
    def test_anchors(self):
        config = sync.Config(width=300, offset_x=10, offset_y=20)
        cases = {
            "top-left": (10, 20),
            "top-right": (1000 - 300 - 10, 20),
            "bottom-left": (10, 800 - 100 - 20),
            "bottom-right": (690, 680),
        }
        for anchor, expected in cases.items():
            with self.subTest(anchor=anchor):
                self.assertEqual(
                    sync.initial_position(
                        sync.replace(config, anchor=anchor), 1000, 800, 100,
                    ),
                    expected,
                )

    def test_position_round_trip_and_corrupt_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "nested" / "position.json"
            self.assertIsNone(sync.load_saved_position(state))
            sync.save_position(12, 34, state)
            self.assertEqual(sync.load_saved_position(state), (12, 34))
            state.write_text("not json", encoding="utf-8")
            self.assertIsNone(sync.load_saved_position(state))


if __name__ == "__main__":
    unittest.main()
