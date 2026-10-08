"""Hash local research records without publishing their contents or opening databases."""
from __future__ import annotations
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def audit(root):
    records, outcomes = [], Counter()
    for base in (root / '.popper', root / 'evaluation/runs', root / 'integrations/runs'):
        if not base.exists():
            continue
        for path in sorted(base.rglob('*')):
            if not path.is_file() or path.suffix not in {'.json', '.log', '.sqlite', '.db'}:
                continue
            if path.suffix in {'.sqlite', '.db'}:
                records.append({'path': path.relative_to(root).as_posix(),
                                'bytes': path.stat().st_size, 'status': 'database_not_opened'})
                continue
            content = path.read_bytes()
            status, category = 'unclassified', None
            if path.suffix == '.json':
                try:
                    obj = json.loads(content)
                    if isinstance(obj, dict):
                        value = obj.get('status')
                        if isinstance(value, str) and value in {'success', 'failed', 'completed', 'inconclusive', 'pass', 'error'}:
                            status = value
                        error = obj.get('error_type')
                        if isinstance(error, str) and error.isidentifier() and len(error) <= 80:
                            category = error
                except (ValueError, UnicodeError):
                    status = 'invalid_json'
            outcomes[status] += 1
            records.append({'path': path.relative_to(root).as_posix(),
                            'run': path.relative_to(base).parts[0],
                            'sha256': hashlib.sha256(content).hexdigest(),
                            'bytes': len(content), 'status': status, 'failure_category': category})
    return {'schema': 'local-record-inventory/v1', 'raw_contents_published': False,
            'databases_opened': False, 'counts': dict(outcomes), 'records': records}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(Path(__file__).resolve().parents[1])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'records': len(result['records']), 'counts': result['counts']}))


if __name__ == '__main__':
    main()
