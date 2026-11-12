# AGENTS.md

Guidance for AI coding agents (and humans) working on this repo.

## What this is

Obsidian Sticky: a floating post-it desktop widget that shows the tasks in an
Obsidian `TODO.md` note. It's a single-file Python/tkinter app for Linux
(X11, and Wayland via XWayland, with first-class Hyprland/Omarchy support) and
macOS. It uses only the standard library; PyObjC is optional on macOS.

- Unchecked tasks (`- [ ] ...`) get a clickable checkbox. Ticking one in the
  widget, or ticking it in Obsidian, moves the line from `TODO.md` to `DONE.md`
  and appends ` ✅ YYYY-MM-DD`.
- Inline Markdown is rendered: bold, italic, code, strike, links, wiki links
  and bare URLs. Vault-relative links become `obsidian://open?path=...`.
  Unknown URI schemes are refused.
- Drag the header or body to move the note. The drop point is saved to
  `$XDG_STATE_HOME/obsidian-sticky/position.json`.

## Files

| File | Purpose |
|------|---------|
| `obsidian_todos_sync.py` | The whole app: config loading, Markdown span parser, TODO/DONE file logic, `StickyWidget` UI, `main()` CLI |
| `test_obsidian_todos_sync.py` | `unittest` suite (no display needed) |
| `config.example.toml` | Every setting with its default. A test checks that it loads and matches `Config()` |
| `install.sh` | Cross-platform installer. It generates the systemd user unit (Linux) or the LaunchAgent plist (macOS) with the user's paths at install time |

The installed layout is `~/.local/bin/obsidian-sticky` (a copy of the script),
`~/.config/obsidian-sticky/config.toml`, and
`~/.config/systemd/user/obsidian-sticky.service` or
`~/Library/LaunchAgents/local.obsidian-sticky.plist`.

## Commands

```bash
python3 -m unittest -v                                  # run tests
python3 obsidian_todos_sync.py --todo-file /tmp/TODO.md # run against a scratch note
bash -n install.sh                                      # syntax-check installer
```

Python 3.11+ is required (`tomllib`, `X | None` annotations).

## Architecture and invariants

- **Keep pure logic UI-free.** `load_config`, `initial_position`,
  `parse_inline_markdown`, `obsidian_url`, `read_todo_document`,
  `move_completed_todos` and `complete_todo` don't touch tkinter and are
  unit-tested. Only `StickyWidget` does UI work.
- **File safety is the core invariant.** Writes are atomic (`tempfile` +
  `os.replace`, with permission bits preserved). Line endings are preserved
  (`newline=""`). Every widget action re-checks that `TODO.md` still matches
  the snapshot the user saw, and on a mismatch raises `TodoConflictError` and
  rolls back `DONE.md`. Never "guess" which line to edit after a concurrent
  Obsidian or sync edit. `DONE.md` is written first and restored if replacing
  `TODO.md` fails.
- **Config.** `Config`, `Colors` and `Fonts` are frozen dataclasses. TOML keys
  map 1:1 to their fields, and `_build` rejects unknown keys and wrong types.
  To add a setting: add the field with a default, add it to
  `config.example.toml`, and test it. Lookup order: `--config`, then
  `$OBSIDIAN_STICKY_CONFIG`, then `$XDG_CONFIG_HOME/obsidian-sticky/config.toml`.
  If the default file is missing, the defaults are used.
- **Windowing per platform.**
  - macOS: `overrideredirect` frameless window, raised to
    `NSFloatingWindowLevel` on all Spaces via PyObjC.
  - X11: `overrideredirect` frameless window plus `-topmost`.
  - Wayland (`WAYLAND_DISPLAY` set): a *managed* window, because the
    compositor can't apply rules to override-redirect windows. The Tk
    `className` is `obsidian-sticky`, so WM_CLASS is `Obsidian-sticky`.
    On Hyprland, `register_hyprland_rule()` runs
    `hyprctl eval 'hl.window_rule({...float, pin...})'` before the window
    maps. That rule survives `hyprctl reload`. Hyprland honours Tk's own
    `geometry()` moves for floating XWayland windows, which is what makes
    header dragging work.
- Tk cursor names differ: `pointinghand` exists only on macOS and raises
  `TclError` on X11, where the app uses `hand2`.
- Saving the drag position uses pointer coordinates, not `winfo_x()`, because
  `winfo_x()` lags behind the compositor at button release.

## Conventions

- No hard-coded personal paths, usernames or vault locations anywhere in the
  repo. Defaults use `~` and XDG paths. The installer writes the user's real
  paths only into files outside the repo.
- Keep it single-file and stdlib-only.
- On Omarchy, never edit `/usr/share/omarchy/`. User Hyprland config lives in
  `~/.config/hypr/`. After changing it, validate with `hyprctl reload` and then
  `hyprctl configerrors`.
