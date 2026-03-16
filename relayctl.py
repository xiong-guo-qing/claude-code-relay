#!/usr/bin/env python3
"""relayctl - manage claude-code-relay config.json

Usage:
  python3 relayctl.py list
  python3 relayctl.py add <alias_model_id> <target_openai_model>
  python3 relayctl.py del <alias_model_id>

Examples:
  python3 relayctl.py add claude-sonnet-4-6 gpt-5.2
  python3 relayctl.py add claude-opus-4-6 gpt-5.2
  python3 relayctl.py del claude-opus-4

Notes:
- Relay hot-reloads config.json on change; no service restart needed.
- /v1/models output is derived from model_map keys (excluding 'default').
"""

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
P = BASE / 'config.json'


def load():
    return json.loads(P.read_text(encoding='utf-8'))


def save(cfg):
    P.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding='utf-8')


def die(msg, code=2):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def main(argv):
    if len(argv) < 2:
        die(__doc__)
    cmd = argv[1]
    cfg = load()
    mm = cfg.setdefault('model_map', {})

    if cmd == 'list':
        for k in sorted(mm.keys()):
            print(f"{k} -> {mm[k]}")
        return 0

    if cmd == 'add':
        if len(argv) != 4:
            die('Usage: relayctl.py add <alias_model_id> <target_openai_model>')
        alias_id, target = argv[2], argv[3]
        mm[alias_id] = target
        if alias_id != 'default' and 'default' not in mm:
            mm['default'] = target
        save(cfg)
        print(f"✓ added: {alias_id} -> {target}")
        return 0

    if cmd == 'del':
        if len(argv) != 3:
            die('Usage: relayctl.py del <alias_model_id>')
        alias_id = argv[2]
        if alias_id not in mm:
            die(f"not found: {alias_id}")
        del mm[alias_id]
        save(cfg)
        print(f"✓ deleted: {alias_id}")
        return 0

    die(f"Unknown cmd: {cmd}")


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
