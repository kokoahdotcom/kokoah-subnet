"""The agent sandbox refuses network sockets, other processes' memory and the kernel's riskier calls, allows Unix
sockets, and runs nothing of the bundle before it is in place."""

import os
import platform
import subprocess
import sys
import textwrap
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
    assert started.wait(timeout=60) == 0
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
