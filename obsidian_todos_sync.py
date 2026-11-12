#!/usr/bin/env python3
"""
Obsidian Sticky — a floating post-it widget showing the tasks in an Obsidian
TODO.md note. Tick a task to move it to DONE.md. Drag the note to move it.

Works on macOS (LaunchAgent) and Linux, including Wayland compositors via
XWayland; on Hyprland (e.g. Omarchy) the note floats and is pinned to every
workspace. Settings live in a TOML file — see config.example.toml.
"""

import argparse
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
import tkinter.font as tkfont
import tomllib
from urllib.parse import quote, unquote, urlsplit
import webbrowser

IS_MAC = sys.platform == "darwin"
APP_ID = "obsidian-sticky"
# Tk capitalises the first letter of className for WM_CLASS ("Obsidian-sticky").
WM_CLASS = APP_ID[0].upper() + APP_ID[1:]

# Use PyObjC to set NSFloatingWindowLevel — unlike tkinter's -topmost,
# this keeps the window above all others even when Python loses focus.
HAS_PYOBJC = False
if IS_MAC:
    try:
        from AppKit import (
            NSApp,
            NSFloatingWindowLevel,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
        )
        HAS_PYOBJC = True
    except ImportError:
        pass


# ── Config ────────────────────────────────────────────────────────────────────
def _xdg_dir(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / fallback


CONFIG_PATH = _xdg_dir("XDG_CONFIG_HOME", ".config") / APP_ID / "config.toml"
STATE_PATH = _xdg_dir("XDG_STATE_HOME", ".local/state") / APP_ID / "position.json"
ANCHORS = ("top-right", "top-left", "bottom-right", "bottom-left")


@dataclass(frozen=True)
class Colors:
    background: str = "#FEFBB3"
    header: str = "#F0E040"
    text: str = "#2a2a2a"
    muted: str = "#777777"
    link: str = "#1756A9"
    code_background: str = "#F3EFA1"
    warning: str = "#A02B20"
    close_hover: str = "#cc0000"


@dataclass(frozen=True)
class Fonts:
    family: str = "Helvetica Neue" if IS_MAC else "Sans"
    mono_family: str = "Menlo" if IS_MAC else "Monospace"
    size: int = 13 if IS_MAC else 11
    header_size: int = 12 if IS_MAC else 10
    mono_size: int = 11 if IS_MAC else 10


@dataclass(frozen=True)
class Config:
    todo_file: Path = Path("~/Obsidian/TODO.md")
    done_file: Path | None = None          # default: DONE.md beside todo_file
    title: str = "◆ Obsidian TODOs"
    refresh_seconds: int = 60
    width: int = 360
    opacity: float = 0.95
    anchor: str = "top-right"              # where the note first appears
    offset_x: int = 40                     # px from the anchored side
    offset_y: int = 60
    remember_position: bool = True         # reopen where you last dropped it
    always_on_top: bool = True
    colors: Colors = field(default_factory=Colors)
    fonts: Fonts = field(default_factory=Fonts)

    @property
    def todo_path(self) -> Path:
        return self.todo_file.expanduser()

    @property
    def done_path(self) -> Path:
        if self.done_file is not None:
            return self.done_file.expanduser()
        return self.todo_path.with_name("DONE.md")


class ConfigError(ValueError):
    """Raised for an unreadable or invalid config file."""


def _build(cls, values: dict, section: str):
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(values) - set(known))
    if unknown:
        where = f"[{section}]" if section else "top level"
        raise ConfigError(f"unknown setting(s) at {where}: {', '.join(unknown)}")
    converted = {}
    for name, value in values.items():
        default = getattr(cls(), name)
        if isinstance(default, (Colors, Fonts)):
            if not isinstance(value, dict):
                raise ConfigError(f"[{name}] must be a table")
            value = _build(type(default), value, name)
        elif name in {"todo_file", "done_file"}:
            value = Path(str(value))
        elif isinstance(default, bool) and not isinstance(value, bool):
            raise ConfigError(f"{name} must be true or false")
        elif isinstance(default, (int, float)) and not isinstance(default, bool):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{name} must be a number")
            value = type(default)(value)
        elif isinstance(default, str) and not isinstance(value, str):
            raise ConfigError(f"{name} must be a string")
        converted[name] = value
    return replace(cls(), **converted)


