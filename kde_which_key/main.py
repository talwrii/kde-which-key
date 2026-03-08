"""kde-which-key: Interactive shortcut browser and launcher for KDE."""
from __future__ import annotations

import configparser
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "kglobalshortcutsrc"
DESKTOP_DIR = Path.home() / ".local" / "share" / "applications"

TKINTER_TO_KDE = {
    "Control_L": "Ctrl", "Control_R": "Ctrl",
    "Alt_L": "Alt", "Alt_R": "Alt",
    "Super_L": "Meta", "Super_R": "Meta",
    "Shift_L": "Shift", "Shift_R": "Shift",
}

MODIFIER_KEYSYMS = {
    "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Shift_L", "Shift_R",
}

MODIFIER_ORDER = ["Meta", "Ctrl", "Alt", "Shift"]

# Tri-state: None = any, True = must have, False = must not have
TRISTATE_CYCLE = {None: True, True: False, False: None}
TRISTATE_LABEL = {None: "·", True: "✓", False: "✗"}
TRISTATE_FG = {None: "#6c7086", True: "#a6e3a1", False: "#f38ba8"}


@dataclass
class Shortcut:
    group: str
    key: str
    binding: str
    description: str

    @property
    def display_name(self) -> str:
        if self.description and self.description != self.key:
            return f"{self.description}"
        return self.key

    @property
    def bindings(self) -> list[str]:
        """Split multi-bound shortcuts."""
        return [b.strip() for b in self.binding.split("\\t")]


def load_shortcuts(config_path: Optional[Path] = None) -> list[Shortcut]:
    """Load all active shortcuts from kglobalshortcutsrc."""
    path = config_path or DEFAULT_CONFIG_PATH
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    if path.exists():
        parser.read(str(path))
    shortcuts = []
    for section in parser.sections():
        for key, value in parser.items(section):
            if key == "_k_friendly_name":
                continue
            parts = value.split(",", 2)
            if len(parts) == 3:
                active, _default, description = parts
            elif len(parts) == 2:
                active, _default = parts
                description = ""
            else:
                active = parts[0] if parts else ""
                description = ""
            active = active.strip()
            if active and active.lower() not in ("", "none"):
                shortcuts.append(Shortcut(
                    group=section,
                    key=key,
                    binding=active,
                    description=description.strip(),
                ))
    return shortcuts


def resolve_command(sc: Shortcut) -> str:
    """Return a human-readable description of the command that will run."""
    if sc.group.endswith(".desktop"):
        desktop_path = DESKTOP_DIR / sc.group
        if desktop_path.exists():
            for line in desktop_path.read_text().splitlines():
                if line.startswith("Exec="):
                    cmd = line[5:].strip()
                    for code in ["%u", "%U", "%f", "%F", "%i", "%c", "%k"]:
                        cmd = cmd.replace(code, "")
                    cmd = cmd.strip()
                    return f"Exec: {cmd}"
        component = sc.group.removesuffix(".desktop")
        return (
            f"DBus: qdbus org.kde.kglobalaccel\n"
            f"  /component/{component}\n"
            f"  invokeShortcut \"{sc.key}\""
        )
    else:
        return (
            f"DBus: qdbus org.kde.kglobalaccel\n"
            f"  /component/{sc.group}\n"
            f"  invokeShortcut \"{sc.key}\""
        )


def save_command(sc: Shortcut, new_cmd: str) -> str:
    """Save an edited command. Returns an error message or empty string on success."""
    new_cmd = new_cmd.strip()
    if not new_cmd:
        return "Command cannot be empty"
    if sc.group.endswith(".desktop"):
        desktop_path = DESKTOP_DIR / sc.group
        if desktop_path.exists():
            lines = desktop_path.read_text().splitlines()
            new_lines = []
            replaced = False
            for line in lines:
                if line.startswith("Exec=") and not replaced:
                    new_lines.append(f"Exec={new_cmd}")
                    replaced = True
                else:
                    new_lines.append(line)
            if not replaced:
                new_lines.append(f"Exec={new_cmd}")
            desktop_path.write_text("\n".join(new_lines) + "\n")
            return ""
        return "No .desktop file found to edit"
    # DBus shortcut — no persistent file to edit
    return "DBus shortcuts cannot be edited (no backing file)"


