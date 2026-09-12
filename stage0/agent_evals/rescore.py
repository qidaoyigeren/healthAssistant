"""用当前评分协议重评既有产物，输出到**新**目录。

旧产物一律只读：重评写新文件，并标注 ``rescored_by`` 与 ``not_comparable_to``。
缺字段的记 ``undetermined``——补造证据比承认不可判定更糟。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import scoring

DATA = Path(__file__).with_name('visitprep_dev.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--in-dir', required=True)
    parser.add_argument('--out-dir', required=True)
    args = parser.parse_args()
    index = {task['task_id']: task
             for task in json.loads(DATA.read_text(encoding='utf-8'))}
    source, target = Path(args.in_dir), Path(args.out_dir)
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob('*.json')):
        artifact = json.loads(path.read_text(encoding='utf-8'))
        result = scoring.rescore(artifact, index)
        result['rescored_by'] = scoring.PROTOCOL
        result['source_file'] = path.name
        (target / path.name).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"{path.name}: {json.dumps(result['summary'], ensure_ascii=False)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
