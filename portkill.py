#!/usr/bin/env python3
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

USER_RE = re.compile(r'\("((?:[^"\\]|\\.)*)",pid=(\d+)')
C_FLAG = re.compile(r"-[a-zA-Z]*c[a-zA-Z]*")
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "nu"}
HOSTS = {"claude", "droid", "codex", "gemini", "opencode", "aider", "amp", "cursor-agent",
         "goose", "crush", "code", "cursor", "zed", "windsurf", "apex-code", "prime-agent"}
RUNNERS = {"make", "npm", "npx", "pnpm", "yarn", "bun", "bunx", "deno", "node", "tsx",
           "ts-node", "python", "uv", "uvicorn", "gunicorn", "celery", "flask", "fastapi",
           "hypercorn", "daphne", "vite", "next", "nodemon", "turbo", "cargo", "go", "air",
           "ruby", "rails", "bundle", "php", "java", "gradle", "mvn", "docker-compose",
           "dotnet", "just", "task", "honcho", "foreman", "overmind", "watchexec"}
MARKERS = (".git", "package.json", "pyproject.toml", "Makefile", "Cargo.toml", "go.mod",
           "docker-compose.yml", "compose.yaml", "Gemfile", "composer.json",
           "requirements.txt")


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    name: str
    cmdline: str  # shlex-joined argv, so paths with spaces survive a round trip
    cwd: str | None
    service: bool = False  # lives in a systemd `.service` cgroup, so not a terminal leftover


class Origin(Enum):
    TERMINAL = "terminal"
    ORPHAN = "orphan"
    AGENT = "agent"
    APP = "app"
    SERVICE = "service"


@dataclass(frozen=True)
class Job:
    root: Proc
    procs: tuple[Proc, ...]
    ports: tuple[int, ...]
    origin: Origin
    project: str | None


# ---- pure core -------------------------------------------------------------

def argv(p: Proc) -> list[str]:
    try:
        return shlex.split(p.cmdline)
    except ValueError:
        return p.cmdline.split()


def _base(path: str, strip_version=False) -> str:
    base = path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[\d.]+$", "", base) if strip_version else base


def boundary(p: Proc | None) -> Origin | None:
    if p is None or p.pid <= 1 or p.name == "systemd":
        return Origin.ORPHAN
    args = argv(p) or [p.name]
    # `sh -c '...'` and `bash script.sh` belong to a job; only a bare shell is a terminal.
    if p.name in SHELLS and all(a[:1] == "-" and not C_FLAG.fullmatch(a) for a in args[1:]):
        return Origin.TERMINAL
    names = {p.name, _base(args[0])}
    if len(args) > 1 and _base(args[0], True) in {"node", "bun", "python"}:
        names.add(_base(args[1]))  # script hosts (gemini, amp, aider) run as node/python
    return Origin.AGENT if names & HOSTS else None


def is_dev_runner(p: Proc) -> bool:
    a0 = (argv(p) or [p.name])[0]
    return ("/.venv/bin/" in "/" + a0 or "/node_modules/.bin/" in "/" + a0
            or _base(a0, True) in RUNNERS)


def project_of(cwd: str | None, home: str, is_project_dir: Callable[[str], bool],
               is_repo: Callable[[str], bool]) -> str | None:
    home = home.rstrip("/")
    if not cwd or not cwd.startswith(home + "/"):
        return None
    d, outermost = cwd.rstrip("/"), None
    while d != home:
        if is_repo(d):
            return d
        if is_project_dir(d):
            outermost = d
        d = os.path.dirname(d)
    return outermost


def is_glue(p: Proc) -> bool:
    return is_dev_runner(p) or (p.name in SHELLS and boundary(p) is None)


def _subtree(root: int, procs: dict[int, Proc], kids: dict[int, list[int]],
             edge: dict[int, Origin | None]) -> list[int]:
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        out.append(pid)
        todo += [k for k in kids.get(pid, ()) if edge[k] is None and k not in out]
    return sorted(out)


def _index(procs: dict[int, Proc]):
    kids: dict[int, list[int]] = {}
    for p in procs.values():
        kids.setdefault(p.ppid, []).append(p.pid)
    return kids, {pid: boundary(p) for pid, p in procs.items()}


