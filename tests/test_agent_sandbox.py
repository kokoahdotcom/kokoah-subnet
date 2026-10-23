"""The agent sandbox refuses network sockets and other processes' memory, and allows Unix sockets."""

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
# The agent makes its own namespaces and tries to take the covering mount off.
UNCOVER = (
    "import ctypes, os, sys\n"
    "libc = ctypes.CDLL(None, use_errno=True)\n"
    "uid, gid = os.geteuid(), os.getegid()\n"
    "assert libc.unshare(0x10000000 | 0x00020000) == 0\n"
    "open('/proc/self/setgroups', 'w').write('deny')\n"
    "open('/proc/self/uid_map', 'w').write(f'0 {uid} 1')\n"
    "open('/proc/self/gid_map', 'w').write(f'0 {gid} 1')\n"
    "print('removed' if libc.umount2(sys.argv[1].encode(), 0) == 0 else 'locked', os.listdir(sys.argv[1]))\n"
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
    assert as_agent(UNCOVER, str(launcher), hide=[str(launcher)]) == "locked []"


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
