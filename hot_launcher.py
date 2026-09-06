# -*- coding: utf-8 -*-
"""热更新中间人：由旧服务 spawn（detached）。
职责：确保旧进程死亡（TerminateProcess，沙箱下可靠）→ 等待端口/Qdrant 释放 → 启动新 app.py。

用法：python hot_launcher.py <旧进程PID>
"""
import ctypes
import os
import subprocess
import sys
import time

PROCESS_TERMINATE = 0x0001


def kill_pid(pid: int) -> bool:
    """终止旧进程：Windows 使用 Win32 API 强杀，macOS/Linux 使用 SIGKILL。"""
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if not h:
                return False
            ok = kernel32.TerminateProcess(h, 1)
            kernel32.CloseHandle(h)
            return bool(ok)
        except Exception:
            return False
    else:
        try:
            import signal
            os.kill(pid, signal.SIGKILL)
            return True
        except Exception:
            return False


def main():
    if len(sys.argv) < 2:
        print("[HotLauncher] 缺少旧进程 PID 参数")
        return
    old_pid = int(sys.argv[1])

    # 1) 等待旧服务把响应完整返回客户端
    time.sleep(2)

    # 2) 强制终止旧进程（os._exit 在沙箱环境下不可靠，TerminateProcess 已验证可靠）
    if kill_pid(old_pid):
        print(f"[HotLauncher] 旧进程 {old_pid} 已终止")
    else:
        print(f"[HotLauncher] 旧进程 {old_pid} 可能已自行退出")

    # 3) 等待端口与 Qdrant 锁释放
    time.sleep(3)

    # 4) 启动新服务（日志追加到 ino_hot.log）
    log_path = os.path.join(os.getcwd(), "ino_hot.log")
    # open() 的文件对象句柄默认可继承（os.open 的 fd 默认不可继承，子进程拿不到会丢日志）
    log_f = open(log_path, "a", encoding="utf-8", buffering=1)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [sys.executable, "-u", "app.py"],  # -u：无缓冲，日志实时落盘到 ino_hot.log
        cwd=os.getcwd(),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=log_f,
        close_fds=True,
    )
    print(f"[HotLauncher] 新服务已启动 (pid 待查)")


if __name__ == "__main__":
    main()