def load_config(path: Path | None = None) -> Config:
    """Load settings from TOML; a missing default config file means defaults."""
    explicit = path is not None
    path = path or Path(os.environ.get("OBSIDIAN_STICKY_CONFIG", CONFIG_PATH))
    if not path.exists():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return Config()
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    config = _build(Config, data, "")
    if config.anchor not in ANCHORS:
        raise ConfigError(f"anchor must be one of: {', '.join(ANCHORS)}")
    if not 0.1 <= config.opacity <= 1.0:
        raise ConfigError("opacity must be between 0.1 and 1.0")
    if config.refresh_seconds < 1 or config.width < 120:
        raise ConfigError("refresh_seconds must be >= 1 and width >= 120")
    return config


def initial_position(
    config: Config, screen_w: int, screen_h: int, win_h: int,
) -> tuple[int, int]:
    """Top-left corner for the note, from its anchor and offsets."""
    x = config.offset_x
    y = config.offset_y
    if config.anchor.endswith("right"):
        x = screen_w - config.width - config.offset_x
    if config.anchor.startswith("bottom"):
        y = screen_h - win_h - config.offset_y
    return x, y


def load_saved_position(state_path: Path = STATE_PATH) -> tuple[int, int] | None:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return int(data["x"]), int(data["y"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_position(x: int, y: int, state_path: Path = STATE_PATH) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"x": x, "y": y}), encoding="utf-8")
    except OSError:
        pass  # Remembering the position is a convenience, never fatal.