def group(procs: dict[int, Proc]) -> dict[int, tuple[Origin, list[int]]]:
    """Root pid -> (origin, member pids). Every non-boundary process lands in exactly one job.

    Only dev runners and `sh -c` glue own their subtree. Anything else (cinnamon-session, a
    GUI app) is a job of just itself, so a port held by one desktop app can never pull the
    whole session into a Stop.
    """
    kids, edge = _index(procs)
    todo = [(pid, edge.get(p.ppid) or Origin.ORPHAN) for pid, p in procs.items()
            if edge[pid] is None and (p.ppid not in procs or edge[p.ppid] is not None)]
    out = {}
    while todo:
        pid, origin = todo.pop()
        if origin is Origin.ORPHAN and procs[pid].service:
            origin = Origin.SERVICE
        if is_glue(procs[pid]):
            out[pid] = (origin, _subtree(pid, procs, kids, edge))
        else:
            out[pid] = (origin, [pid])
            todo += [(k, Origin.APP) for k in kids.get(pid, ()) if edge[k] is None]
    return out


def find_jobs(procs: dict[int, Proc], listeners: dict[int, set[int]], home: str,
              is_project_dir: Callable[[str], bool],
              is_repo: Callable[[str], bool] = lambda d: False) -> list[Job]:
    jobs = []
    for r, (origin, pids) in group(procs).items():
        root, members = procs[r], tuple(procs[p] for p in pids)
        ports = tuple(sorted({port for p in pids for port in listeners.get(p, ())}))
        cwd = root.cwd or next((p.cwd for p in members if p.cwd), None)
        project = project_of(cwd, home, is_project_dir, is_repo)
        if ports or (origin in (Origin.TERMINAL, Origin.ORPHAN) and project
                     and is_dev_runner(root)):
            jobs.append(Job(root, members, ports, origin, project))
    return jobs


def kill_targets(job: Job, procs: dict[int, Proc]) -> list[int]:
    """The root's current job (if the root pid wasn't reused) plus known pids not reused."""
    targets = {p.pid for p in job.procs if p.pid in procs and procs[p.pid].name == p.name}
    root = procs.get(job.root.pid)
    if root and root.name == job.root.name:
        kids, edge = _index(procs)
        targets |= set(_subtree(root.pid, procs, kids, edge) if is_glue(root) else [root.pid])
    return sorted(targets)


def short_cmd(p: Proc, limit: int = 60) -> str:
    args = argv(p) or [p.name]
    args[0] = _base(args[0])
    if len(args) > 1 and args[1].startswith("/"):
        args[1] = _base(args[1])
    text = shlex.join(args)
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---- boundary --------------------------------------------------------------

def parse_ss(text: str) -> dict[int, tuple[set[str], set[tuple[int, str]]]]:
    out: dict[int, tuple[set[str], set[tuple[int, str]]]] = {}
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 6 or "users:(" not in fields[5]:
            continue
        addr, _, port = fields[3].rpartition(":")
        if not port.isdigit():
            continue
        users = {(int(pid), name) for name, pid in USER_RE.findall(fields[5])}
        if not users:
            continue
        addrs, pids = out.setdefault(int(port), (set(), set()))
        addrs.add(addr)
        pids |= users
    return out


def read_listeners() -> dict[int, set[int]]:
    try:
        text = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict[int, set[int]] = {}
    for port, (_, users) in parse_ss(text).items():
        for pid, _ in users:
            out.setdefault(pid, set()).add(port)
    return out


def read_procs() -> dict[int, Proc]:
    uid, out = os.getuid(), {}
    for entry in filter(str.isdigit, os.listdir("/proc")):
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            with open(f"/proc/{entry}/stat") as f:
                head, _, tail = f.read().rpartition(")")  # comm may contain ")" and spaces
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                args = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
            state, ppid = tail.split()[:2]
        except (OSError, ValueError):
            continue
        if state == "Z":
            continue
        try:
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except OSError:
            cwd = None
        try:
            with open(f"/proc/{entry}/cgroup") as f:
                service = f.read().strip().endswith(".service")
        except OSError:
            service = False
        # npm/node rewrite their title into one string ("npm run dev"); real paths stay whole.
        if len(args) == 1 and " " in args[0] and not os.path.exists(args[0]):
            args = args[0].split()
        name = head.partition("(")[2]
        out[int(entry)] = Proc(int(entry), int(ppid), name, shlex.join(args or [name]), cwd,
                               service)
    return out


def has_marker(d: str) -> bool:
    return any(os.path.exists(os.path.join(d, m)) for m in MARKERS)


def snapshot() -> list[Job]:
    jobs = find_jobs(read_procs(), read_listeners(), os.path.expanduser("~"), has_marker,
                     lambda d: os.path.exists(os.path.join(d, ".git")))
    jobs = [j for j in jobs if all(p.pid != os.getpid() for p in j.procs)]
    return sorted(jobs, key=lambda j: (j.project is None, j.project or "",
                                       j.ports[0] if j.ports else 1 << 17, j.root.pid))


