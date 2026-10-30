"""The agent's sandbox: what the supervisor applies to itself just before it becomes the miner's agent.

    python -I -m kokoah_subnet.agent.sandbox [--uid N --gid N] [--memory-mb M] [--setenv NAME=VALUE] -- <command...>

It is started isolated (`python -I`, from a fixed environment), so nothing in the miner's bundle is imported or run
before the sandbox is in place: the agent's environment (`--setenv`, which points Python at the bundle) is set only
when the command starts, and is all the command gets.

In order: drop to the agent's user and group (when started as root), hide paths the agent must never reach (`--hide`:
the agent gets its own view of the filesystem, in which each of them is an empty read-only directory), set resource
limits (no core dumps, memory, processes, open files), set `no_new_privs`, install a seccomp filter, and exec the
command. The filter refuses
every network socket except Unix sockets (so the agent can reach the harness and nothing else, whatever its code
does), and the calls an agent never needs that widen what it can reach of the kernel (`DENIED`): other processes'
memory, io_uring (which can open sockets without the socket call), new namespaces (in one of its own the agent would
hold every capability again), mounts, kernel keyrings, BPF, performance counters, userfaultfd, modules, kexec, and
the host's clock, swap and logs. Other system calls on a foreign or x32 ABI kill the process. The filter and
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

PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
RET_ALLOW = 0x7FFF0000
RET_KILL_PROCESS = 0x80000000
RET_ERRNO = 0x00050000
AF_UNIX = 1
CLONE_NEWNS, CLONE_NEWUSER = 0x00020000, 0x10000000
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_REC, MS_PRIVATE = 1, 2, 4, 8, 0x4000, 1 << 18

# BPF opcodes
LD_W_ABS = 0x20
JEQ_K = 0x15
JGE_K = 0x35
JSET_K = 0x45
RET_K = 0x06

# clone() flags that make a namespace (clone3 cannot be read this way: it is answered "not implemented", and the C
# library falls back to clone)
CLONE_NAMESPACES = 0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000

DENIED = (
    "ptrace", "process_vm_readv", "process_vm_writev", "kcmp", "pidfd_getfd", "process_madvise",
    "io_uring_setup", "io_uring_enter", "io_uring_register",
    "unshare", "setns", "mount", "umount2", "pivot_root", "chroot", "open_tree", "move_mount", "fsopen", "fsconfig",
    "fsmount", "fspick", "mount_setattr",
    "keyctl", "add_key", "request_key", "bpf", "perf_event_open", "userfaultfd", "fanotify_init",
    "open_by_handle_at", "name_to_handle_at",
    "init_module", "finit_module", "delete_module", "kexec_load", "kexec_file_load",
    "reboot", "swapon", "swapoff", "acct", "settimeofday", "clock_settime", "syslog", "quotactl", "quotactl_fd",
    "lookup_dcookie", "nfsservctl", "vhangup",
    "modify_ldt", "iopl", "ioperm", "uselib", "ustat", "sysfs", "_sysctl", "create_module", "get_kernel_syms",
    "query_module",
)  # fmt: skip

_SHARED = {  # the same number on both architectures
    "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427, "open_tree": 428, "move_mount": 429,
    "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433, "clone3": 435, "pidfd_getfd": 438,
    "process_madvise": 440, "mount_setattr": 442, "quotactl_fd": 443,
}  # fmt: skip
SYSCALLS = {
    "x86_64": _SHARED | {
        "socket": 41, "socketpair": 53, "clone": 56, "ptrace": 101, "syslog": 103, "uselib": 134, "ustat": 136,
        "sysfs": 139, "vhangup": 153, "modify_ldt": 154, "pivot_root": 155, "_sysctl": 156, "chroot": 161,
        "acct": 163, "settimeofday": 164, "mount": 165, "umount2": 166, "swapon": 167, "swapoff": 168, "reboot": 169,
        "iopl": 172, "ioperm": 173, "create_module": 174, "init_module": 175, "delete_module": 176,
        "get_kernel_syms": 177, "query_module": 178, "quotactl": 179, "nfsservctl": 180, "lookup_dcookie": 212,
        "clock_settime": 227, "kexec_load": 246, "add_key": 248, "request_key": 249, "keyctl": 250, "unshare": 272,
        "perf_event_open": 298, "fanotify_init": 300, "name_to_handle_at": 303, "open_by_handle_at": 304,
        "setns": 308, "process_vm_readv": 310, "process_vm_writev": 311, "kcmp": 312, "finit_module": 313,
        "kexec_file_load": 320, "bpf": 321, "userfaultfd": 323,
    },
    "aarch64": _SHARED | {
        "lookup_dcookie": 18, "umount2": 39, "mount": 40, "pivot_root": 41, "nfsservctl": 42, "chroot": 51,
        "vhangup": 58, "quotactl": 60, "acct": 89, "unshare": 97, "kexec_load": 104, "init_module": 105,
        "delete_module": 106, "clock_settime": 112, "syslog": 116, "ptrace": 117, "reboot": 142,
        "settimeofday": 170, "socket": 198, "socketpair": 199, "add_key": 217, "request_key": 218, "keyctl": 219,
        "clone": 220, "swapon": 224, "swapoff": 225, "perf_event_open": 241, "fanotify_init": 262,
        "name_to_handle_at": 264, "open_by_handle_at": 265, "setns": 268, "process_vm_readv": 270,
        "process_vm_writev": 271, "kcmp": 272, "finit_module": 273, "bpf": 280, "userfaultfd": 282,
        "kexec_file_load": 294,
    },
}  # fmt: skip
ARCH = {"x86_64": (0xC000003E, 0x40000000), "aarch64": (0xC00000B7, None)}  # machine: (audit arch, x32 bit)


def _ins(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def filter_program(machine: str | None = None) -> bytes:
    """The BPF program as bytes (struct sock_filter[]), for this machine's architecture."""
    m = machine or platform.machine()
    if m not in ARCH:
        raise RuntimeError(f"no sandbox filter for architecture {m}")
    audit, x32 = ARCH[m]
    nr = SYSCALLS[m]
    deny = RET_ERRNO | errno.EACCES
    prog: list[bytes] = []
    prog.append(_ins(LD_W_ABS, 0, 0, 4))  # arch
    prog.append(_ins(JEQ_K, 1, 0, audit))  # right arch: skip the kill
    prog.append(_ins(RET_K, 0, 0, RET_KILL_PROCESS))
    prog.append(_ins(LD_W_ABS, 0, 0, 0))  # syscall number
    if x32 is not None:
        prog.append(_ins(JGE_K, 0, 1, x32))  # x32 -> kill
        prog.append(_ins(RET_K, 0, 0, RET_KILL_PROCESS))
    for name in DENIED:
        if name in nr:
            prog.append(_ins(JEQ_K, 0, 1, nr[name]))
            prog.append(_ins(RET_K, 0, 0, deny))
    prog.append(_ins(JEQ_K, 0, 1, nr["clone3"]))
    prog.append(_ins(RET_K, 0, 0, RET_ERRNO | errno.ENOSYS))
    # clone: refused when it would make a namespace
    prog.append(_ins(JEQ_K, 0, 4, nr["clone"]))
    prog.append(_ins(LD_W_ABS, 0, 0, 16))  # args[0] (the flags), low 32 bits (little endian)
    prog.append(_ins(JSET_K, 0, 1, CLONE_NAMESPACES))
    prog.append(_ins(RET_K, 0, 0, deny))
    prog.append(_ins(RET_K, 0, 0, RET_ALLOW))
    # socket and socketpair: Unix only
    prog.append(_ins(JEQ_K, 1, 0, nr["socket"]))
    prog.append(_ins(JEQ_K, 0, 4, nr["socketpair"]))
    prog.append(_ins(LD_W_ABS, 0, 0, 16))  # args[0] (the family)
    prog.append(_ins(JEQ_K, 0, 1, AF_UNIX))
    prog.append(_ins(RET_K, 0, 0, RET_ALLOW))
    prog.append(_ins(RET_K, 0, 0, deny))
    prog.append(_ins(RET_K, 0, 0, RET_ALLOW))
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


