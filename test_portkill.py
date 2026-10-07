import unittest

from portkill import (NO_PREFERENCE, PREFER_DARK, PREFER_LIGHT, Job, Origin, Proc, find_jobs,
                      is_dev_runner, kill_targets, parse_ss, project_of, short_cmd, theme_for)

FIXTURE = """\
LISTEN 0      4096      127.0.0.54:53   0.0.0.0:*
LISTEN 0      4096       127.0.0.1:631  0.0.0.0:*
LISTEN 0      511        127.0.0.1:6379 0.0.0.0:*
LISTEN 0      2048       127.0.0.1:8000 0.0.0.0:* users:(("python3",pid=1073211,fd=3),("python3",pid=1073209,fd=3))
LISTEN 0      4096   127.0.0.53%lo:53   0.0.0.0:*
LISTEN 0      200        127.0.0.1:5432 0.0.0.0:*
LISTEN 0      511                *:3000       *:* users:(("next-server (v1",pid=1074057,fd=22))
LISTEN 0      4096           [::1]:631     [::]:*
LISTEN 0      511            [::1]:6379    [::]:*
"""


class ParseSsTest(unittest.TestCase):
    def setUp(self):
        self.result = parse_ss(FIXTURE)

    def test_only_owned_ports(self):
        self.assertEqual(set(self.result), {3000, 8000})

    def test_multiple_pids_on_one_port(self):
        addrs, users = self.result[8000]
        self.assertEqual({pid for pid, _ in users}, {1073209, 1073211})
        self.assertEqual(users, {(1073209, "python3"), (1073211, "python3")})
        self.assertEqual(addrs, {"127.0.0.1"})

    def test_name_with_space_and_paren_and_wildcard_address(self):
        addrs, users = self.result[3000]
        self.assertEqual(users, {(1074057, "next-server (v1")})
        self.assertEqual(addrs, {"*"})

    def test_same_pid_ipv4_and_ipv6_collapse(self):
        text = (
            'LISTEN 0 511 127.0.0.1:5173 0.0.0.0:* users:(("node",pid=4242,fd=20))\n'
            'LISTEN 0 511     [::1]:5173    [::]:* users:(("node",pid=4242,fd=21))\n'
        )
        self.assertEqual(parse_ss(text),
                         {5173: ({"127.0.0.1", "[::1]"}, {(4242, "node")})})

    def test_empty_input(self):
        self.assertEqual(parse_ss(""), {})


HOME = "/home/nochaserz"
SF = "/home/nochaserz/Documents/Coding Projects/scanforge/scanforge-security-platform"
PY = f"'{SF}/.venv/bin/python3'"
PROJECT_DIRS = {SF, f"{SF}/apps/web", f"{SF}/apps/api", HOME}

P = Proc
TABLE = [
    P(1, 0, "systemd", "/sbin/init splash", None),
    P(1265, 1, "systemd", "/usr/lib/systemd/systemd --user", HOME),
    P(3676, 1265, "herdr", "/home/nochaserz/.local/bin/herdr server", HOME),
    P(3767, 3676, "bash", "/bin/bash", SF),
    P(1028346, 3767, "droid", "droid", SF),
    P(1068665, 1028346, "MainThread",
      "node /home/nochaserz/.nvm/versions/node/v24.15.0/bin/context7-mcp", SF),
    # api: make api-dev in an interactive terminal
    P(3780, 3676, "bash", "/bin/bash", SF),
    P(1073200, 3780, "make", "make api-dev", SF),
    P(1073209, 1073200, "python3",
      f"{PY} -m uvicorn app.main:app --reload --port 8000", f"{SF}/apps/api"),
    P(1073211, 1073209, "python3",
      f"{PY} -c 'from multiprocessing.spawn import spawn_main; spawn_main()'",
      f"{SF}/apps/api"),
    # web: npm run dev whose terminal is gone
    P(1074001, 1265, "npm run dev", "npm run dev", f"{SF}/apps/web"),
    P(1074020, 1074001, "sh", "sh -c 'next dev -p 3000'", f"{SF}/apps/web"),
    P(1074057, 1074020, "next-server (v1", "next-server (v16.0.1)", f"{SF}/apps/web"),
    # worker: no port, spawns scanners
    P(1075000, 1265, "python", f"{PY} -m app.worker.main", SF),
    P(1075100, 1075000, "trivy", "trivy fs --format json /tmp/scan-123", SF),
    # noise
    P(1761, 1265, "blueman-applet", "/usr/bin/python3 /usr/bin/blueman-applet", HOME),
    P(1080000, 1265, "python3", "python3 -m http.server 9999", "/tmp"),
    P(3801, 3676, "bash", "-bash", SF),
    # agent-run dev server holding a port
    P(1081744, 3767, "claude", "claude --dangerously-skip-permissions", SF),
    P(1090000, 1081744, "bash", "bash -c 'npx vite'", f"{SF}/apps/web"),
    P(1090001, 1090000, "MainThread", f"node {SF}/apps/web/node_modules/.bin/vite",
      f"{SF}/apps/web"),
]
PROCS = {p.pid: p for p in TABLE}
LISTENERS = {1073209: {8000}, 1073211: {8000}, 1074057: {3000}, 1080000: {9999},
             1090001: {5173}}