def invoke_shortcut(sc: Shortcut):
    """Trigger a shortcut action via dbus or by running the desktop Exec."""
    if sc.group.endswith(".desktop"):
        desktop_path = DESKTOP_DIR / sc.group
        if desktop_path.exists():
            for line in desktop_path.read_text().splitlines():
                if line.startswith("Exec="):
                    cmd = line[5:].strip()
                    for code in ["%u", "%U", "%f", "%F", "%i", "%c", "%k"]:
                        cmd = cmd.replace(code, "")
                    cmd = cmd.strip()
                    subprocess.Popen(cmd, shell=True)
                    return
        component = sc.group.removesuffix(".desktop")
        _invoke_via_dbus(component, sc.key)
    else:
        _invoke_via_dbus(sc.group, sc.key)


def _invoke_via_dbus(component: str, action: str):
    """Invoke a shortcut action via kglobalaccel dbus."""
    try:
        subprocess.Popen([
            "qdbus", "org.kde.kglobalaccel",
            f"/component/{component}",
            "org.kde.kglobalaccel.Component.invokeShortcut",
            action,
        ])
    except FileNotFoundError:
        pass


def fuzzy_match(query: str, text: str) -> tuple[bool, int]:
    """Simple fuzzy match. Returns (matched, score). Lower score = better."""
    query_lower = query.lower()
    text_lower = text.lower()
    if query_lower in text_lower:
        return True, text_lower.index(query_lower)
    qi = 0
    score = 0
    last_pos = -1
    for ti, ch in enumerate(text_lower):
        if qi < len(query_lower) and ch == query_lower[qi]:
            gap = ti - last_pos - 1
            score += gap
            last_pos = ti
            qi += 1
    if qi == len(query_lower):
        return True, score + 100
    return False, 0


def parse_binding_parts(binding: str) -> tuple[set[str], str]:
    """Split a binding like 'Meta+Shift+A' into (modifiers, key)."""
    parts = binding.split("+")
    mods = set()
    key = ""
    for p in parts:
        if p in MODIFIER_ORDER:
            mods.add(p)
        else:
            key = p
    return mods, key


