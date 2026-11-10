"""判别力自检（一次性）：把收紧判据换回旧行为，关键断言必须变红。"""
import contextlib
import datetime as dt
import importlib.util
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("cd_check", ROOT / "tools" / "cleanup_daily.py")
cd = importlib.util.module_from_spec(spec)
sys.modules["cd_check"] = cd
spec.loader.exec_module(cd)

record = {}
cd.load_synced_fingerprints = lambda: {}
cd.collect_outputs = lambda before: (record.__setitem__("outputs_before", before), [])[1]
cd.collect_workspace_media = lambda days: []
cd.prune_download_maps = lambda apply: 0
cd.truncate_logs = lambda apply: []
cd.prune_history = lambda apply: 0
cd.collect_trash_dirs = lambda days: (record.__setitem__("trash_days", days), [])[1]
cd.free_gb_for = lambda path: 12.0

# 旧行为：只看命令行给的保留期
cd.tighten_for_low_disk = lambda free_gb, o, t, threshold=25.0: (o, t, False)

sys.argv = ["cleanup_daily.py", "--outputs-retention", "7", "--trash-days", "7"]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cd.main()

old_outputs_ok = record["outputs_before"] == dt.date.today() - dt.timedelta(days=7)
new_outputs = record["outputs_before"] == dt.date.today() - dt.timedelta(days=3)
print("旧行为下 outputs 用的是「今天-7」:", old_outputs_ok)
print("旧行为下 outputs 会是「今天-3」(新判据):", new_outputs)
assert old_outputs_ok and not new_outputs, "判别力不足：旧行为也能通过！"
assert "低磁盘水位" not in buf.getvalue(), "旧行为不该报收紧"
print("OK：把收紧判据换回旧行为后，新测试的关键断言确实会红 → 判据有判别力")
