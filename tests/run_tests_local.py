"""本机没有 pytest，用最小 runner 直接跑 tests/ 下的测试函数。

只实现用到的两个 fixture：
  tmp_path   -> 每个测试一个临时目录（pathlib.Path）
  monkeypatch -> setattr / setitem / delattr，测试结束自动还原

用法（在 ai-generation-portable-apps 目录下）：
    python tests/run_tests_local.py tests/test_job_zombie_queue.py
不传路径则跑 tests/ 下全部 test_*.py。
"""
import importlib.util
import inspect
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value=None, raising=True):
        if isinstance(target, str):
            module_name, _, attr = target.rpartition(".")
            target = importlib.import_module(module_name)
            name, value = attr, name
        had = hasattr(target, name)
        old = getattr(target, name, None)
        self._undo.append((target, name, had, old))
        setattr(target, name, value)

    def delattr(self, target, name):
        had = hasattr(target, name)
        old = getattr(target, name, None)
        self._undo.append((target, name, had, old))
        if had:
            delattr(target, name)

    def setitem(self, mapping, key, value):
        had = key in mapping
        old = mapping.get(key)
        self._undo.append((mapping, key, had, old, True))
        mapping[key] = value

    def undo(self):
        for item in reversed(self._undo):
            if len(item) == 5:
                mapping, key, had, old, _ = item
                if had:
                    mapping[key] = old
                else:
                    mapping.pop(key, None)
            else:
                target, name, had, old = item
                if had:
                    setattr(target, name, old)
                else:
                    try:
                        delattr(target, name)
                    except AttributeError:
                        pass
        self._undo.clear()


def run_file(path: Path) -> tuple[int, int]:
    spec = importlib.util.spec_from_file_location("runner_" + path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except ImportError as exc:
        # 有些测试依赖 pytest（本机没装），跳过而不是整轮中断
        print("  SKIP (import error: %s)" % exc)
        return 0, 0

    passed = failed = 0
    for name, fn in sorted(vars(mod).items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        params = list(inspect.signature(fn).parameters)
        kwargs = {}
        mp = MonkeyPatch()
        tmpdir = None
        if "tmp_path" in params:
            tmpdir = tempfile.TemporaryDirectory()
            kwargs["tmp_path"] = Path(tmpdir.name)
        if "monkeypatch" in params:
            kwargs["monkeypatch"] = mp
        try:
            fn(**kwargs)
            passed += 1
            print("  PASS %s" % name)
        except Exception:
            failed += 1
            print("  FAIL %s" % name)
            traceback.print_exc()
        finally:
            mp.undo()
            if tmpdir:
                tmpdir.cleanup()
    return passed, failed


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or sorted((ROOT / "tests").glob("test_*.py"))
    total_pass = total_fail = 0
    for path in targets:
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        print("== %s" % path.name)
        p, f = run_file(path)
        total_pass += p
        total_fail += f
    print("\n%d passed, %d failed" % (total_pass, total_fail))
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
