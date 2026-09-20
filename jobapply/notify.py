"""jobapply 系统弹窗提示模块（Windows 优先，失败静默降级）。

策略：写 PowerShell NotifyIcon 气球脚本到 data/_notify.ps1（UTF-8-BOM），
中文经命令行 args（UTF-16）传入，detached Popen 后台执行，绝不阻塞、绝不抛异常。
"""
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PS1_PATH = BASE / "data" / "_notify.ps1"

_PS1 = r"""param([string]$Title, [string]$Message)
try {
    Add-Type -AssemblyName System.Windows.Forms
    $n = New-Object System.Windows.Forms.NotifyIcon
    $n.Icon = [System.Drawing.SystemIcons]::Information
    $n.Visible = $true
    $n.BalloonTipTitle = $Title
    $n.BalloonTipText = $Message
    $n.ShowBalloonTip(8000)
    Start-Sleep -Seconds 8
    $n.Dispose()
} catch {}
"""


def _ensure_ps1() -> Path:
    """确保 _notify.ps1 存在（UTF-8-BOM，PowerShell 才能正确读中文）。"""
    PS1_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PS1_PATH.exists() or PS1_PATH.read_text(encoding="utf-8-sig", errors="replace") != _PS1:
        with open(PS1_PATH, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write(_PS1)
    return PS1_PATH


def notify(title: str, message: str) -> bool:
    """弹系统通知。成功 True；任何失败降级 print，返回 False，绝不抛异常。"""
    try:
        if sys.platform != "win32":
            print(f"[notify] {title}: {message}", flush=True)
            return False
        ps1 = _ensure_ps1()
        # 截断防命令行过长
        title = (title or "jobapply")[:80]
        message = (message or "")[:240]
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        # 后台气球：不从父进程 job 连坐（与 _launch 一致加 BREAKAWAY）
        creationflags |= 0x00000008 | 0x00000200 | 0x01000000
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1), "-Title", title, "-Message", message],
            creationflags=creationflags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(BASE),
        )
        return True
    except Exception as e:  # 任何失败降级为控制台输出
        print(f"[notify fallback] {title}: {message} ({e})", flush=True)
        return False


if __name__ == "__main__":
    ok = notify("jobapply 测试", "这是一条系统弹窗测试消息")
    print("notify ok" if ok else "notify fallback", flush=True)