def _alive(pid: int) -> bool:
    # A zombie still answers kill(pid, 0) until its parent reaps it, so read the state.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rpartition(")")[2].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def terminate(job: Job) -> str | None:
    pids, denied = kill_targets(job, read_procs()), set()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                denied.add(pid)
        deadline = time.monotonic() + 2
        while pids and time.monotonic() < deadline:
            pids = [p for p in pids if p not in denied and _alive(p)]
            time.sleep(0.05)
        if not pids:
            break
    if denied:
        return f"Permission denied for PID {', '.join(map(str, sorted(denied)))}"
    return f"PID {', '.join(map(str, pids))} did not exit" if pids else None


# ---- UI --------------------------------------------------------------------

PALETTES = {
    "light": dict(bg="#F6F7F9", ink="#1F2430", muted="#687083", faint="#8A91A1",
                  rule="#E2E5EB", lamp="#C2620A", lamp_hover="#A8540A", lamp_text="#FFFFFF",
                  lamp_off="#E4B98F", stop="#C42B1C", stop_wash="alpha(#C42B1C, 0.08)",
                  hover="alpha(#1F2430, 0.035)", disabled="#A9AFBB"),
    "dark": dict(bg="#1C2029", ink="#E6E9EF", muted="#9BA3B4", faint="#7F8798",
                 rule="#2C313C", lamp="#F0A24A", lamp_hover="#F5B566", lamp_text="#1C2029",
                 lamp_off="#5C4630", stop="#FF7A6B", stop_wash="alpha(#FF7A6B, 0.14)",
                 hover="alpha(#E6E9EF, 0.04)", disabled="#5A6170"),
}
CSS = b"""
window, list { background-color: @pk_bg; color: @pk_ink; font-family: Inter; }
row { padding: 0; border: none; background: none; }
row:hover { background-color: @pk_hover; }
row.leftover { box-shadow: inset 3px 0 @pk_lamp; }

.port { font-size: 26px; font-weight: 300; font-feature-settings: "tnum"; color: @pk_ink;
        min-width: 84px; }
row.leftover .port { color: @pk_lamp; }
.cmd { font-family: "JetBrainsMono NF", monospace; font-size: 12.5px; color: @pk_ink; }
.status { font-size: 12px; color: @pk_muted; }
row.leftover .status { color: @pk_lamp; font-weight: 600; }
.error { font-size: 12px; color: @pk_stop; }

.project { font-size: 15px; font-weight: 600; color: @pk_ink; }
.path { font-size: 11.5px; color: @pk_faint; }
.project-head { border-top: 1px solid @pk_rule; }
.project-head.first { border-top: none; }

button.stop { background: none; border: none; box-shadow: none; color: @pk_muted;
              font-weight: 500; padding: 4px 10px; }
button.stop:hover, button.stop:focus { color: @pk_stop; background-color: @pk_stop_wash; }
button.stop:disabled { color: @pk_disabled; }

button.sweep { background-image: none; background-color: @pk_lamp; color: @pk_lamp_text;
               border: none; box-shadow: none; font-weight: 600; padding: 3px 12px; }
button.sweep:hover { background-color: @pk_lamp_hover; }
button.sweep:disabled { background-color: @pk_lamp_off; color: @pk_lamp_text; }

.empty-title { font-size: 17px; font-weight: 600; color: @pk_ink; }
.empty-body { font-size: 12.5px; color: @pk_muted; }
"""


def palette_css(name: str) -> bytes:
    return "".join(f"@define-color pk_{k} {v};\n" for k, v in PALETTES[name].items()).encode()


def is_dark(rgba) -> bool:
    return 0.2126 * rgba.red + 0.7152 * rgba.green + 0.0722 * rgba.blue < 0.5


THEME_DIRS = ("/usr/share/themes", os.path.expanduser("~/.themes"),
              os.path.expanduser("~/.local/share/themes"))
NO_PREFERENCE, PREFER_DARK, PREFER_LIGHT = 0, 1, 2  # org.freedesktop.appearance color-scheme


def theme_for(base: str, scheme: int, exists: Callable[[str], bool]) -> str:
    """The installed variant of `base` matching the system color scheme, else `base`."""
    if scheme == PREFER_DARK:
        options = [base.replace("-Light", "-Dark"), base + "-Dark", base + "-dark"]
    elif scheme == PREFER_LIGHT:
        options = [base.replace("-Dark", "-Light"), base.replace("-Dark", ""),
                   base.replace("-dark", "")]
    else:
        options = []
    return next((t for t in options if t != base and exists(t)), base)