def _block_global_shortcuts(block: bool):
    """Tell kglobalaccel to block/unblock global shortcuts."""
    try:
        subprocess.run(
            ["qdbus", "org.kde.kglobalaccel", "/kglobalaccel",
             "blockGlobalShortcuts", "true" if block else "false"],
            capture_output=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def remove_shortcut_from_config(sc: Shortcut, config_path: Optional[Path] = None):
    """Reset a shortcut binding to 'none' in kglobalshortcutsrc."""
    import shutil
    path = config_path or DEFAULT_CONFIG_PATH
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(str(path))
    if not parser.has_section(sc.group) or not parser.has_option(sc.group, sc.key):
        return
    value = parser.get(sc.group, sc.key)
    parts = value.split(",", 2)
    if len(parts) == 3:
        _active, default, description = parts
    elif len(parts) == 2:
        _active, default = parts
        description = ""
    else:
        default = ""
        description = ""
    parser.set(sc.group, sc.key, f"none,{default},{description}")
    if path.exists():
        shutil.copy2(path, path.with_suffix(".bak"))
    with open(path, "w") as f:
        parser.write(f, space_around_delimiters=False)


class Tooltip:
    """In-window overlay tooltip placed on top of the hovered row."""

    def __init__(self, tk, root):
        self._tk = tk
        self._root = root
        self._frame: Optional[object] = None
        self._show_after: Optional[str] = None
        self._hide_after: Optional[str] = None
        self._mouse_in_tooltip = False
        self._hover_widget: Optional[object] = None

    def show(self, widget, sc: Shortcut):
        """Schedule tooltip display after short delay."""
        self._hover_widget = widget
        self._cancel_hide()
        if self._show_after:
            self._root.after_cancel(self._show_after)
        self._show_after = self._root.after(350, lambda: self._display(widget, sc))

    def hide(self):
        """Schedule tooltip hide with a small grace period."""
        if self._show_after:
            self._root.after_cancel(self._show_after)
            self._show_after = None
        self._cancel_hide()
        self._hide_after = self._root.after(120, self._do_hide)

    def _cancel_hide(self):
        if self._hide_after:
            self._root.after_cancel(self._hide_after)
            self._hide_after = None

    def _do_hide(self):
        self._hide_after = None
        if self._mouse_in_tooltip:
            return
        self._destroy_frame()

    def force_hide(self):
        """Immediately hide (e.g. on list redraw)."""
        if self._show_after:
            self._root.after_cancel(self._show_after)
            self._show_after = None
        self._cancel_hide()
        self._mouse_in_tooltip = False
        self._destroy_frame()

    def _destroy_frame(self):
        if self._frame:
            self._frame.place_forget()
            self._frame.destroy()
            self._frame = None

    def _display(self, widget, sc: Shortcut):
        self._show_after = None
        self._destroy_frame()
        cmd = resolve_command(sc)
        # Place inside root window, positioned over the hovered widget
        wx = widget.winfo_rootx() - self._root.winfo_rootx()
        wy = widget.winfo_rooty() - self._root.winfo_rooty()
        rw = self._root.winfo_width()
        rh = self._root.winfo_height()
        frame = self._tk.Frame(self._root, bg="#313244", bd=1, relief="solid")
        # Info rows
        info_text = "\n".join([
            f"Group:   {sc.group}",
            f"Action:  {sc.key}",
            f"Binding: {sc.binding}",
        ])
        info_label = self._tk.Label(
            frame, text=info_text, justify="left",
            font=("monospace", 10), fg="#cdd6f4", bg="#313244",
            padx=10, pady=8,  # fixed: tuples not allowed on Label
        )
        info_label.pack(anchor="w")
        # Separator
        sep = self._tk.Frame(frame, bg="#45475a", height=1)
        sep.pack(fill="x", padx=8, pady=(0, 4))
        # Command label
        cmd_lbl = self._tk.Label(
            frame, text="Command:", justify="left",
            font=("monospace", 9), fg="#a6adc8", bg="#313244",
            padx=10, pady=0,
        )
        cmd_lbl.pack(anchor="w")
        # Editable command entry
        cmd_var = self._tk.StringVar(value=cmd)
        cmd_entry = self._tk.Entry(
            frame, textvariable=cmd_var,
            font=("monospace", 10), fg="#cdd6f4", bg="#1e1e2e",
            insertbackground="#cdd6f4", relief="flat",
            width=50,
        )
        cmd_entry.pack(fill="x", padx=10, pady=(2, 4))
        # Save button + status
        btn_row = self._tk.Frame(frame, bg="#313244")
        btn_row.pack(fill="x", padx=10, pady=(0, 8))
        status_lbl = self._tk.Label(
            btn_row, text="", font=("monospace", 9),
            fg="#a6e3a1", bg="#313244",
        )
        status_lbl.pack(side="left")

        def do_save():
            err = save_command(sc, cmd_var.get())
            if err:
                status_lbl.config(text=err, fg="#f38ba8")
            else:
                status_lbl.config(text="Saved ✓", fg="#a6e3a1")

        save_btn = self._tk.Button(
            btn_row, text="Save",
            font=("monospace", 9, "bold"),
            fg="#1e1e2e", bg="#89b4fa",
            activeforeground="#1e1e2e", activebackground="#74c7ec",
            relief="flat", padx=8, pady=2,
            cursor="hand2",
            command=do_save,
        )
        save_btn.pack(side="right")
        # Bind Enter in the entry to save too
        cmd_entry.bind("<Return>", lambda e: do_save())
        # Measure after packing
        frame.update_idletasks()
        tw = frame.winfo_reqwidth()
        th = frame.winfo_reqheight()
        x = max(4, min(wx + 40, rw - tw - 4))
        y = max(4, min(wy, rh - th - 4))
        frame.place(x=x, y=y)
        frame.lift()

        def on_enter(e):
            self._mouse_in_tooltip = True
            self._cancel_hide()

        def on_leave(e):
            self._mouse_in_tooltip = False
            self.hide()

        for w in (frame, info_label, sep, cmd_lbl, cmd_entry, btn_row, save_btn, status_lbl):
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
        self._frame = frame


class WhichKeyApp:
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path
        self.shortcuts = load_shortcuts(config_path)
        self.filtered: list[Shortcut] = list(self.shortcuts)
        self.selected_index = 0
        # Key filter state
        self.modifiers_held: set[str] = set()
        self.key_filter_key: str = ""
        # Tri-state modifier filters: None=any, True=must have, False=must not have
        self.mod_filter: dict[str, Optional[bool]] = {m: None for m in MODIFIER_ORDER}
        # Search mode
        self.search_mode = False
        self.search_query = ""
        self._build_ui()

    def _build_ui(self):
        import tkinter as tk
        import tkinter.font as tkfont
        self._tk = tk
        self.root = tk.Tk()
        self.root.title("kde-which-key")
        self.root.attributes("-topmost", True)
        width, height = 700, 540
        self.root.geometry(f"{width}x{height}")
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.configure(bg="#1e1e2e")
        self.font_main = tkfont.Font(family="monospace", size=11)
        self.font_binding = tkfont.Font(family="monospace", size=11, weight="bold")
        self.font_status = tkfont.Font(family="sans-serif", size=10)
        self.font_search = tkfont.Font(family="monospace", size=14)
        self.font_btn = tkfont.Font(family="monospace", size=10, weight="bold")
        # Status bar
        self.status_frame = tk.Frame(self.root, bg="#313244", height=40)
        self.status_frame.pack(fill="x", padx=8, pady=(8, 4))
        self.status_frame.pack_propagate(False)
        # Status bar — split into parts so "? = search" is clickable
        self.status_label_left = tk.Label(
            self.status_frame,
            text="Press keys to filter  |  ",
            font=self.font_status, fg="#a6adc8", bg="#313244", anchor="w",
        )
        self.status_label_left.pack(side="left", padx=(10, 0))
        self.search_trigger_label = tk.Label(
            self.status_frame,
            text="? = search",
            font=self.font_status, fg="#89b4fa", bg="#313244",
            cursor="hand2",
        )
        self.search_trigger_label.pack(side="left")
        self.search_trigger_label.bind("<Button-1>", lambda e: self._enter_search_mode())
        self.status_label_right = tk.Label(
            self.status_frame,
            text="  |  Del = remove  |  Esc = quit",
            font=self.font_status, fg="#a6adc8", bg="#313244", anchor="w",
        )
        self.status_label_right.pack(side="left")
        # Keep a reference to all status widgets for show/hide during search
        self._status_parts = [self.status_label_left, self.search_trigger_label, self.status_label_right]
        self.status_label = self.status_label_left  # kept for compat
        self.search_entry = tk.Entry(
            self.status_frame, font=self.font_search,
            fg="#cdd6f4", bg="#45475a", insertbackground="#cdd6f4",
            relief="flat", borderwidth=0,
        )
        # Modifier toggle buttons
        self.btn_frame = tk.Frame(self.root, bg="#1e1e2e")
        self.btn_frame.pack(fill="x", padx=8, pady=(0, 4))
        self.mod_buttons: dict[str, tk.Button] = {}
        for mod in MODIFIER_ORDER:
            btn = tk.Button(
                self.btn_frame,
                text=f"{mod} {TRISTATE_LABEL[None]}",
                font=self.font_btn,
                fg=TRISTATE_FG[None], bg="#313244",
                activeforeground="#cdd6f4", activebackground="#45475a",
                relief="flat", borderwidth=0, padx=12, pady=4,
                command=lambda m=mod: self._toggle_mod(m),
            )
            btn.pack(side="left", padx=4)
            self.mod_buttons[mod] = btn
        self.key_label = tk.Label(
            self.btn_frame, text="", font=self.font_btn,
            fg="#f5c2e7", bg="#1e1e2e",
        )
        self.key_label.pack(side="left", padx=(12, 0))
        # Match count
        self.count_label = tk.Label(
            self.btn_frame, text="", font=self.font_status,
            fg="#a6adc8", bg="#1e1e2e",
        )
        self.count_label.pack(side="right", padx=8)
        # List area
        self.list_frame = tk.Frame(self.root, bg="#1e1e2e")
        self.list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.canvas = tk.Canvas(self.list_frame, bg="#1e1e2e", highlightthickness=0)
        self.scrollbar = tk.Scrollbar(self.list_frame, orient="vertical", command=self.canvas.yview)
        self.inner_frame = tk.Frame(self.canvas, bg="#1e1e2e")
        self.inner_frame.bind("<Configure>",
                              lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        # Tooltip
        self.tooltip = Tooltip(tk, self.root)
        # Key bindings
        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)
        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<Return>", self._on_enter)
        self.root.bind("<Up>", self._on_arrow_up)
        self.root.bind("<Down>", self._on_arrow_down)
        self.root.bind("<BackSpace>", self._on_backspace)
        self.root.bind("<Delete>", self._on_delete)
        self.canvas.bind_all("<Button-4>",
                             lambda e: self.canvas.yview_scroll(-3, "units"))
        self.canvas.bind_all("<Button-5>",
                             lambda e: self.canvas.yview_scroll(3, "units"))
        self._update_list()

    def _toggle_mod(self, mod: str):
        """Cycle modifier filter: any → on → off → any."""
        self.mod_filter[mod] = TRISTATE_CYCLE[self.mod_filter[mod]]
        self._update_mod_buttons()
        self._apply_key_filter()

    def _update_mod_buttons(self):
        for mod, btn in self.mod_buttons.items():
            state = self.mod_filter[mod]
            btn.config(
                text=f"{mod} {TRISTATE_LABEL[state]}",
                fg=TRISTATE_FG[state],
            )

    def _on_key_press(self, event):
        if self.search_mode:
            self._handle_search_key(event)
            return
        keysym = event.keysym
        # ? enters search mode
        if keysym == "question" or (keysym == "slash" and event.state & 1):
            self._enter_search_mode()
            return
        if keysym in MODIFIER_KEYSYMS:
            kde_name = TKINTER_TO_KDE.get(keysym, keysym)
            self.modifiers_held.add(kde_name)
            # Pressing a modifier sets it to "must have"
            self.mod_filter[kde_name] = True
            self._update_mod_buttons()
            self._apply_key_filter()
        else:
            kde_key = keysym
            if len(keysym) == 1:
                kde_key = keysym.upper()
            self.key_filter_key = kde_key
            self._apply_key_filter()

    def _on_key_release(self, event):
        if self.search_mode:
            return
        keysym = event.keysym
        if keysym in MODIFIER_KEYSYMS:
            kde_name = TKINTER_TO_KDE.get(keysym, keysym)
            self.modifiers_held.discard(kde_name)

    def _on_escape(self, event):
        if self.search_mode:
            self._exit_search_mode()
        elif self._has_filter():
            self._reset_filter()
        else:
            self.root.destroy()

    def _on_backspace(self, event):
        if self.search_mode:
            return
        self._reset_filter()

    def _on_delete(self, event):
        if self.search_mode:
            return
        if self.filtered and 0 <= self.selected_index < len(self.filtered):
            self._delete_item(self.selected_index)

    def _delete_item(self, index):
        sc = self.filtered[index]
        remove_shortcut_from_config(sc, self.config_path)
        # Remove from our lists
        self.shortcuts = [s for s in self.shortcuts
                          if not (s.group == sc.group and s.key == sc.key)]
        self.filtered = [s for s in self.filtered
                         if not (s.group == sc.group and s.key == sc.key)]
        # Fix selection
        if self.selected_index >= len(self.filtered):
            self.selected_index = max(0, len(self.filtered) - 1)
        self._update_list()

    def _on_enter(self, event):
        if self.filtered and 0 <= self.selected_index < len(self.filtered):
            sc = self.filtered[self.selected_index]
            self.root.destroy()
            invoke_shortcut(sc)

    def _on_arrow_up(self, event):
        if self.selected_index > 0:
            self.selected_index -= 1
            self._update_list()
            self._ensure_visible()

    def _on_arrow_down(self, event):
        if self.selected_index < len(self.filtered) - 1:
            self.selected_index += 1
            self._update_list()
            self._ensure_visible()

    def _enter_search_mode(self):
        self.search_mode = True
        self.search_query = ""
        for w in self._status_parts:
            w.pack_forget()
        self.search_entry.pack(fill="both", expand=True, padx=10, pady=5)
        self.search_entry.focus_set()
        self.search_entry.delete(0, self._tk.END)
        self.search_entry.bind("<Key>", self._on_search_entry_key)
        self._apply_search_filter()

    def _exit_search_mode(self):
        self.search_mode = False
        self.search_query = ""
        self.search_entry.pack_forget()
        for w in self._status_parts:
            w.pack(side="left")
        self.status_label_left.pack_configure(padx=(10, 0))
        self._reset_filter()
        self.root.focus_set()

    def _handle_search_key(self, event):
        pass

    def _on_search_entry_key(self, event):
        if event.keysym == "Escape":
            self._exit_search_mode()
            return "break"
        if event.keysym == "Return":
            self._on_enter(event)
            return "break"
        if event.keysym == "Up":
            self._on_arrow_up(event)
            return "break"
        if event.keysym == "Down":
            self._on_arrow_down(event)
            return "break"
        self.root.after(1, self._apply_search_filter)

    def _has_filter(self) -> bool:
        return (
            self.key_filter_key != ""
            or any(v is not None for v in self.mod_filter.values())
        )

    def _apply_key_filter(self):
        """Filter shortcuts by modifier tri-state and key."""
        self.filtered = []
        for sc in self.shortcuts:
            for binding in sc.bindings:
                bind_mods, bind_key = parse_binding_parts(binding)
                match = True
                for mod in MODIFIER_ORDER:
                    want = self.mod_filter[mod]
                    has = mod in bind_mods
                    if want is True and not has:
                        match = False
                        break
                    if want is False and has:
                        match = False
                        break
                if not match:
                    continue
                if self.key_filter_key:
                    if bind_key.lower() != self.key_filter_key.lower():
                        continue
                self.filtered.append(sc)
                break
        self.selected_index = 0
        self._update_key_label()
        self._update_list()

    def _apply_search_filter(self):
        """Filter shortcuts by fuzzy search query."""
        query = self.search_entry.get().strip()
        if not query:
            self.filtered = list(self.shortcuts)
            self.selected_index = 0
            self._update_list()
            return
        scored = []
        for sc in self.shortcuts:
            best_match = False
            best_score = 99999
            for text in [sc.display_name, sc.key, sc.group, sc.binding]:
                matched, score = fuzzy_match(query, text)
                if matched and score < best_score:
                    best_match = True
                    best_score = score
            if best_match:
                scored.append((best_score, sc))
        scored.sort(key=lambda x: x[0])
        self.filtered = [sc for _, sc in scored]
        self.selected_index = 0
        self._update_list()

    def _reset_filter(self):
        self.mod_filter = {m: None for m in MODIFIER_ORDER}
        self.key_filter_key = ""
        self.filtered = list(self.shortcuts)
        self.selected_index = 0
        self._update_mod_buttons()
        self._update_key_label()
        self._update_list()

    def _update_key_label(self):
        if self.key_filter_key:
            self.key_label.config(text=f"+ {self.key_filter_key}")
        else:
            self.key_label.config(text="")
        self.count_label.config(text=f"{len(self.filtered)} matches")

    def _update_list(self):
        """Redraw the shortcut list."""
        self.tooltip.force_hide()
        for widget in self.inner_frame.winfo_children():
            widget.destroy()
        self.count_label.config(text=f"{len(self.filtered)} matches")
        if not self.filtered:
            label = self._tk.Label(
                self.inner_frame, text="No matching shortcuts",
                font=self.font_main, fg="#6c7086", bg="#1e1e2e",
            )
            label.pack(pady=20)
            return
        for i, sc in enumerate(self.filtered):
            is_selected = (i == self.selected_index)
            bg = "#45475a" if is_selected else "#1e1e2e"
            fg_desc = "#cdd6f4" if is_selected else "#bac2de"
            fg_bind = "#f5c2e7" if is_selected else "#a6adc8"
            row = self._tk.Frame(self.inner_frame, bg=bg)
            row.pack(fill="x", padx=4, pady=1)
            desc_label = self._tk.Label(
                row, text=f"  {sc.display_name}",
                font=self.font_main, fg=fg_desc, bg=bg,
                anchor="w", width=45,
            )
            desc_label.pack(side="left", fill="x", expand=True)
            bind_label = self._tk.Label(
                row, text=f"{sc.binding}  ",
                font=self.font_binding, fg=fg_bind, bg=bg,
                anchor="e",
            )
            bind_label.pack(side="right")
            del_btn = self._tk.Label(
                row, text=" ✗ ",
                font=self.font_main, fg="#f38ba8" if is_selected else "#585b70",
                bg=bg, cursor="hand2",
            )
            del_btn.pack(side="right")
            del_btn.bind("<Button-1>", lambda e, idx=i: self._delete_item(idx))
            # Tooltip bindings on the binding label and row
            for widget in [row, desc_label, bind_label]:
                widget.bind("<Button-1>", lambda e, idx=i: self._click_item(idx))
                widget.bind("<Enter>", lambda e, s=sc: self.tooltip.show(e.widget, s))
                widget.bind("<Leave>", lambda e: self.tooltip.hide())

    def _click_item(self, index):
        self.selected_index = index
        self._update_list()
        sc = self.filtered[index]
        self.root.destroy()
        invoke_shortcut(sc)

    def _ensure_visible(self):
        """Scroll to keep selected item visible."""
        self.root.update_idletasks()
        children = self.inner_frame.winfo_children()
        if 0 <= self.selected_index < len(children):
            widget = children[self.selected_index]
            y = widget.winfo_y()
            h = widget.winfo_height()
            canvas_h = self.canvas.winfo_height()
            total = self.inner_frame.winfo_height()
            if total > 0:
                top = self.canvas.yview()[0] * total
                bottom = top + canvas_h
                if y < top:
                    self.canvas.yview_moveto(y / total)
                elif y + h > bottom:
                    self.canvas.yview_moveto((y + h - canvas_h) / total)

    def run(self):
        _block_global_shortcuts(True)
        try:
            self.root.mainloop()
        finally:
            _block_global_shortcuts(False)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Interactive KDE shortcut browser")
    parser.add_argument("--config", default=None, help="Path to kglobalshortcutsrc")
    args = parser.parse_args()
    config_path = Path(args.config) if args.config else None
    app = WhichKeyApp(config_path)
    app.run()


if __name__ == "__main__":
    main()