"""The agent's sandbox: what the supervisor applies to itself just before it becomes the miner's agent.

    python -m kokoah_subnet.agent.sandbox [--uid N --gid N] [--memory-mb M] -- <command...>

In order: drop to the agent's user and group (when started as root), set resource limits (no core dumps, memory,
processes, open files), set `no_new_privs`, install a seccomp filter, and exec the command. The filter refuses
every network socket except Unix sockets (so the agent can reach the harness and nothing else, whatever its code
does), io_uring (which can open sockets without the socket call) and reading or writing other processes' memory
(ptrace, process_vm_readv/writev). Other system calls on a foreign or x32 ABI kill the process. The filter and
`no_new_privs` survive exec and are inherited by every child, so nothing the agent starts can undo them.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import platform
import resource
import struct
import sys

PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
RET_ALLOW = 0x7FFF0000
RET_KILL_PROCESS = 0x80000000
RET_ERRNO = 0x00050000
AF_UNIX = 1

# BPF opcodes
LD_W_ABS = 0x20
JEQ_K = 0x15
JGE_K = 0x35
RET_K = 0x06

ARCH = {
    # machine: (audit arch, socket, io_uring_setup, ptrace, process_vm_readv, process_vm_writev, x32 bit)
    "x86_64": (0xC000003E, 41, 425, 101, 310, 311, 0x40000000),
    "aarch64": (0xC00000B7, 198, 425, 117, 270, 271, None),
}


def _ins(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def filter_program(machine: str | None = None) -> bytes:
    """The BPF program as bytes (struct sock_filter[]), for this machine's architecture."""
    m = machine or platform.machine()
    if m not in ARCH:
        raise RuntimeError(f"no sandbox filter for architecture {m}")
    audit, sock, io_uring, ptrace, vm_read, vm_write, x32 = ARCH[m]
    deny = RET_ERRNO | errno.EACCES
    prog: list[bytes] = []
    # labels are resolved by position: build with placeholders, then patch the jumps
    prog.append(_ins(LD_W_ABS, 0, 0, 4))  # 0: arch
    prog.append(_ins(JEQ_K, 1, 0, audit))  # 1: right arch -> 3
    prog.append(_ins(RET_K, 0, 0, RET_KILL_PROCESS))  # 2
    prog.append(_ins(LD_W_ABS, 0, 0, 0))  # 3: syscall number
    body: list[tuple[int, int]] = []  # (syscall, action) where action: 0 deny, 1 socket check
    if x32 is not None:
        prog.append(_ins(JGE_K, 0, 1, x32))  # 4: x32 -> kill
        prog.append(_ins(RET_K, 0, 0, RET_KILL_PROCESS))  # 5
    for nr in (io_uring, ptrace, vm_read, vm_write):
        body.append((nr, 0))
    for nr, _ in body:
        prog.append(_ins(JEQ_K, 0, 1, nr))
        prog.append(_ins(RET_K, 0, 0, deny))
    prog.append(_ins(JEQ_K, 1, 0, sock))  # socket -> check the family
    prog.append(_ins(RET_K, 0, 0, RET_ALLOW))
    prog.append(_ins(LD_W_ABS, 0, 0, 16))  # args[0], low 32 bits (little endian)
    prog.append(_ins(JEQ_K, 0, 1, AF_UNIX))
    prog.append(_ins(RET_K, 0, 0, RET_ALLOW))
    prog.append(_ins(RET_K, 0, 0, deny))
    return b"".join(prog)


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def install_filter() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
    program = filter_program()
    buf = ctypes.create_string_buffer(program, len(program))
    fprog = _SockFprog(len(program) // 8, ctypes.cast(buf, ctypes.c_void_p))
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_SECCOMP) failed")


def apply_limits(memory_mb: int | None, max_processes: int, max_files: int) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (max_files, max_files))
    resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))
    if memory_mb:
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 2**20, memory_mb * 2**20))


def drop_privileges(uid: int, gid: int) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.getuid() != uid or os.geteuid() != uid:
        raise RuntimeError("could not drop to the agent user")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kokoah-agent-sandbox")
    ap.add_argument("--uid", type=int)
    ap.add_argument("--gid", type=int)
    ap.add_argument("--memory-mb", type=int, default=None)
    ap.add_argument("--max-processes", type=int, default=256)
    ap.add_argument("--max-files", type=int, default=1024)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("a command is required after --")
    if args.uid is not None:
        drop_privileges(args.uid, args.gid if args.gid is not None else args.uid)
    apply_limits(args.memory_mb, args.max_processes, args.max_files)
    install_filter()
    os.execvp(command[0], command)
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main())