def jobs_by_root():
    jobs = find_jobs(PROCS, LISTENERS, HOME, PROJECT_DIRS.__contains__)
    return {j.root.pid: j for j in jobs}


class FindJobsTest(unittest.TestCase):
    def setUp(self):
        self.jobs = jobs_by_root()

    def test_exactly_the_expected_jobs_are_shown(self):
        self.assertEqual(sorted(self.jobs), [1073200, 1074001, 1075000, 1080000, 1090000])

    def test_api_is_one_terminal_job_rooted_at_make(self):
        self.assertEqual(self.jobs[1073200], Job(
            root=PROCS[1073200],
            procs=(PROCS[1073200], PROCS[1073209], PROCS[1073211]),
            ports=(8000,), origin=Origin.TERMINAL, project=SF))

    def test_web_orphan_lands_in_outermost_project(self):
        job = self.jobs[1074001]
        self.assertEqual([p.pid for p in job.procs], [1074001, 1074020, 1074057])
        self.assertEqual((job.ports, job.origin, job.project), ((3000,), Origin.ORPHAN, SF))

    def test_worker_without_port_is_shown_as_orphan(self):
        job = self.jobs[1075000]
        self.assertEqual([p.pid for p in job.procs], [1075000, 1075100])
        self.assertEqual((job.ports, job.origin, job.project), ((), Origin.ORPHAN, SF))

    def test_port_without_project_goes_to_other(self):
        job = self.jobs[1080000]
        self.assertEqual((job.ports, job.origin, job.project, len(job.procs)),
                         ((9999,), Origin.ORPHAN, None, 1))

    def test_agent_child_with_port_is_shown(self):
        job = self.jobs[1090000]
        self.assertEqual([p.pid for p in job.procs], [1090000, 1090001])
        self.assertEqual((job.ports, job.origin, job.project),
                         ((5173,), Origin.AGENT, SF))

    def test_mcp_server_desktop_daemon_and_idle_shell_are_hidden(self):
        hidden = {1068665, 1761, 3801, 3767, 1028346, 1081744, 1265, 3676}
        shown = {p.pid for j in self.jobs.values() for p in j.procs}
        self.assertEqual(hidden & shown, set())

    def test_missing_parent_counts_as_orphan(self):
        procs = {7: Proc(7, 6, "node", "node server.js", f"{SF}/apps/web")}
        [job] = find_jobs(procs, {}, HOME, PROJECT_DIRS.__contains__)
        self.assertEqual((job.origin, job.project), (Origin.ORPHAN, SF))


    def test_gui_app_port_never_pulls_in_desktop_session(self):
        procs = {
            1300: Proc(1300, 900, "cinnamon-sessio", "cinnamon-session --session cinnamon", HOME),
            1400: Proc(1400, 1300, "cinnamon", "cinnamon --replace", HOME),
            1450: Proc(1450, 1400, "brave", "/opt/brave.com/brave/brave", HOME),
            1500: Proc(1500, 1400, "Discord", "/usr/share/discord/Discord", HOME),
            1501: Proc(1501, 1500, "Discord", "/usr/share/discord/Discord --type=gpu", HOME),
            1600: Proc(1600, 1400, "x-terminal-emul", "gnome-terminal", HOME),
            1601: Proc(1601, 1600, "bash", "bash", SF),
            1602: Proc(1602, 1601, "make", "make web-dev", SF),
        }
        jobs = {j.root.pid: j for j in find_jobs(procs, {1500: {6463}}, HOME,
                                                 PROJECT_DIRS.__contains__)}
        self.assertEqual(sorted(jobs), [1500, 1602])
        self.assertEqual(([p.pid for p in jobs[1500].procs], jobs[1500].origin),
                         ([1500], Origin.APP))
        self.assertEqual(kill_targets(jobs[1500], procs), [1500])
        self.assertEqual(kill_targets(jobs[1602], procs), [1602])


    def test_systemd_service_is_not_a_leftover(self):
        procs = {
            1265: Proc(1265, 1, "systemd", "/usr/lib/systemd/systemd --user", HOME),
            2000: Proc(2000, 1265, "syncthing", "/usr/bin/syncthing serve", HOME, service=True),
            2100: Proc(2100, 1265, "python3", f"{PY} -m app.worker.main", SF, service=True),
        }
        jobs = find_jobs(procs, {2000: {8384}}, HOME, PROJECT_DIRS.__contains__)
        self.assertEqual([(j.root.pid, j.origin) for j in jobs], [(2000, Origin.SERVICE)])


