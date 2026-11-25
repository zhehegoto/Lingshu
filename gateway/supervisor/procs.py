"""子进程启停 + Windows Job Object。

设计要点：
- 所有子进程被 assign 进同一个 Job Object，且该 Job 设了
  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE：gateway 进程消失（含被 taskkill /F）
  时，内核会连带杀掉所有子进程，不留孤儿占显存。
- 这里只管"进程"，不认识 sd / llama 的语义（那是 backends/ 和 supervisor/ 的事）。
- 起进程统一加 CREATE_NO_WINDOW，避免弹黑框。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------- Win32 常量与结构 ----------------

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
CREATE_NO_WINDOW = 0x08000000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED = 0x1000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
kernel32.SetInformationJobObject.restype = wt.BOOL
kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
kernel32.AssignProcessToJobObject.restype = wt.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobObjectError(RuntimeError):
    pass


class JobObject:
    """一个带 KILL_ON_JOB_CLOSE 的 Job Object。close() 即杀光成员进程。"""

    def __init__(self) -> None:
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise JobObjectError(f"CreateJobObjectW 失败: {ctypes.get_last_error()}")

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            self._handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            code = ctypes.get_last_error()
            kernel32.CloseHandle(self._handle)
            self._handle = None
            raise JobObjectError(f"SetInformationJobObject 失败: {code}")

    def assign(self, pid: int) -> None:
        hproc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED, False, pid)
        if not hproc:
            raise JobObjectError(f"OpenProcess 失败 pid={pid}: {ctypes.get_last_error()}")
        try:
            if not kernel32.AssignProcessToJobObject(self._handle, hproc):
                raise JobObjectError(f"AssignProcessToJobObject 失败 pid={pid}: {ctypes.get_last_error()}")
        finally:
            kernel32.CloseHandle(hproc)

    def close(self) -> None:
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    @property
    def alive(self) -> bool:
        return bool(self._handle)


@dataclass
class Proc:
    name: str
    exe: Path
    popen: subprocess.Popen
    log_out: Path
    log_err: Path
    _out_fh: object = field(default=None, repr=False)
    _err_fh: object = field(default=None, repr=False)

    @property
    def pid(self) -> int:
        return self.popen.pid

    def alive(self) -> bool:
        return self.popen.poll() is None


class ProcessManager:
    def __init__(self, logs_dir: Path) -> None:
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.job = JobObject()
        self.procs: dict[str, Proc] = {}

    def spawn(self, name: str, exe: str | Path, args: list[str]) -> Proc:
        exe = Path(exe)
        if not exe.exists():
            raise FileNotFoundError(f"{name} 可执行文件不存在: {exe}")
        if name in self.procs and self.procs[name].alive():
            raise RuntimeError(f"{name} 已在运行 pid={self.procs[name].pid}")

        log_out = self.logs_dir / f"{name}.log"
        log_err = self.logs_dir / f"{name}.err.log"
        out_fh = open(log_out, "a", encoding="utf-8", errors="replace")
        err_fh = open(log_err, "a", encoding="utf-8", errors="replace")
        out_fh.write(f"\n===== spawn {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        out_fh.flush()

        popen = subprocess.Popen(
            [str(exe), *args],
            stdout=out_fh,
            stderr=err_fh,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            cwd=str(exe.parent),
        )
        self.job.assign(popen.pid)
        proc = Proc(name=name, exe=exe, popen=popen, log_out=log_out, log_err=log_err,
                    _out_fh=out_fh, _err_fh=err_fh)
        self.procs[name] = proc
        return proc

    def kill(self, name: str, timeout: float = 5.0) -> bool:
        proc = self.procs.get(name)
        if proc is None:
            return False
        if proc.popen.poll() is not None:
            self._close_fhs(proc)
            return False
        proc.popen.terminate()
        try:
            proc.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.popen.kill()
            proc.popen.wait(timeout=timeout)
        self._close_fhs(proc)
        return True

    def stop_all(self) -> None:
        for name in list(self.procs):
            self.kill(name)

    def shutdown(self) -> None:
        """先优雅杀子进程，再关 Job Object（关句柄会兜底杀掉漏网的）。"""
        self.stop_all()
        self.job.close()

    @staticmethod
    def _close_fhs(proc: Proc) -> None:
        for fh in (proc._out_fh, proc._err_fh):
            try:
                if fh:
                    fh.close()
            except Exception:
                pass
        proc._out_fh = proc._err_fh = None


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def pids_on_port(port: int) -> list[int]:
    """用 netstat + tasklist 查监听某端口的 PID。仅 Windows。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return []
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper().startswith("TCP"):
            local, state, pid = parts[1], parts[3], parts[4]
            if state.upper() == "LISTENING" and local.endswith(f":{port}"):
                try:
                    pids.add(int(pid))
                except ValueError:
                    pass
    return sorted(pids)


def exe_path_of_pid(pid: int) -> str | None:
    """按 PID 取可执行文件完整路径（用于"确认是本项目 exe 才杀"）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
    except Exception:
        return None
    if not out or out.startswith("信息:") or "INFO:" in out:
        return None
    # 只有映像名，没有完整路径；完整路径走 wmic 已废弃，改用 PowerShell 由上层另行处理
    return out.split(",")[0].strip('"') if "," in out else None


def now() -> float:
    return time.perf_counter()


def file_tail(path: str | Path, lines: int = 12) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except Exception:
        return ""
