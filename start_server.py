# -*- coding: utf-8 -*-
"""Detached launcher for jobapply server; child self-logs to data/server_out.log."""
import subprocess, sys, os, time

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "data", "server_out.log")

def main():
    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    # rotate log
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 512 * 1024:
            os.replace(LOG, LOG + ".old")
    except Exception:
        pass
    code = (
        "import subprocess,sys,os,time\n"
        "os.chdir(r'%s')\n"
        "log=open(r'%s','ab',buffering=0)\n"
        "log.write(('[launcher] start %%s\\n' %% time.strftime('%%F %%T')).encode())\n"
        "p=subprocess.Popen([sys.executable,'server.py'],cwd=r'%s',stdout=log,stderr=subprocess.STDOUT)\n"
        "log.write(('[launcher] pid=%%d\\n' %% p.pid).encode())\n"
        "rc=p.wait()\n"
        "log.write(('[launcher] exited rc=%%d\\n' %% rc).encode())\n"
    ) % (BASE, LOG, BASE)
    child = os.path.join(BASE, "data", "_server_child.py")
    with open(child, "w", encoding="ascii") as f:
        f.write(code)
    DETACHED = 0x00000008 | 0x00000200
    p = subprocess.Popen([sys.executable, child], cwd=BASE, creationflags=DETACHED)
    print("launcher pid=%d" % p.pid, flush=True)
    time.sleep(8)
    # probe
    import urllib.request
    for attempt in range(6):
        try:
            r = urllib.request.urlopen("http://127.0.0.1:8899/api/status", timeout=3)
            print("PROBE OK %s %s" % (r.status, r.read().decode("utf-8", "replace")[:200]), flush=True)
            return
        except Exception as e:
            print("probe %d fail: %s" % (attempt, e), flush=True)
            time.sleep(3)
    print("PROBE FAILED after retries; see data/server_out.log", flush=True)

if __name__ == "__main__":
    main()