def theme_installed(name: str) -> bool:
    return any(os.path.isdir(os.path.join(d, name, "gtk-3.0")) for d in THEME_DIRS)


STATUS = {Origin.ORPHAN: "Left running", Origin.TERMINAL: "In an open terminal",
          Origin.AGENT: "Started by an agent", Origin.APP: "Desktop app",
          Origin.SERVICE: "Background service"}


def leftovers(jobs: list[Job]) -> list[Job]:
    return [j for j in jobs if j.origin is Origin.ORPHAN]


def main() -> None:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gio, GLib, Gtk, Pango

    def label(text="", classes=(), markup=None, **kw):
        w = Gtk.Label(label=text, **{"xalign": 0, **kw})
        if markup is not None:
            w.set_markup(markup)
        for c in classes:
            w.get_style_context().add_class(c)
        return w

    def stop_button(text, busy, on_click):
        b = Gtk.Button(label="Stopping…" if busy else text, valign=Gtk.Align.CENTER,
                       sensitive=not busy)
        b.get_style_context().add_class("stop")
        b.connect("clicked", lambda _b: on_click())
        return b

    def vbox(*children, spacing=0, **kw):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kw)
        for c in children:
            box.add(c)
        return box

    def short_path(path):
        home = os.path.expanduser("~")
        parent = os.path.dirname(path)
        return "~" + parent[len(home):] if parent.startswith(home) else parent

    class JobRow(Gtk.ListBoxRow):
        def __init__(self, job: Job, child):
            super().__init__(activatable=False)
            self.job = job
            if job.origin is Origin.ORPHAN:
                self.get_style_context().add_class("leftover")
            self.add(child)

    class App(Gtk.Window):
        def __init__(self):
            super().__init__(title="Ports")
            self.set_default_size(600, 460)
            screen, prio = Gdk.Screen.get_default(), Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            self.palette = Gtk.CssProvider()
            rules = Gtk.CssProvider()
            rules.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(screen, self.palette, prio)
            Gtk.StyleContext.add_provider_for_screen(screen, rules, prio)
            self.apply_palette()
            self.settings = Gtk.Settings.get_default()
            self.base_theme = self.applied_theme = self.settings.props.gtk_theme_name
            self.scheme = NO_PREFERENCE
            self.settings.connect("notify::gtk-theme-name", self.on_theme_changed)
            self.settings.connect("notify::gtk-application-prefer-dark-theme",
                                  lambda *_: GLib.idle_add(self.apply_palette))
            self.watch_color_scheme()
            self.header = Gtk.HeaderBar(title="Ports", show_close_button=True)
            self.sweep = Gtk.Button(valign=Gtk.Align.CENTER, no_show_all=True)
            self.sweep.get_style_context().add_class("sweep")
            self.sweep.connect("clicked", lambda _b: self.stop(leftovers(self.current)))
            self.header.pack_start(self.sweep)
            self.set_titlebar(self.header)
            self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            self.listbox.set_header_func(self.make_header)
            empty = vbox(
                label("Nothing left running", ["empty-title"], xalign=0.5),
                label("Servers and workers you start in a project folder show up here, "
                      "so you can stop them when a port is taken.", ["empty-body"],
                      xalign=0.5, justify=Gtk.Justification.CENTER, wrap=True,
                      max_width_chars=44),
                spacing=6, valign=Gtk.Align.CENTER, margin=32)
            empty.show_all()
            self.listbox.set_placeholder(empty)
            scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            scroller.add(self.listbox)
            self.add(scroller)
            self.current: list[Job] = []
            self.stopping: set[int] = set()
            self.errors: dict[int, str] = {}
            self.refresh(force=True)
            GLib.timeout_add_seconds(2, self.refresh)

        def watch_color_scheme(self):
            try:
                portal = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
                    "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                    "org.freedesktop.portal.Settings", None)
                value = portal.call_sync("ReadOne", GLib.Variant(
                    "(ss)", ("org.freedesktop.appearance", "color-scheme")),
                    Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
            except GLib.Error:
                return  # no portal: keep following the GTK theme alone
            portal.connect("g-signal", self.on_portal_signal)
            self.set_scheme(value)

        def on_portal_signal(self, _proxy, _sender, signal_name, params):
            namespace, key, value = params.unpack()
            if (signal_name, namespace, key) == (
                    "SettingChanged", "org.freedesktop.appearance", "color-scheme"):
                self.set_scheme(value)

        def set_scheme(self, scheme):
            self.scheme = scheme
            self.settings.props.gtk_application_prefer_dark_theme = scheme == PREFER_DARK
            theme = theme_for(self.base_theme, scheme, theme_installed)
            if theme != self.settings.props.gtk_theme_name:
                self.applied_theme = theme
                self.settings.props.gtk_theme_name = theme
            GLib.idle_add(self.apply_palette)

        def on_theme_changed(self, *_):
            name = self.settings.props.gtk_theme_name
            if name != self.applied_theme:  # the user picked a new desktop theme
                self.base_theme = self.applied_theme = name
                self.set_scheme(self.scheme)
            GLib.idle_add(self.apply_palette)

        def apply_palette(self):
            # Read the theme actually rendering the header bar, so the list always matches it.
            found, bg = self.get_style_context().lookup_color("theme_bg_color")
            self.palette.load_from_data(palette_css("dark" if found and is_dark(bg) else "light"))
            return False

        def refresh(self, force=False):
            snap = snapshot()
            if force or snap != self.current:
                self.current = snap
                live = {j.root.pid for j in snap}
                self.errors = {p: e for p, e in self.errors.items() if p in live}
                self.rebuild()
            return True

        def rebuild(self):
            for child in self.listbox.get_children():
                self.listbox.remove(child)
            left = leftovers(self.current)
            busy = bool(left) and all(j.root.pid in self.stopping for j in left)
            self.sweep.set_label("Stopping…" if busy else f"Stop {len(left)} left running")
            self.sweep.set_sensitive(not busy)
            self.sweep.set_visible(bool(left))
            n = len(self.current)
            self.header.set_subtitle(f"{n} running" if n else "")
            for job in self.current:
                self.listbox.add(self.make_row(job))
            self.listbox.show_all()

        def make_header(self, row, before):
            project = row.job.project
            if before is not None and before.job.project == project:
                return row.set_header(None)
            name = os.path.basename(project) if project else "Other"
            text = vbox(label(name, ["project"], ellipsize=Pango.EllipsizeMode.END), spacing=1)
            box = Gtk.Box(spacing=12, margin_start=20, margin_end=14, margin_top=18,
                          margin_bottom=4)
            box.pack_start(text, True, True, 0)
            if project:  # no bulk stop for the unrelated grab bag under "Other"
                text.add(label(short_path(project), ["path"], tooltip_text=project,
                               ellipsize=Pango.EllipsizeMode.START))
                jobs = [j for j in self.current if j.project == project]
                if len(jobs) > 1:
                    busy = all(j.root.pid in self.stopping for j in jobs)
                    box.pack_end(stop_button("Stop all", busy, lambda: self.stop(jobs)),
                                 False, False, 0)
            head = vbox(box)
            head.get_style_context().add_class("project-head")
            if before is None:
                head.get_style_context().add_class("first")
            head.show_all()
            row.set_header(head)

        def make_row(self, job: Job) -> Gtk.ListBoxRow:
            port = label(str(job.ports[0]) if job.ports else "", ["port"], xalign=1,
                         valign=Gtk.Align.START, tooltip_text="Listening on " + ", ".join(
                             f"localhost:{p}" for p in job.ports) if job.ports else None)
            more = f"   +{len(job.ports) - 1} more ports" if len(job.ports) > 1 else ""
            text = vbox(
                label(short_cmd(job.root), ["cmd"], ellipsize=Pango.EllipsizeMode.END,
                      tooltip_text=job.root.cmdline),
                label(STATUS[job.origin] + more, ["status"], tooltip_text="\n".join(
                    f"PID {p.pid}  {short_cmd(p, 80)}" for p in job.procs)),
                spacing=3, valign=Gtk.Align.CENTER)
            if job.root.pid in self.errors:
                text.add(label(self.errors[job.root.pid], ["error"], wrap=True))
            box = Gtk.Box(spacing=16, margin_start=8, margin_end=14, margin_top=7,
                          margin_bottom=7)
            box.pack_start(port, False, False, 0)
            box.pack_start(text, True, True, 0)
            box.pack_end(stop_button("Stop", job.root.pid in self.stopping,
                                     lambda: self.stop([job])), False, False, 0)
            return JobRow(job, box)

        def stop(self, jobs):
            for job in jobs:
                if job.root.pid in self.stopping:
                    continue
                self.stopping.add(job.root.pid)
                self.errors.pop(job.root.pid, None)
                threading.Thread(target=lambda j=job: GLib.idle_add(
                    self.on_stopped, j.root.pid, terminate(j)), daemon=True).start()
            self.rebuild()

        def on_stopped(self, pid, err):
            self.stopping.discard(pid)
            if err:
                self.errors[pid] = err
            self.refresh(force=True)
            return False

    win = App()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    win.set_focus(None)
    Gtk.main()


if __name__ == "__main__":
    main()