def register_hyprland_rule() -> None:
    """Float and pin the note on Hyprland, even without a user window rule.

    The persistent rule belongs in ~/.config/hypr (install.sh adds it); this
    runtime copy just makes `python3 obsidian_todos_sync.py` work out of the box.
    """
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not shutil.which("hyprctl"):
        return
    rule = (
        f'hl.window_rule({{ match = {{ class = "^{WM_CLASS}$" }}, '
        'float = true, pin = true, no_initial_focus = true, border_size = 0 })'
    )
    try:
        subprocess.run(
            ["hyprctl", "eval", rule], capture_output=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
# ─────────────────────────────────────────────────────────────────────────────

TASK_RE = re.compile(r"^\s*-\s+\[([ xX])\]\s?(.*)$")
COMPLETION_DATE_RE = re.compile(r"\s*✅\s*\d{4}-\d{2}-\d{2}\s*$")


@dataclass(frozen=True)
class MarkdownSpan:
    """A small, UI-independent piece of rendered inline Markdown."""

    text: str
    styles: tuple[str, ...] = ()
    target: str | None = None


@dataclass(frozen=True)
class TodoItem:
    """A task tied to its exact source line in a TODO snapshot."""

    text: str
    raw_line: str
    line_index: int


@dataclass(frozen=True)
class TodoDocument:
    """Parsed tasks plus the source used for conflict-safe widget actions."""

    source: str
    unchecked: tuple[TodoItem, ...]
    checked: tuple[TodoItem, ...]


class TodoConflictError(RuntimeError):
    """Raised when Obsidian changes TODO.md during a widget action."""


# This intentionally implements inline Markdown only: enough for compact task
# labels without trying to embed a full browser or Markdown engine in tkinter.
INLINE_TOKEN_RE = re.compile(
    r"(?P<image>!\[(?P<image_alt>[^]]*)\]\((?P<image_url><[^>]+>|[^\s)]+)\))"
    r"|(?P<link>\[(?P<link_text>[^]]+)\]\((?P<link_url><[^>]+>|[^\s)]+)\))"
    r"|(?P<wiki>\[\[(?P<wiki_target>[^]|]+)(?:\|(?P<wiki_text>[^]]+))?\]\])"
    r"|(?P<autolink><(?P<autolink_url>(?:https?://|mailto:)[^>]+)>)"
    r"|(?P<bare>(?:https?://|www\.)[^\s<>]+)"
    r"|(?P<code>`(?P<code_text>[^`]+)`)"
    r"|(?P<bold>\*\*(?P<bold_text>.+?)\*\*|__(?P<bold_u_text>.+?)__)"
    r"|(?P<strike>~~(?P<strike_text>.+?)~~)"
    r"|(?P<italic>\*(?P<italic_text>[^*]+)\*|_(?P<italic_u_text>[^_]+)_)",
)


def _trim_bare_url(url: str) -> tuple[str, str]:
    """Separate sentence punctuation from a URL without losing its fragment."""
    suffix = ""
    while url and url[-1] in ".,;:!?":
        suffix = url[-1] + suffix
        url = url[:-1]
    while url.endswith(")") and url.count("(") < url.count(")"):
        suffix = ")" + suffix
        url = url[:-1]
    return url, suffix


def parse_inline_markdown(text: str) -> list[MarkdownSpan]:
    """Render common inline Markdown and Obsidian wiki links into spans."""
    spans: list[MarkdownSpan] = []
    cursor = 0
    for match in INLINE_TOKEN_RE.finditer(text):
        if match.start() > cursor:
            spans.append(MarkdownSpan(text[cursor:match.start()]))

        kind = next(
            name for name in (
                "image", "link", "wiki", "autolink", "bare", "code",
                "bold", "strike", "italic",
            )
            if match.group(name) is not None
        )
        if kind == "image":
            alt = match.group("image_alt") or "image"
            target = match.group("image_url").strip("<>")
            spans.append(MarkdownSpan(f"🖼 {alt}", ("link",), target))
        elif kind == "link":
            target = match.group("link_url").strip("<>")
            spans.append(MarkdownSpan(match.group("link_text"), ("link",), target))
        elif kind == "wiki":
            target = match.group("wiki_target")
            label = match.group("wiki_text") or target.split("#", 1)[0]
            spans.append(MarkdownSpan(label, ("link",), target))
        elif kind == "autolink":
            target = match.group("autolink_url")
            spans.append(MarkdownSpan(target, ("link",), target))
        elif kind == "bare":
            target, suffix = _trim_bare_url(match.group("bare"))
            href = f"https://{target}" if target.startswith("www.") else target
            spans.append(MarkdownSpan(target, ("link",), href))
            if suffix:
                spans.append(MarkdownSpan(suffix))
        elif kind == "code":
            spans.append(MarkdownSpan(match.group("code_text"), ("code",)))
        elif kind == "bold":
            spans.append(MarkdownSpan(
                match.group("bold_text") or match.group("bold_u_text"),
                ("bold",),
            ))
        elif kind == "strike":
            spans.append(MarkdownSpan(match.group("strike_text"), ("strike",)))
        elif kind == "italic":
            spans.append(MarkdownSpan(
                match.group("italic_text") or match.group("italic_u_text"),
                ("italic",),
            ))
        cursor = match.end()

    if cursor < len(text):
        spans.append(MarkdownSpan(text[cursor:]))
    return spans or [MarkdownSpan("")]


def obsidian_url(target: str, source_file: Path | None = None) -> str | None:
    """Turn a Markdown/Obsidian target into a safe URL suitable for opening."""
    source_file = source_file or Path("TODO.md")
    target = target.strip().strip("<>")
    parsed = urlsplit(target)
    scheme = parsed.scheme.lower()

    if scheme in {"http", "https", "mailto", "obsidian"}:
        return target
    if scheme:
        return None  # Do not launch arbitrary URI schemes from note content.

    raw_path = unquote(parsed.path)
    if raw_path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = source_file.parent / path
        path = path.resolve()
    else:
        path = source_file.resolve()

    # Obsidian expects #Heading or #^block to be encoded as part of the path.
    path_and_fragment = str(path)
    if parsed.fragment:
        path_and_fragment += f"#{unquote(parsed.fragment)}"
    return f"obsidian://open?path={quote(path_and_fragment, safe='')}"


def open_link(target: str, source_file: Path | None = None) -> bool:
    """Open a rendered link, returning False for unsupported targets."""
    url = obsidian_url(target, source_file)
    return bool(url and webbrowser.open(url))


def read_todo_document(todo_file: Path) -> TodoDocument:
    """Read tasks with enough source identity to complete one safely."""
    source = _read_text(todo_file)
    unchecked: list[TodoItem] = []
    checked: list[TodoItem] = []
    for line_index, line in enumerate(source.splitlines(keepends=True)):
        match = TASK_RE.match(line.rstrip("\r\n"))
        if not match:
            continue
        clean = COMPLETION_DATE_RE.sub("", match.group(2)).rstrip()
        item = TodoItem(clean, line, line_index)
        if match.group(1) == " ":
            unchecked.append(item)
        else:
            checked.append(item)
    return TodoDocument(source, tuple(unchecked), tuple(checked))


def extract_todos(todo_file: Path) -> tuple[list[str], list[str]]:
    """Read display-ready task text without changing the Markdown file."""
    if not todo_file.exists():
        return [f"⚠  File not found:\n   {todo_file}"], []
    try:
        document = read_todo_document(todo_file)
    except OSError as exc:
        return [f"⚠  Read error:\n   {exc}"], []
    unchecked = [f"• {item.text}" for item in document.unchecked]
    checked = [f"• {item.text}" for item in document.checked]
    if not unchecked and not checked:
        unchecked = ["✅  All done — nothing pending!"]
    return unchecked, checked


def _with_completion_date(line: str, completed_on: date) -> str:
    content = line.rstrip("\r\n")
    ending = line[len(content):] or "\n"
    if not COMPLETION_DATE_RE.search(content):
        content += f" ✅ {completed_on.isoformat()}"
    return content + ending


def _atomic_write(path: Path, content: str, mode: int) -> None:
    """Replace a text file atomically while retaining its permission bits."""
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _read_text(path: Path) -> str:
    """Read without Python's universal-newline conversion."""
    with path.open("r", encoding="utf-8", newline="") as file:
        return file.read()


def _checked_line(line: str) -> str:
    """Mark an unchecked task line as checked, retaining indentation."""
    checked, replacements = re.subn(
        r"^(\s*-\s+)\[ \]", r"\1[x]", line, count=1,
    )
    if replacements != 1:
        raise TodoConflictError("the selected task is no longer unchecked")
    return checked


def _archive_task_lines(
    todo_file: Path,
    done_file: Path,
    original: str,
    line_indexes: set[int],
    completed_on: date,
    mark_checked: bool = False,
) -> int:
    """Atomically remove selected lines and append their completed forms."""
    lines = original.splitlines(keepends=True)
    if not line_indexes:
        return 0
    if min(line_indexes) < 0 or max(line_indexes) >= len(lines):
        raise TodoConflictError("the selected task no longer exists")

    selected = [line for index, line in enumerate(lines) if index in line_indexes]
    if mark_checked:
        selected = [_checked_line(line) for line in selected]
    remaining = "".join(
        line for index, line in enumerate(lines) if index not in line_indexes
    )
    archived = "".join(
        _with_completion_date(line, completed_on) for line in selected
    )

    done_existed = done_file.exists()
    done_original = _read_text(done_file) if done_existed else ""
    separator = "" if not done_original or done_original.endswith(("\n", "\r")) else "\n"
    done_updated = done_original + separator + archived

    todo_mode = todo_file.stat().st_mode & 0o777
    done_mode = (done_file.stat().st_mode & 0o777) if done_existed else todo_mode

    # Write DONE first, then verify TODO still matches the displayed snapshot.
    # Roll back DONE if Obsidian won the race or replacing TODO fails.
    _atomic_write(done_file, done_updated, done_mode)
    try:
        if _read_text(todo_file) != original:
            raise TodoConflictError("TODO.md changed in Obsidian")
        _atomic_write(todo_file, remaining, todo_mode)
    except (OSError, TodoConflictError):
        if done_existed:
            _atomic_write(done_file, done_original, done_mode)
        else:
            done_file.unlink(missing_ok=True)
        raise
    return len(selected)


def move_completed_todos(
    todo_file: Path,
    done_file: Path,
    completed_on: date | None = None,
) -> int:
    """Move checked task lines from TODO to DONE and return the move count."""
    completed_on = completed_on or date.today()
    original = _read_text(todo_file)
    lines = original.splitlines(keepends=True)
    completed_indexes = {
        index for index, line in enumerate(lines)
        if (
            (match := TASK_RE.match(line.rstrip("\r\n")))
            and match.group(1).lower() == "x"
        )
    }
    return _archive_task_lines(
        todo_file, done_file, original, completed_indexes, completed_on,
    )


def complete_todo(
    item: TodoItem,
    expected_source: str,
    todo_file: Path,
    done_file: Path,
    completed_on: date | None = None,
) -> None:
    """Complete one widget task, refusing to guess after concurrent edits."""
    current = _read_text(todo_file)
    if current != expected_source:
        raise TodoConflictError("TODO.md changed in Obsidian")
    lines = current.splitlines(keepends=True)
    if item.line_index >= len(lines) or lines[item.line_index] != item.raw_line:
        raise TodoConflictError("the selected task changed in Obsidian")
    match = TASK_RE.match(item.raw_line.rstrip("\r\n"))
    if not match or match.group(1) != " ":
        raise TodoConflictError("the selected task is no longer unchecked")
    _archive_task_lines(
        todo_file, done_file, current, {item.line_index},
        completed_on or date.today(), mark_checked=True,
    )


class StickyWidget:

    def __init__(self, config: Config, state_path: Path = STATE_PATH):
        self.config = config
        self.todo_file = config.todo_path
        self.done_file = config.done_path
        self.state_path = state_path
        colors, fonts = config.colors, config.fonts
        self._link_cursor = "pointinghand" if IS_MAC else "hand2"

        register_hyprland_rule()
        self.root = tk.Tk(className=APP_ID)
        self.root.withdraw()
        self.root.title(config.title.lstrip("◆ ").strip() or "Obsidian TODOs")
        self.root.configure(bg=colors.background)
        # macOS and plain X11: a frameless override-redirect window. Wayland
        # compositors need a managed window so window rules (float/pin) apply;
        # Hyprland draws no title bar for it anyway.
        if not self._is_wayland():
            self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", config.always_on_top)
        try:
            self.root.wm_attributes("-alpha", config.opacity)
        except tk.TclError:
            pass
        self.root.resizable(False, False)

        self._drag_x = self._drag_y = 0
        self._dragged = False
        self._link_number = 0
        self._checkboxes: list[tuple[tk.Checkbutton, tk.BooleanVar]] = []
        self._refresh_job: str | None = None
        self._status_error: str | None = None
        self._placed = False

        header_font = (fonts.family, fonts.header_size, "bold")

        # ── header ───────────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=colors.header, cursor="fleur")
        hdr.pack(fill=tk.X)

        tk.Label(hdr, text=config.title, bg=colors.header, fg=colors.text,
                 font=header_font, anchor="w", padx=10, pady=7
                 ).pack(side=tk.LEFT)

        self._ts = tk.Label(hdr, text="", bg=colors.header, fg=colors.muted,
                            font=(fonts.family, max(6, fonts.header_size - 2)),
                            anchor="e", padx=6)
        self._ts.pack(side=tk.LEFT, expand=True, fill=tk.X)

        close = tk.Label(hdr, text="×", bg=colors.header, fg=colors.text,
                         font=(fonts.family, fonts.header_size + 3, "bold"),
                         padx=10, pady=4, cursor=self._link_cursor)
        close.pack(side=tk.RIGHT)
        close.bind("<Button-1>", lambda _event: self.root.destroy())
        close.bind("<Enter>", lambda _event: close.config(fg=colors.close_hover))
        close.bind("<Leave>", lambda _event: close.config(fg=colors.text))

        for widget in (hdr, *hdr.winfo_children()):
            if widget is close:
                continue
            self._bind_drag(widget)

        # ── body ─────────────────────────────────────────────────────────────
        self._body = tk.Text(
            self.root, bg=colors.background, fg=colors.text,
            wrap=tk.WORD, width=1, height=1, borderwidth=0,
            highlightthickness=0, padx=12, pady=7, spacing1=1, spacing3=1,
            cursor="arrow", takefocus=False,
        )
        self._body.pack(fill=tk.BOTH)
        self._bind_drag(self._body)
        self._configure_markdown_tags()

        # First paint + position
        self.refresh()
        self.root.update_idletasks()
        window_height = self.root.winfo_reqheight()
        x, y = initial_position(
            config, self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(), window_height,
        )
        saved = load_saved_position(state_path) if config.remember_position else None
        if saved and self._on_screen(*saved):
            x, y = saved
        self.root.geometry(f"{config.width}x{window_height}+{x}+{y}")
        self._placed = True
        self.root.deiconify()
        self._set_native_floating()

    @staticmethod
    def _is_wayland() -> bool:
        return not IS_MAC and bool(os.environ.get("WAYLAND_DISPLAY"))

    def _on_screen(self, x: int, y: int) -> bool:
        width = self.root.winfo_screenwidth()
        height = self.root.winfo_screenheight()
        return -self.config.width // 2 < x < width - 40 and 0 <= y < height - 30

    def _configure_markdown_tags(self):
        fonts, colors = self.config.fonts, self.config.colors
        body = tkfont.Font(family=fonts.family, size=fonts.size)
        bold = body.copy()
        bold.configure(weight="bold")
        italic = body.copy()
        italic.configure(slant="italic")
        code = tkfont.Font(family=fonts.mono_family, size=fonts.mono_size)
        self._fonts = (body, bold, italic, code)  # Keep tkinter font objects alive.
        # Rough wrap estimate used to size the window to its content.
        sample = "the quick brown fox jumps over a lazy dog"
        char_width = body.measure(sample) / len(sample)
        self._chars_per_line = max(10, int((self.config.width - 40) / char_width))
        self._body.configure(font=body)
        self._body.tag_configure("bold", font=bold)
        self._body.tag_configure("italic", font=italic)
        self._body.tag_configure("code", font=code, background=colors.code_background)
        self._body.tag_configure("strike", overstrike=True)
        self._body.tag_configure("link", foreground=colors.link, underline=True)
        self._body.tag_configure("done", foreground=colors.muted, overstrike=True)
        self._body.tag_configure("warning", foreground=colors.warning)

    def _set_native_floating(self):
        """Keep the widget floating on every macOS Mission Control space."""
        if not HAS_PYOBJC:
            return
        try:
            self.root.update()
            title = self.root.title()
            for native_window in NSApp.windows():
                if native_window.title() == title:
                    native_window.setLevel_(NSFloatingWindowLevel)
                    native_window.setCollectionBehavior_(
                        NSWindowCollectionBehaviorCanJoinAllSpaces
                    )
                    break
        except Exception:
            pass  # Silently fall back to tkinter's -topmost.

    # ── drag ──────────────────────────────────────────────────────────────────
    def _bind_drag(self, widget: tk.Misc):
        widget.bind("<ButtonPress-1>", self._drag_start, add="+")
        widget.bind("<B1-Motion>", self._drag_move, add="+")
        widget.bind("<ButtonRelease-1>", self._drag_end, add="+")

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()
        self._dragged = False

    def _drag_move(self, event):
        self._dragged = True
        self.root.geometry(
            f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}"
        )

    def _drag_end(self, event):
        # winfo_x/y lag behind the compositor, so derive the drop point.
        if self._dragged and self.config.remember_position:
            save_position(
                event.x_root - self._drag_x, event.y_root - self._drag_y,
                self.state_path,
            )

    # ── Markdown rendering ────────────────────────────────────────────────────
    def _insert_markdown(self, text: str, extra_tags: tuple[str, ...] = ()):
        for span in parse_inline_markdown(text):
            tags = (*span.styles, *extra_tags)
            if span.target:
                self._link_number += 1
                link_tag = f"url_{self._link_number}"
                tags = (*tags, link_tag)
                target = span.target
                self._body.tag_bind(
                    link_tag, "<ButtonRelease-1>",
                    lambda _event, href=target: (
                        None if self._dragged else open_link(href, self.todo_file)
                    ),
                )
                self._body.tag_bind(
                    link_tag, "<Enter>",
                    lambda _event: self._body.configure(cursor=self._link_cursor),
                )
                self._body.tag_bind(
                    link_tag, "<Leave>",
                    lambda _event: self._body.configure(cursor="arrow"),
                )
            self._body.insert(tk.END, span.text, tags)

    def _wrapped_lines(self, visible_length: int) -> int:
        return max(1, math.ceil(visible_length / self._chars_per_line))

    def _add_line(self, text: str, tags: tuple[str, ...] = ()) -> int:
        self._insert_markdown(text, tags)
        self._body.insert(tk.END, "\n")
        visible_length = sum(len(span.text) for span in parse_inline_markdown(text))
        return self._wrapped_lines(visible_length)

    def _add_task(self, item: TodoItem, source: str) -> int:
        """Insert an actionable checkbox followed by rendered task Markdown."""
        colors = self.config.colors
        checked = tk.BooleanVar(self.root, value=False)
        checkbox = tk.Checkbutton(
            self._body, variable=checked,
            command=lambda task=item, snapshot=source: self._complete_from_widget(
                task, snapshot
            ),
            bg=colors.background, activebackground=colors.background,
            selectcolor=colors.background, fg=colors.text,
            highlightthickness=0, borderwidth=0, relief=tk.FLAT,
            padx=0, pady=0, takefocus=False, cursor=self._link_cursor,
        )
        self._checkboxes.append((checkbox, checked))
        self._body.window_create(tk.END, window=checkbox, align=tk.CENTER)
        self._body.insert(tk.END, " ")
        self._insert_markdown(item.text)
        self._body.insert(tk.END, "\n")
        visible_length = 2 + sum(
            len(span.text) for span in parse_inline_markdown(item.text)
        )
        return self._wrapped_lines(visible_length)

    def _complete_from_widget(self, item: TodoItem, source: str):
        """Handle a checkbox click synchronously, then repaint immediately."""
        for checkbox, _variable in self._checkboxes:
            checkbox.configure(state=tk.DISABLED)
        try:
            complete_todo(
                item, source, self.todo_file, self.done_file,
            )
        except TodoConflictError:
            self._status_error = (
                "TODO.md changed in Obsidian; refreshed — please tick again."
            )
        except OSError as exc:
            self._status_error = f"Could not complete task: {exc}"
        self.refresh()

    # ── refresh ───────────────────────────────────────────────────────────────
    def refresh(self):
        if self._refresh_job is not None:
            try:
                self.root.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
            self._refresh_job = None

        move_error: OSError | TodoConflictError | None = None
        try:
            if self.todo_file.exists():
                move_completed_todos(self.todo_file, self.done_file)
        except (OSError, TodoConflictError) as exc:
            move_error = exc

        document: TodoDocument | None = None
        read_error: str | None = None
        try:
            document = read_todo_document(self.todo_file)
        except OSError as exc:
            read_error = (
                f"File not found: {self.todo_file} — set todo_file in "
                f"{CONFIG_PATH}"
                if not self.todo_file.exists() else str(exc)
            )
        self._ts.config(text=datetime.now().strftime("%d %b  %H:%M"))

        self._body.configure(state=tk.NORMAL)
        for checkbox, _variable in self._checkboxes:
            checkbox.destroy()
        self._checkboxes.clear()
        self._body.delete("1.0", tk.END)
        self._link_number = 0
        display_lines = 0

        if self._status_error:
            display_lines += self._add_line(
                f"⚠ {self._status_error}", ("warning",)
            )
            self._status_error = None

        if move_error:
            display_lines += self._add_line(
                f"⚠ Could not move completed task: {move_error}", ("warning",)
            )

        if read_error:
            display_lines += self._add_line(f"⚠ {read_error}", ("warning",))
        elif document:
            for item in document.unchecked:
                display_lines += self._add_task(item, document.source)

            if document.checked:
                display_lines += self._add_line("")
                for item in document.checked:
                    display_lines += self._add_line(
                        f"• {item.text}", ("done",)
                    )

            if not document.unchecked and not document.checked:
                display_lines += self._add_line(
                    "✅  All done — nothing pending!"
                )

        self._body.configure(height=max(1, display_lines), state=tk.DISABLED)

        # Resize window to fit new content, keeping its current position.
        if self._placed:
            self.root.update_idletasks()
            new_height = self.root.winfo_reqheight()
            self.root.geometry(
                f"{self.config.width}x{new_height}"
                f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
            )

        self._refresh_job = self.root.after(
            self.config.refresh_seconds * 1000, self.refresh,
        )

    def run(self):
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--config", type=Path,
        help=f"TOML config file (default: $OBSIDIAN_STICKY_CONFIG or {CONFIG_PATH})",
    )
    parser.add_argument(
        "--todo-file", type=Path, help="override todo_file from the config",
    )
    parser.add_argument(
        "--reset-position", action="store_true",
        help="forget the remembered position and use anchor/offsets",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"{APP_ID}: config error: {exc}", file=sys.stderr)
        return 2
    if args.todo_file:
        config = replace(config, todo_file=args.todo_file)
    if args.reset_position:
        STATE_PATH.unlink(missing_ok=True)
    StickyWidget(config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