def _write(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


def hide_paths(paths: list[str]) -> None:
    """Give this process (and all it starts) its own view of the filesystem in which each path is an empty, read-only
    directory. It works where the paths themselves cannot be changed (Confidential Space mounts the launcher's folder
    read-only and its socket is open to every user). The view lives in a new user namespace that maps only the
    current user, so nothing in it can act as root; the covering mounts come locked into any namespace the agent
    makes itself, so they cannot be taken off to reach what is under them."""
    if not paths:
        return
    uid, gid = os.geteuid(), os.getegid()
    libc = ctypes.CDLL(None, use_errno=True)
    # switching users made /proc/self root's; the agent's program is dumpable again after exec anyway (ptrace stays
    # refused by the filter, core dumps by the limits)
    if libc.prctl(PR_SET_DUMPABLE, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE) failed")
    if libc.unshare(CLONE_NEWUSER | CLONE_NEWNS) != 0:
        raise OSError(ctypes.get_errno(), "could not give the agent its own view of the filesystem")
    _write("/proc/self/setgroups", "deny")
    _write("/proc/self/uid_map", f"{uid} {uid} 1")
    _write("/proc/self/gid_map", f"{gid} {gid} 1")
    if libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None) != 0:
        raise OSError(ctypes.get_errno(), "could not make the agent's view private")
    flags = MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC
    for p in paths:
        if libc.mount(b"tmpfs", os.fsencode(p), b"tmpfs", flags, b"size=4k,mode=0555") != 0:
            raise OSError(ctypes.get_errno(), f"could not hide {p}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kokoah-agent-sandbox")
    ap.add_argument("--uid", type=int)
    ap.add_argument("--gid", type=int)
    ap.add_argument("--memory-mb", type=int, default=None)
    ap.add_argument("--max-processes", type=int, default=256)
    ap.add_argument("--max-files", type=int, default=1024)
    ap.add_argument("--hide", action="append", default=[], help="a directory the agent must never reach; repeatable")
    ap.add_argument(
        "--setenv", action="append", default=[], metavar="NAME=VALUE",
        help="the command's environment, set only when it starts (all it gets); repeatable",
    )  # fmt: skip
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("a command is required after --")
    if any("=" not in item for item in args.setenv):
        ap.error("--setenv takes NAME=VALUE")
    environment = dict(item.split("=", 1) for item in args.setenv)
    if args.uid is not None:
        drop_privileges(args.uid, args.gid if args.gid is not None else args.uid)
    hide_paths(args.hide)
    apply_limits(args.memory_mb, args.max_processes, args.max_files)
    install_filter()
    os.execvpe(command[0], command, environment)
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main())
