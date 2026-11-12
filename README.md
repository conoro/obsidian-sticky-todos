# Obsidian Sticky

A floating yellow post-it on your desktop that shows the open tasks in an
[Obsidian](https://obsidian.md) `TODO.md` note.

- Tick a task on the note, or in Obsidian, and the line moves to `DONE.md`
  with a `✅ YYYY-MM-DD` completion date.
- Bold, italic, `code`, ~~strike~~, links, `[[wiki links]]` and bare URLs are
  rendered. Clicking a note link opens it in Obsidian.
- Drag the note anywhere. It reopens where you left it.
- Stays above other windows. On Hyprland it is pinned to every workspace, and
  on macOS it shows on every Space.
- Safe with Obsidian and sync tools: writes are atomic, and if `TODO.md`
  changes under it, it refreshes instead of guessing.

It's a single Python file with no dependencies beyond tkinter. PyObjC is
optional, on macOS only.

## Install

```bash
git clone https://github.com/conoro/obsidian-sticky-todos.git
cd obsidian-sticky-todos
./install.sh --todo-file ~/path/to/vault/TODO.md
```

The installer:

1. checks for Python 3.11+ with tkinter;
2. copies the script to `~/.local/bin/obsidian-sticky`;
3. creates `~/.config/obsidian-sticky/config.toml` if it doesn't exist (it never overwrites one);
4. sets the note to start on login. On Linux that's a systemd user service; on macOS it's a LaunchAgent.

`./install.sh --uninstall` removes it and keeps your config.

### Requirements

| Platform | Needs |
|----------|-------|
| Omarchy / Arch | `tk` (`omarchy pkg add tk` or `sudo pacman -S tk`) |
| Debian / Ubuntu / Fedora | `python3-tk` / `python3-tkinter` |
| macOS | Homebrew `python@3.13` + `python-tk@3.13` (the installer adds PyObjC) |

### Linux / Wayland notes

The note runs through XWayland. On **Hyprland** (including Omarchy) it adds its
own window rule at startup, so it floats, has no border and is pinned to every
workspace, and you don't have to edit your Hyprland config. If you'd rather keep
the rule in your config, add this to `~/.config/hypr/hyprland.lua`:

```lua
hl.window_rule({ match = { class = "^Obsidian-sticky$" },
  float = true, pin = true, no_initial_focus = true, border_size = 0 })
```

On other Wayland compositors, make a window rule for the class
`Obsidian-sticky` that floats it (and keeps it on top, if you want that). On
X11 the note is a frameless window and needs no setup.

## Configure

Edit `~/.config/obsidian-sticky/config.toml`, then restart the note. Every
setting is optional. [`config.example.toml`](config.example.toml) lists them
all with their defaults:

```toml
todo_file = "~/Obsidian/TODO.md"
width = 360
anchor = "top-right"     # first position; after that, wherever you drag it
remember_position = true

[colors]
background = "#FEFBB3"
header = "#F0E040"
```

You can also pass `--config PATH` or set `OBSIDIAN_STICKY_CONFIG`.
`--todo-file PATH` overrides the note path for one run, and `--reset-position`
forgets where you dragged the note.

```bash
systemctl --user restart obsidian-sticky                 # Linux
launchctl kickstart -k gui/$(id -u)/local.obsidian-sticky # macOS
```

## Develop

```bash
python3 -m unittest -v          # no display needed
python3 obsidian_todos_sync.py --todo-file /tmp/TODO.md
```

See [AGENTS.md](AGENTS.md) for how the code is laid out.

## License

MIT
