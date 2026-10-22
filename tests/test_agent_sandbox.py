"""The agent sandbox refuses network sockets and other processes' memory, and allows Unix sockets."""

import platform
import subprocess
import sys
import textwrap
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
