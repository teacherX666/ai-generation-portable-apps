import os
import subprocess
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_src = os.path.join(_here, "src")
_venv_py = os.path.join(_here, ".venv", "Scripts", "python.exe")

if os.path.exists(_venv_py):
    # 关键：这个包放在 src/ 下、且**没有安装进 venv**，所以
    # `-m feishu_generation_agent.main` 必须能看到 src，否则直接
    # ModuleNotFoundError 崩掉。
    # 之前这里漏了这一步，导致 Portal 每次拉起都崩（日志被刷到 16MB），
    # 8765 上一直跑的是手工启动的实例，把这个坑盖住了 —— 直到 Portal
    # 重启、手工实例消失，才暴露出来。
    env = dict(os.environ)
    _existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _src + (os.pathsep + _existing if _existing else "")
    cmd = [_venv_py, "-m", "feishu_generation_agent.main"] + sys.argv[1:]
    sys.exit(subprocess.call(cmd, cwd=_here, env=env))

if _src not in sys.path:
    sys.path.insert(0, _src)

from feishu_generation_agent.main import main

if __name__ == "__main__":
    main()