class HelpersTest(unittest.TestCase):
    def test_nearest_git_beats_outermost_marker(self):
        wt = f"{SF}/.worktrees/feat"
        self.assertEqual(project_of(f"{wt}/apps/web", HOME, lambda d: True,
                                    lambda d: d == wt), wt)

    def test_outermost_marker_below_home(self):
        self.assertEqual(project_of(f"{SF}/apps/web/src", HOME,
                                    PROJECT_DIRS.__contains__, lambda d: False), SF)
        self.assertEqual(project_of(HOME, HOME, lambda d: True, lambda d: False), None)
        self.assertEqual(project_of("/tmp/x", HOME, lambda d: True, lambda d: False), None)

    def test_dev_runner(self):
        runner = lambda cmd: is_dev_runner(Proc(1, 0, "x", cmd, None))
        self.assertEqual([runner("/usr/bin/python3.12 -m x"), runner(f"{PY} -m x"),
                          runner("./node_modules/.bin/tsc -w"), runner("htop"),
                          runner("/usr/bin/blueman-applet")],
                         [True, True, True, False, False])

    def test_short_cmd(self):
        self.assertEqual(short_cmd(PROCS[1075000]), "python3 -m app.worker.main")
        self.assertEqual(short_cmd(PROCS[1074020]), "sh -c 'next dev -p 3000'")
        self.assertEqual(short_cmd(Proc(1, 0, "x", "x " + "a" * 80, None)),
                         "x " + "a" * 57 + "…")


class ThemeForTest(unittest.TestCase):
    INSTALLED = {"WhiteSur-Light", "WhiteSur-Dark", "Adwaita", "Adwaita-dark", "Mint-Y"}

    def pick(self, base, scheme):
        return theme_for(base, scheme, self.INSTALLED.__contains__)

    def test_follows_system_scheme_to_installed_sibling(self):
        self.assertEqual(
            [self.pick("WhiteSur-Light", PREFER_DARK), self.pick("WhiteSur-Dark", PREFER_LIGHT),
             self.pick("Adwaita", PREFER_DARK), self.pick("Adwaita-dark", PREFER_LIGHT),
             self.pick("WhiteSur-Light", NO_PREFERENCE), self.pick("Mint-Y", PREFER_DARK)],
            ["WhiteSur-Dark", "WhiteSur-Light", "Adwaita-dark", "Adwaita",
             "WhiteSur-Light", "Mint-Y"])


class KillTargetsTest(unittest.TestCase):
    def test_includes_new_children_and_skips_reused_pids(self):
        job = jobs_by_root()[1075000]
        now = {
            1075000: PROCS[1075000],
            1075100: Proc(1075100, 999, "bash", "bash", HOME),  # trivy exited, pid reused
            1075200: Proc(1075200, 1075000, "gitleaks", "gitleaks detect", SF),
            1075201: Proc(1075201, 1075200, "git", "git log", SF),
        }
        self.assertEqual(kill_targets(job, now), [1075000, 1075200, 1075201])

    def test_reused_root_pid_is_not_walked(self):
        job = jobs_by_root()[1075000]
        now = {1075000: Proc(1075000, 1, "sshd", "sshd", "/"),
               1075300: Proc(1075300, 1075000, "sshd", "sshd", "/"),
               1075100: PROCS[1075100]}
        self.assertEqual(kill_targets(job, now), [1075100])


if __name__ == "__main__":
    unittest.main()
