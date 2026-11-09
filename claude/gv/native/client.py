#!/usr/bin/env python3
"""Мост в окно Claude из терминала: client.py [--socket PATH] ИМЯ [JSON-аргументы].

Имена: status, list, send '{"text": …}', rewind '{"target": 2}' или '{"target": 2, "text": …}'.
Без --socket — единственная живая сессия ccg ($TMPDIR/gv-UID/PID.sock).
"""
import argparse
import json
import os
from pathlib import Path
import socket
import tempfile


def find_socket() -> str:
    alive = []
    for path in (Path(tempfile.gettempdir()) / f"gv-{os.getuid()}").glob("*.sock"):
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(0.2)
                probe.connect(str(path))
            alive.append(str(path))
        except OSError:
            pass
    if len(alive) != 1:
        raise SystemExit(f"живых сессий ccg: {len(alive)} — укажите --socket")
    return alive[0]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--socket")
    p.add_argument("name", choices=["status", "list", "send", "rewind"])
    p.add_argument("args", nargs="?", default="{}")
    a = p.parse_args()
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(15)
        s.connect(a.socket or os.environ.get("GV_NATIVE_SOCKET") or find_socket())
        s.sendall((json.dumps({"name": a.name, "args": json.loads(a.args)}) + "\n").encode())
        result = json.loads(s.makefile("r").readline())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if "error" in result else 0)


if __name__ == "__main__":
    main()
