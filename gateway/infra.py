"""启动期基础设施守卫（infra_guard）。

**规则（本文件的存在理由）**：Windows 上凡是要"跨进程保证"的东西，先问它是内核对象还是
用户态约定。文件区域锁、锁文件、标记文件都是用户态约定——对方不遵守约定就拦不住；
Mutex / Job Object / Event 是内核对象，进程死了由系统释放。同一个问题在这台机器上踩过两次：
`wmic` 已被移除（别把可靠性押在外部命令上）、`msvcrt.locking` 跨进程无效（别把跨进程保证
押在用户态约定上）。以后遇到单实例、生命周期、跨进程信号，直接上内核对象。

四个实现要点：
1. 端口检测必须"LISTENING + PID + 完整路径"三段比对——只比进程名会误杀用户从别处下的同名 sd.cpp。
2. 单实例用命名互斥体，不用端口 8000——8000 可能被用户其他服务占。
3. 杀完要轮询端口释放，taskkill 返回成功 ≠ 端口能立刻 bind（TIME_WAIT）。
4. 显存高但端口干净 = 别人的 GPU 应用在跑，只记录不阻断，名字叫 vram_occupied_on_boot 而不是 dirty。
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import msvcrt
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("infra")


class InstanceAlreadyRunning(RuntimeError):
    pass


@dataclass
class PortOwner:
    port: int
    pid: int
    name: str
    exe_path: str | None


@dataclass
class GuardReport:
    killed: list[PortOwner] = field(default_factory=list)
    skipped: list[PortOwner] = field(default_factory=list)
    released: list[int] = field(default_factory=list)
    release_timeout: list[int] = field(default_factory=list)
    vram_occupied_on_boot: int | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"killed={[(o.port, o.pid) for o in self.killed]} "
            f"skipped={[(o.port, o.pid, o.exe_path) for o in self.skipped]} "
            f"released={self.released} timeout={self.release_timeout} "
            f"vram_on_boot={self.vram_occupied_on_boot}"
        )


# ---------------- 单实例锁 ----------------

class InstanceLock:
    """单实例用 Windows 命名互斥体，不用端口 8000（8000 可能被用户其他服务占）。

    为什么不用 msvcrt 文件区域锁：实测两个独立进程之间拦不住（第二个进程照样拿到锁）。
    命名互斥体是内核对象，进程退出（含崩溃）自动释放，比文件锁可靠。

    名字带安装目录哈希：原来是一个固定名字，导致同一台机器上放两份灵枢（比如 C 盘一份、
    D 盘一份，或者新旧两个版本）互相顶掉，报"已有一份 gateway 在运行"。现在只拦"同一个
    目录被开两遍"——那才是真双开。
    """
    PREFIX = "Local\\doubao-local-ai-gateway-"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.name = self.PREFIX + hashlib.sha1(
            str(self.path.parent.resolve()).encode("utf-8")).hexdigest()[:12]
        self._handle = None

    def acquire(self) -> None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        h = k32.CreateMutexW(None, 0, self.name)
        err = ctypes.get_last_error()
        if not h:
            raise RuntimeError(f"CreateMutexW 失败: {err}")
        if err == 183:                                  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(ctypes.c_void_p(h))
            pid = ""
            try:
                pid = self.path.read_text(encoding="utf-8").strip()
            except Exception:
                pass
            raise InstanceAlreadyRunning(f"已有一份 gateway 在运行（PID {pid or '未知'}）")

        self._handle = h
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass

    def release(self) -> None:
        if self._handle:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


# ---------------- 端口 → PID → 路径 ----------------

class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("dwState", ctypes.c_ulong),
                ("dwLocalAddr", ctypes.c_ulong),
                ("dwLocalPort", ctypes.c_ulong),
                ("dwRemoteAddr", ctypes.c_ulong),
                ("dwRemotePort", ctypes.c_ulong),
                ("dwOwningPid", ctypes.c_ulong)]


def pids_listening(port: int) -> list[int]:
    """返回 LISTENING 在该端口的 PID 列表（Windows，纯 iphlpapi）。

    不用 netstat：实测在 uvicorn 子进程里 `netstat -ano` 返回码 0 但 stdout 为空，
    函数会静默返回 []，infra_guard 于是完全失效——这是"外部命令输出不可靠"的又一例。
    GetExtendedTcpTable 是内核 API，进程内直接查询，不 spawn 子进程。
    """
    AF_INET, TCP_TABLE_OWNER_PID_LISTENER = 2, 3
    try:
        iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
        size = ctypes.c_ulong(0)
        iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET,
                                  TCP_TABLE_OWNER_PID_LISTENER, 0)
        if size.value == 0:
            return []
        buf = ctypes.create_string_buffer(size.value)
        ret = iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                        TCP_TABLE_OWNER_PID_LISTENER, 0)
        if ret != 0:
            log.warning("GetExtendedTcpTable 失败 ret=%s", ret)
            return []

        count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0]
        rows = ctypes.cast(ctypes.byref(buf, ctypes.sizeof(ctypes.c_ulong)),
                           ctypes.POINTER(_MIB_TCPROW_OWNER_PID))
        out: list[int] = []
        for i in range(count):
            row = rows[i]
            local_port = _ntohs(row.dwLocalPort & 0xFFFF)
            if local_port == port and row.dwOwningPid:
                out.append(int(row.dwOwningPid))
        return sorted(set(out))
    except Exception as e:
        log.warning("pids_listening 失败: %s", e)
        return []


def _ntohs(value: int) -> int:
    return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)


def process_name(pid: int) -> str:
    """进程名从 QueryFullProcessImageNameW 的路径取，不走 tasklist。

    tasklist 与 netstat 是同一类风险：外部命令在被包装的进程里可能返回空。
    """
    path = process_exe_path(pid)
    if path:
        return Path(path).name
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10, check=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        out = (out or "").strip()
        if not out or "INFO" in out or "信息" in out:
            return ""
        return out.split(",")[0].strip('"')
    except Exception:
        return ""


def process_exe_path(pid: int) -> str | None:
    """取完整路径。优先 QueryFullProcessImageNameW（无外部依赖），再回落 wmic / PowerShell。

    wmic 在 Windows 11 已默认移除，PowerShell 回退在部分受限环境下也拿不到——
    所以主路径必须是 ctypes，别把可靠性押在外部命令上。
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.QueryFullProcessImageNameW.restype = ctypes.c_int
    k32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)
    ]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if h:
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_ulong(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            k32.CloseHandle(h)

    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"processid={pid}", "get", "ExecutablePath", "/value"],
            capture_output=True, text=True, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.splitlines():
            if line.lower().startswith("executablepath="):
                p = line.split("=", 1)[1].strip()
                if p:
                    return p
    except Exception:
        pass
    return None


