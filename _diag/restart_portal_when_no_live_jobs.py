"""等「没有在飞的生成任务」就重启 Portal，装上新的后端代码。

为什么不用 `watch_until_idle.py`：它的判据是「连续静默 180 秒」，工作时段团队一直在提交，
这条几乎永远不成立（实测等了 6 分钟一直是 BUSY）。真正决定「重启会不会打断任务」的是
**有没有非终态任务**（running / 排队 / 待审批都算），所以这里用：

    live == 0  且  距上次提交 ≥ 30 秒（防「刚提交、还没进活动日志」的竞态）
    连续确认 2 次（间隔 10 秒），杀进程前再复核一次

满足即调 `restart_portal_if_idle.py --force --reason ...`（--force 是它留的授权口子，
理由会写进日志留痕）。**live==0 时子应用没有任务在跑，重启不会打断任何人生成。**
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import watch_until_idle as idle  # noqa: E402

MIN_QUIET_SUBMIT = 30
CONFIRMS_NEEDED = 2
POLL_SECONDS = 10
MAX_MINUTES = 240


def log(msg: str) -> None:
    # GBK 控制台（本仓库老坑）：打印 emoji/替换字符会抛 UnicodeEncodeError，
    # 把「刚重启完、还没打印结果」的脚本整死。必须降级打印。
    line = time.strftime("[%H:%M:%S]") + " " + msg
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("gbk", "replace").decode("gbk"), flush=True)


def main() -> int:
    deadline = time.time() + MAX_MINUTES * 60
    confirms = 0
    while time.time() < deadline:
        try:
            st = idle.idle_status()
        except Exception as exc:  # 读状态失败绝不当成空闲
            log(f"idle_status 失败（当忙处理）: {exc}")
            confirms = 0
            time.sleep(POLL_SECONDS)
            continue
        live = int(st.get("live") or 0)
        quiet_submit = float(st.get("quiet_submit") or 0)
        quiet_activity = float(st.get("quiet_activity") or 0)
        ok = live == 0 and quiet_submit >= MIN_QUIET_SUBMIT and quiet_activity >= MIN_QUIET_SUBMIT
        log(f"live={live} quiet_submit={quiet_submit:.0f}s quiet_activity={quiet_activity:.0f}s -> {'可重启' if ok else '等'}")
        confirms = confirms + 1 if ok else 0
        if confirms >= CONFIRMS_NEEDED:
            # 杀之前复核：这一刻还有任务就退回等待
            st2 = idle.idle_status()
            if int(st2.get("live") or 0) != 0 or float(st2.get("quiet_submit") or 0) < MIN_QUIET_SUBMIT:
                log(f"复核发现又忙了（live={st2.get('live')}），继续等")
                confirms = 0
                time.sleep(POLL_SECONDS)
                continue
            reason = (f"使用统计导出改中文表头（commit a9d5d5e）需重启生效；"
                      f"已确认 live=0 连续 {CONFIRMS_NEEDED} 次、提交静默 ≥{MIN_QUIET_SUBMIT}s，"
                      f"此刻无任何排队/生成中任务，重启不打断生成")
            log("确认无在飞任务 -> 重启 Portal：" + reason)
            r = subprocess.run(
                [sys.executable, str(HERE / "restart_portal_if_idle.py"), "--force", "--reason", reason],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            log("restart rc=%s\n%s%s" % (r.returncode, r.stdout or "", r.stderr or ""))
            return r.returncode
        time.sleep(POLL_SECONDS)
    log(f"等满 {MAX_MINUTES} 分钟一直有任务在跑，未重启（什么都没做）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
