"""诊断：核对报 input_too_large 的那个 run 的真实素材与记录（只读）。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
THREAD = "5c8ff0a5-52e3-4442-95a4-7b26b62a9412"
RUN = "13268f86-1522-43f4-ada4-35cdfc72b9e6"
LIMIT = 32 * 1024 * 1024


def main() -> None:
    con = sqlite3.connect(ROOT / "data" / "agent.sqlite3")
    con.row_factory = sqlite3.Row
    print("--- runs by run_id ---")
    for row in con.execute("select * from runs where run_id=?", (RUN,)):
        print(dict(row))
    print("--- runs by thread_id ---")
    for row in con.execute("select * from runs where thread_id=?", (THREAD,)):
        print(dict(row))
    print("--- events ---")
    try:
        cols = [d[1] for d in con.execute("pragma table_info(events)")]
        print("events cols:", cols)
        for row in con.execute(
            "select * from events where run_id=? order by rowid", (RUN,)
        ):
            print(dict(row))
    except Exception as exc:  # noqa: BLE001
        print("events err:", exc)
    print("--- artifacts ---")
    try:
        for row in con.execute(
            "select * from artifacts where run_id=?", (RUN,)
        ):
            d = dict(row)
            print({k: d[k] for k in d if k != "payload"})
    except Exception as exc:  # noqa: BLE001
        print("artifacts err:", exc)
    print("--- operations ---")
    try:
        for row in con.execute(
            "select * from operations where run_id=?", (RUN,)
        ):
            print(dict(row))
    except Exception as exc:  # noqa: BLE001
        print("operations err:", exc)

    inputs = ROOT / "data" / "runs" / RUN / "inputs"
    print("--- inputs dir ---", inputs)
    if inputs.is_dir():
        for path in sorted(inputs.iterdir()):
            size = path.stat().st_size
            print(
                f"{path.name} bytes={size} MB={size / 1024 / 1024:.2f} "
                f"over_32MiB={size > LIMIT} delta={size - LIMIT}"
            )
    else:
        print("missing")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