def same_exe(actual: str | None, expected: Path) -> bool:
    """按绝对路径比对，但**不跟随 junction/symlink**——否则拷贝一份到别的目录
    再用 junction 指回来会被误判成本项目进程。"""
    if not actual:
        return False
    a = os.path.normpath(os.path.abspath(actual)).casefold()
    b = os.path.normpath(os.path.abspath(str(expected))).casefold()
    return a == b


def port_released(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pids_listening(port):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", port))
                    return True
            except OSError:
                pass
        time.sleep(0.5)
    return False


def kill_pid(pid: int) -> bool:
    r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True, timeout=15, check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0


# ---------------- 主入口 ----------------

def vram_used_mib(nvidia_smi: Path | None) -> int | None:
    if not nvidia_smi:
        return None
    try:
        out = subprocess.run([str(nvidia_smi), "--query-gpu=memory.used",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8, check=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        return int(out.splitlines()[0]) if out else None
    except Exception:
        return None


def infra_guard(
    ports: dict[int, Path],
    nvidia_smi: Path | None,
    vram_warn_mib: int = 2000,
    release_timeout: float = 5.0,
) -> GuardReport:
    """ports: {端口: 期望的本项目 exe 绝对路径}"""
    rep = GuardReport()

    for port, expected in ports.items():
        for pid in pids_listening(port):
            owner = PortOwner(port, pid, process_name(pid), process_exe_path(pid))
            if same_exe(owner.exe_path, expected):
                log.warning("发现残留 %s PID %s 占着 :%s，强杀", owner.name, pid, port)
                if kill_pid(pid):
                    rep.killed.append(owner)
                else:
                    rep.notes.append(f"kill_failed:{pid}")
            else:
                log.warning(":%s 被非本项目进程占用（PID %s, 路径 %s），跳过不杀",
                            port, pid, owner.exe_path)
                rep.skipped.append(owner)

    for port in ports:
        if port_released(port, release_timeout):
            if any(o.port == port for o in rep.killed):
                rep.released.append(port)
        else:
            rep.release_timeout.append(port)

    used = vram_used_mib(nvidia_smi)
    busy_ports = [p for p in ports if pids_listening(p)]
    if used is not None and used > vram_warn_mib and not busy_ports:
        rep.vram_occupied_on_boot = used
        log.info("开机时显存已占 %s MiB（无本项目端口占用），视为其他 GPU 应用，不阻断启动", used)

    return rep
