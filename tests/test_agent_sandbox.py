"""The agent sandbox refuses network sockets and other processes' memory, and allows Unix sockets."""

import os
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


@pytest.mark.skipif(os.geteuid() != 0, reason="switching to the agent's user needs root")
def test_the_agent_user_cannot_reach_the_attestation_socket() -> None:
    """The harness (root) asks the attestation socket for tokens; the agent's user must not. Closing the socket's
    directory to everyone but root does it, whatever the socket file's own mode."""
    import shutil
    import socket
    import tempfile

    from kokoah_subnet.agent.supervisor import close_to_agent

    base = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        os.chmod(base, 0o755)
        launcher = base / "container_launcher"
        launcher.mkdir(mode=0o755)
        sock = launcher / "teeserver.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock))
        server.listen(4)
        os.chmod(sock, 0o777)
        probe = base / "probe.py"
        probe.write_text(
            "import socket, sys\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "try:\n    s.connect(sys.argv[1]); print('connected')\n"
            "except PermissionError:\n    print('refused')\n"
        )
        os.chmod(probe, 0o644)

        def as_agent() -> str:
            out = subprocess.run(
                [sys.executable, "-m", "kokoah_subnet.agent.sandbox", "--uid", "65534", "--gid", "65534", "--",
                 "/usr/bin/python3", str(probe), str(sock)],
                capture_output=True, text=True, timeout=60,
            )  # fmt: skip
            assert out.returncode == 0, out.stderr
            return out.stdout.strip()

        assert as_agent() == "connected"  # an open directory lets any user in
        assert close_to_agent([launcher]) == []
        assert as_agent() == "refused"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as root_client:  # the harness still reaches it
            root_client.connect(str(sock))
        server.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)
