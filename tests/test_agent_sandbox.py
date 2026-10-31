"""The agent sandbox refuses network sockets, other processes' memory and the kernel's riskier calls, allows Unix
sockets, and runs nothing of the bundle before it is in place."""

import os
import platform
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() not in ("x86_64", "aarch64"), reason="Linux seccomp only"
)


def run_sandboxed(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(code))
    return subprocess.run(
        [sys.executable, "-m", "kokoah_subnet.agent.sandbox", "--", sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_tcp_and_udp_sockets_are_refused(tmp_path: Path) -> None:
    out = run_sandboxed(
        """
        import socket
        for fam, typ in ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET6, socket.SOCK_STREAM),
                         (socket.AF_INET, socket.SOCK_DGRAM)):
            try:
                socket.socket(fam, typ)
                print("OPENED")
            except PermissionError:
                print("refused")
        """,
        tmp_path,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["refused", "refused", "refused"]


def test_unix_sockets_still_work(tmp_path: Path) -> None:
    sock_path = tmp_path / "h.sock"
    out = run_sandboxed(
        f"""
        import socket, threading
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind({str(sock_path)!r}); srv.listen(1)
        def serve():
            c, _ = srv.accept(); c.sendall(c.recv(10).upper()); c.close()
        threading.Thread(target=serve).start()
        cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cli.connect({str(sock_path)!r}); cli.sendall(b"ping"); print(cli.recv(10).decode())
        """,
        tmp_path,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "PING"


def test_children_inherit_the_filter_and_cannot_lift_it(tmp_path: Path) -> None:
    out = run_sandboxed(
        """
        import subprocess, sys
        r = subprocess.run([sys.executable, "-c",
            "import socket\\ntry:\\n socket.create_connection(('1.1.1.1', 443), timeout=2); print('OPENED')\\n"
            "except PermissionError: print('refused')"], capture_output=True, text=True)
        print(r.stdout.strip())
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        print("ptrace", libc.ptrace(16, 1, 0, 0))  # PTRACE_ATTACH to pid 1
        """,
        tmp_path,
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.split("\n")
    assert lines[0] == "refused" and lines[1] == "ptrace -1"


def test_the_kernel_calls_an_agent_never_needs_are_refused_and_threads_and_children_still_work(tmp_path: Path) -> None:
    out = run_sandboxed(
        """
        import ctypes, os, platform, subprocess, sys, threading
        from kokoah_subnet.agent.sandbox import SYSCALLS
        nr = SYSCALLS[platform.machine()]
        libc = ctypes.CDLL(None, use_errno=True)
        def call(name, *args):
            ctypes.set_errno(0)
            r = libc.syscall(nr[name], *args)
            return r, ctypes.get_errno()
        for name in ("unshare", "setns", "keyctl", "bpf", "perf_event_open", "userfaultfd", "mount", "add_key"):
            print(name, call(name, 0, 0, 0, 0, 0))
        print("clone3", call("clone3", 0, 0))
        print("clone with a new user namespace", call("clone", 0x10000000 | 17, 0, 0, 0, 0))
        try:
            os.unshare(os.CLONE_NEWUSER); print("unshare OPENED")
        except PermissionError:
            print("unshare refused")
        t = threading.Thread(target=lambda: print("thread ran")); t.start(); t.join()
        child = subprocess.run([sys.executable, "-c", "print('child ran')"], capture_output=True, text=True)
        print(child.stdout.strip())
        pid = os.fork()
        if pid == 0:
            os._exit(7)
        print("fork", os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
        """,
        tmp_path,
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    for name in ("unshare", "setns", "keyctl", "bpf", "perf_event_open", "userfaultfd", "mount", "add_key"):
        assert f"{name} (-1, 13)" in lines  # EACCES
    assert "clone3 (-1, 38)" in lines  # ENOSYS: the C library falls back to clone
    assert "clone with a new user namespace (-1, 13)" in lines
    assert lines[-4:] == ["unshare refused", "thread ran", "child ran", "fork 7"]


def test_the_filter_uses_the_kernel_s_numbers() -> None:
    import re

    from kokoah_subnet.agent.sandbox import DENIED, SYSCALLS, filter_program

    headers = {
        "x86_64": Path("/usr/include/x86_64-linux-gnu/asm/unistd_64.h"),
        "aarch64": Path("/usr/include/asm-generic/unistd.h"),  # the generic table is arm64's
    }
    checked = 0
    for machine, header in headers.items():
        assert len(filter_program(machine)) % 8 == 0
        assert {"socket", "socketpair", "clone", "clone3"} <= SYSCALLS[machine].keys()
        if not header.exists():
            continue
        found = dict(re.findall(r"#define __NR(?:3264)?_(\w+)\s+(\d+)", header.read_text()))
        for name, number in SYSCALLS[machine].items():
            assert int(found[name]) == number, (machine, name)
            checked += 1
        assert all(name in SYSCALLS[machine] for name in DENIED if name in found), machine
    if not checked:
        pytest.skip("no kernel headers here")


def test_nothing_in_the_bundle_runs_before_the_sandbox_is_in_place(tmp_path: Path) -> None:
    """A bundle holding files named like modules the sandbox imports, or `sitecustomize`, is run only by the agent's
    own Python, inside the sandbox: each records whether the seccomp filter was on (2) when it ran."""
    from kokoah_subnet.agent import supervisor

    bundle = tmp_path / "app"
    bundle.mkdir()
    record = tmp_path / "ran"
    note = (
        "import sys\n"
        "for line in open('/proc/self/status'):\n"
        "    if line.startswith('Seccomp:'):\n"
        f"        open({str(record)!r}, 'a').write(__name__ + ' ' + line.split()[1] + '\\n')\n"
    )
    for name in ("sitecustomize", "argparse", "ctypes", "platform", "struct", "resource", "errno", "os"):
        (bundle / f"{name}.py").write_text(note + ("" if name == "sitecustomize" else "raise SystemExit(3)\n"))
    command = supervisor.agent_command(
        1024, None, None, program=[sys.executable, "-c", "import os; print(os.environ['PYTHONPATH'])"],
        environment=supervisor.agent_environment(bundle),
    )  # fmt: skip
    proc = supervisor.AgentRunner(command, bundle)
    started = proc.start()
    assert started is not None and started.wait(timeout=60) == 0
    ran = record.read_text().split("\n")[:-1]
    assert ran == ["sitecustomize 2"]  # only the agent's own start-up found the bundle, already filtered


def test_core_dumps_are_off(tmp_path: Path) -> None:
    out = run_sandboxed("import resource; print(resource.getrlimit(resource.RLIMIT_CORE))", tmp_path)
    assert out.stdout.strip() == "(0, 0)"


def _user_namespaces() -> bool:
    return subprocess.run(["unshare", "-U", "-m", "true"], capture_output=True).returncode == 0


needs_root_and_namespaces = pytest.mark.skipif(
    os.geteuid() != 0 or not _user_namespaces(), reason="switching users needs root; the view needs user namespaces"
)

CONNECT = (
    "import socket, sys\n"
    "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    "try:\n    s.connect(sys.argv[1]); print('connected')\n"
    "except OSError as e:\n    print(type(e).__name__)\n"
)
# The agent tries to take the covering mount off, directly and from namespaces of its own.
UNCOVER = (
    "import ctypes, os, sys\n"
    "libc = ctypes.CDLL(None, use_errno=True)\n"
    "print('removed' if libc.umount2(sys.argv[1].encode(), 0) == 0 else 'locked',\n"
    "      'own namespaces' if libc.unshare(0x10000000 | 0x00020000) == 0 else 'no namespaces',\n"
    "      os.listdir(sys.argv[1]))\n"
)


@pytest.fixture
def launcher() -> Iterator[Path]:
    """A launcher folder as Confidential Space gives it: a socket open to every user, and a token anyone can read."""
    import shutil
    import socket
    import tempfile

    base = Path(tempfile.mkdtemp(dir="/tmp"))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.chmod(base, 0o755)
        folder = base / "container_launcher"
        folder.mkdir(mode=0o755)
        server.bind(str(folder / "teeserver.sock"))
        server.listen(4)
        os.chmod(folder / "teeserver.sock", 0o777)
        (folder / "attestation_verifier_claims_token").write_text("token")
        yield folder
    finally:
        server.close()
        shutil.rmtree(base, ignore_errors=True)


def as_agent(code: str, *args: str, hide: list[str] | None = None) -> str:
    hidden = [a for d in hide or [] for a in ("--hide", d)]
    out = subprocess.run(
        [sys.executable, "-m", "kokoah_subnet.agent.sandbox", "--uid", "65534", "--gid", "65534", *hidden, "--",
         "/usr/bin/python3", "-c", code, *args],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@needs_root_and_namespaces
def test_the_agent_cannot_reach_the_attestation_socket_though_its_folder_stays_as_it_is(launcher: Path) -> None:
    """Confidential Space mounts the launcher's folder read-only with its socket open to every user, so nothing can be
    changed there: the agent gets its own view of the filesystem in which the folder is empty."""
    sock = str(launcher / "teeserver.sock")
    assert as_agent(CONNECT, sock) == "connected"  # without the view, any user gets in
    assert as_agent(CONNECT, sock, hide=[str(launcher)]) in {"FileNotFoundError", "PermissionError"}
    assert as_agent("import os, sys; print(os.listdir(sys.argv[1]))", str(launcher), hide=[str(launcher)]) == "[]"
    assert launcher.stat().st_mode & 0o777 == 0o755  # nothing about the folder itself was changed
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as root_client:  # the harness still reaches it
        root_client.connect(sock)


@needs_root_and_namespaces
def test_the_agent_cannot_take_the_cover_off_from_namespaces_of_its_own(launcher: Path) -> None:
    assert as_agent(UNCOVER, str(launcher), hide=[str(launcher)]) == "locked no namespaces []"


@needs_root_and_namespaces
def test_the_harness_starts_the_agent_only_once_a_probe_sees_nothing(
    launcher: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kokoah_subnet.agent import supervisor

    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")  # an interpreter the probe's user can run
    assert supervisor.visible_to_agent([str(launcher)], 65534, 65534) == []
    missing = str(launcher.parent / "missing")
    assert supervisor.visible_to_agent([str(launcher), missing], 65534, 65534)  # the view failed: refuse
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    assert supervisor.visible_to_agent([str(launcher)], 65534, 65534)  # the sandbox did not start: refuse


LEAVE_CHILD = (
    "import os, sys, time\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    if sys.argv[2] == 'setsid':\n"
    "        os.setsid()  # its own session: out of the agent's process group\n"
    "    time.sleep(120)\n"
    "    os._exit(0)\n"
    "open(sys.argv[1], 'w').write(str(pid))\n"
)


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False
    return "\nState:\tZ" not in state and "\nState:\tX" not in state


def test_what_the_agent_forked_ends_with_it(tmp_path: Path) -> None:
    import threading

    from kokoah_subnet.agent import supervisor

    note = tmp_path / "child"
    runner = supervisor.AgentRunner(
        supervisor.agent_command(1024, None, None, program=[sys.executable, "-c", LEAVE_CHILD, str(note), "stay"]),
        tmp_path,
    )
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    for _ in range(100):  # the agent forks a child and exits; the runner ends the child with it
        if note.exists() and note.read_text():
            break
        time.sleep(0.05)
    child = int(note.read_text())
    for _ in range(100):
        if not _alive(child):
            break
        time.sleep(0.05)
    runner.stop()
    thread.join(timeout=10)
    assert not _alive(child)


@needs_root_and_namespaces
def test_every_process_of_the_agent_user_ends_even_one_that_left_its_group() -> None:
    import shutil
    import tempfile

    from kokoah_subnet.agent import supervisor

    uid = 61234  # a user nothing else on this machine runs as
    if supervisor.processes_of(uid):
        pytest.skip(f"uid {uid} is in use here")
    home = Path(tempfile.mkdtemp(dir="/tmp"))  # a folder the agent's user can reach (pytest's is root's alone)
    os.chmod(home, 0o777)
    request_cleanup = home
    note = home / "child"
    command = supervisor.agent_command(
        1024, uid, uid, program=["/usr/bin/python3", "-c", LEAVE_CHILD, str(note), "setsid"]
    )
    runner = supervisor.AgentRunner(command, home, uid=uid)
    try:
        first = runner.start()
        assert first is not None and first.wait(timeout=30) == 0
        child = int(note.read_text())
        assert _alive(child) and supervisor.processes_of(uid) == [child]
        again = runner.start()  # the next start ends the leftover first
        assert again is not None and again.wait(timeout=30) == 0
        assert child not in supervisor.processes_of(uid)
        runner.stop()
        assert supervisor.processes_of(uid) == []
        assert runner.start() is None  # stopped: nothing starts again
    finally:
        runner.stop()
        shutil.rmtree(request_cleanup, ignore_errors=True)


def test_a_process_name_the_agent_chose_does_not_stop_the_cleanup(tmp_path: Path) -> None:
    """The agent can rename its processes to bytes that are not text: the scan reads them as bytes."""
    from kokoah_subnet.agent import supervisor

    code = "import os, time\nopen('/proc/self/comm', 'wb').write(b'\\xff\\xfe')\ntime.sleep(30)\n"
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        time.sleep(0.3)
        assert proc.pid in supervisor.processes_of(os.getuid())
    finally:
        proc.kill()
        proc.wait()


@needs_root_and_namespaces
def test_a_stop_while_the_sandbox_is_still_root_ends_the_agent() -> None:
    import shutil
    import tempfile

    from kokoah_subnet.agent import supervisor

    uid = 61235
    if supervisor.processes_of(uid):
        pytest.skip(f"uid {uid} is in use here")
    home = Path(tempfile.mkdtemp(dir="/tmp"))
    os.chmod(home, 0o777)
    runner = supervisor.AgentRunner(
        supervisor.agent_command(1024, uid, uid, program=["/usr/bin/python3", "-c", "import time; time.sleep(120)"]),
        home,
        uid=uid,
    )
    try:
        proc = runner.start()
        assert proc is not None
        runner.stop()  # at once: the sandbox may not have dropped to the agent's user yet
        assert proc.poll() is not None
        time.sleep(0.5)
        assert supervisor.processes_of(uid) == []
    finally:
        runner.stop()
        shutil.rmtree(home, ignore_errors=True)


# The agent's program forks a child that leaves the agent's process group (and in the "thread" case, keeps running
# in a second thread after its main thread exits, so it shows as a zombie), notes its id, and the program exits.
ESCAPE = (
    "import ctypes, os, platform, sys, threading, time\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    os.setsid()\n"
    "    open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "    def kill_all():\n"
    "        while True:\n"
    "            try:\n                os.kill(-1, 9)\n"
    "            except OSError:\n                pass\n"
    "            time.sleep(0.001)\n"
    "    threading.Thread(target=kill_all).start()\n"
    "    if sys.argv[2] == 'thread':\n"
    "        time.sleep(0.2)\n"
    "        ctypes.CDLL(None).syscall({'x86_64': 60, 'aarch64': 93}[platform.machine()], 0)  # the main thread only\n"
    "    while True:\n        time.sleep(1)\n"
    "time.sleep(5)\n"  # the child's first round of kills ends this one too
)


@needs_root_and_namespaces
@pytest.mark.parametrize("how", ["group", "thread"])
def test_an_agent_child_that_left_its_group_and_kills_back_still_ends(how: str) -> None:
    """A child of the agent outside its process group, sending SIGKILL to everything its user can reach (and, in
    one case, running on with its main thread gone, so it shows as a zombie): the supervisor's own cleanup, which no
    agent can signal, ends it after the agent has exited."""
    import shutil
    import tempfile

    from kokoah_subnet.agent import supervisor

    uid = 61237 if how == "group" else 61238
    if supervisor.processes_of(uid):
        pytest.skip(f"uid {uid} is in use here")
    home = Path(tempfile.mkdtemp(dir="/tmp"))
    os.chmod(home, 0o777)
    note = home / "child"
    runner = supervisor.AgentRunner(
        supervisor.agent_command(1024, uid, uid, program=["/usr/bin/python3", "-c", ESCAPE, str(note), how]), home,
        uid=uid,
    )  # fmt: skip
    try:
        proc = runner.start()
        assert proc is not None
        proc.wait(timeout=30)  # the agent's program is gone (its own child killed it); the child is not
        time.sleep(0.5)
        child = int(note.read_text())
        assert supervisor.processes_of(uid) == [child]
        runner.stop()
        assert supervisor.processes_of(uid) == []
    finally:
        runner.stop()
        shutil.rmtree(home, ignore_errors=True)


def test_a_collected_group_leader_counts_as_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """pidfd_open answers EINVAL for a group leader collected while its group lives on: gone, not a failed kill."""
    import ctypes
    import errno

    from kokoah_subnet.agent import supervisor

    class Collected:
        def syscall(self, *args: object) -> int:
            ctypes.set_errno(errno.EINVAL)
            return -1

    monkeypatch.setattr(supervisor, "_libc", Collected())
    supervisor._kill_if_owned(os.getpid(), os.getuid())  # no error


def test_one_process_that_cannot_be_killed_never_spares_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    from kokoah_subnet.agent import supervisor

    running = {11, 12, 13}
    killed: list[int] = []

    def kill(pid: int, uid: int) -> None:
        killed.append(pid)
        if pid == 11 and len(killed) == 1:
            raise supervisor.KillFailed("refused once")
        running.discard(pid)

    monkeypatch.setattr(supervisor, "processes_of", lambda uid: sorted(running))
    monkeypatch.setattr(supervisor, "_kill_if_owned", kill)
    supervisor.kill_user_processes(1234)
    assert killed[:3] == [11, 12, 13] and not running  # the round went on, and the next one took the first


def test_a_kill_that_cannot_be_made_is_never_taken_for_done(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import ctypes
    import errno

    from kokoah_subnet.agent import supervisor

    class Refusing:  # the kernel refuses the pidfd (a container that does not allow it, say)
        def syscall(self, *args: object) -> int:
            ctypes.set_errno(errno.EPERM)
            return -1

    monkeypatch.setattr(supervisor, "_libc", Refusing())
    me = os.getuid()
    with pytest.raises(supervisor.KillFailed, match="pidfd_open"):  # not a PermissionError: no 403 on a signed stop
        supervisor.kill_user_processes(me, rounds=1)
    kill = supervisor.kill_user_processes
    monkeypatch.setattr(supervisor, "kill_user_processes", lambda uid: kill(uid, rounds=2))  # not 1000 slow rounds
    runner = supervisor.AgentRunner([sys.executable, "-c", "pass"], tmp_path, uid=me)
    with pytest.raises(supervisor.KillFailed):
        runner.start()  # no new agent beside processes that could not be ended
    assert runner.proc is None
