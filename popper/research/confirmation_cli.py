"""File transport for a holdout authority and a separately operated runner.

This command does not configure OS permissions. The current Windows runner is
an engineering backend; deploying these roles in one account is not a blind
evaluation. Candidate code never runs in the HoldoutService process.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..core import ProtocolError, canonical, read_json
from .confirmation_contracts import load_private_key, public_key_b64
from .confirmation_service import HoldoutService


def _save(path, value, text=False):
    path = Path(path)
    encoded = value + '\n' if text else canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(encoded)
    except FileExistsError:
        if path.read_text(encoding='utf-8') != encoded:
            raise ProtocolError('Refusing to overwrite a different confirmation artifact')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    keygen = commands.add_parser('keygen')
    keygen.add_argument('--private-key', type=Path, required=True)
    keygen.add_argument('--public-out', type=Path, required=True)
    register = commands.add_parser('register')
    for name in ('service-dir', 'project', 'holdout', 'runner-public-key', 'out', 'public-out'):
        register.add_argument('--' + name, type=Path, required=True)
    for name in ('dataset-id', 'dataset-version', 'evaluation-group', 'runtime-id'):
        register.add_argument('--' + name, required=True)
    begin = commands.add_parser('begin')
    begin.add_argument('--bundle', type=Path, required=True)
    for name in ('features', 'complete', 'fail-interrupted'):
        sub = commands.add_parser(name)
        sub.add_argument('--ticket', type=Path, required=True)
        if name == 'complete':
            sub.add_argument('--runner-result', type=Path, required=True)
        if name == 'fail-interrupted':
            sub.add_argument('--runner-stopped', action='store_true', required=True,
                             help='Operator has verified no runner/scorer is still active')
    for name in ('begin', 'features', 'complete', 'fail-interrupted'):
        sub = commands.choices[name]
        sub.add_argument('--service-dir', type=Path, required=True)
        sub.add_argument('--out', type=Path, required=True)
    run = commands.add_parser('run')
    for name in ('bundle', 'ticket', 'features', 'service-public-key', 'runner-key', 'output-dir'):
        run.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(argv)
    service = None
    try:
        if args.command == 'keygen':
            key = load_private_key(args.private_key, create=True)
            _save(args.public_out, public_key_b64(key), text=True)
            response = {'public_key_file': str(args.public_out.resolve())}
        elif args.command == 'run':
            from .. import sandbox
            from .confirmation_runner import run_confirmation_bundle
            runner_key = load_private_key(args.runner_key)
            # 签名密钥文件是 runner 侧唯一的机密：候选代码在 worker 里跑于 OS 沙箱，
            # 执行窗口内把密钥文件对该进程封读（Windows 强制完整性 NO_READ_UP；
            # Linux bubblewrap 用 mount namespace 屏蔽，候选读到空内容）。
            # 无内核级禁读能力的平台封读为空操作，但那里的 worker 沙箱也不可用，会直接失败，
            # 因此不存在「以为隔离了、其实没有」的成功路径。
            with sandbox.sealed_reads([args.runner_key]):
                result = run_confirmation_bundle(
                    args.bundle, read_json(args.ticket), read_json(args.features),
                    args.service_public_key.read_text(encoding='utf-8').strip(),
                    runner_key, args.output_dir)
            response = {'status': result['runner_receipt']['payload']['status'],
                        'output_dir': str(args.output_dir.resolve())}
        else:
            service = HoldoutService(args.service_dir)
            if args.command == 'register':
                spec = read_json(args.project / 'experiment.json')
                from ..core import evaluator_for_metric
                result = service.register(
                    dataset_id=args.dataset_id, dataset_version=args.dataset_version,
                    evaluation_group=args.evaluation_group,
                    train_path=args.project / spec['train'], dev_path=args.project / spec['dev'],
                    holdout_path=args.holdout, evaluator_id=evaluator_for_metric(spec['metric']),
                    seeds=spec['seeds'], min_effect=spec['min_improvement'], runtime_id=args.runtime_id,
                    runner_public_key=args.runner_public_key.read_text(encoding='utf-8').strip())
                _save(args.public_out, service.public_key, text=True)
            elif args.command == 'begin':
                result = service.begin(read_json(args.bundle / 'bundle.json'))
            elif args.command == 'features':
                result = service.features(read_json(args.ticket))
            elif args.command == 'fail-interrupted':
                result = service.fail_interrupted(read_json(args.ticket))
            else:
                runner_result = read_json(args.runner_result)
                result = service.complete(read_json(args.ticket), runner_result['runner_receipt'],
                                          runner_result['predictions'])
            _save(args.out, result)
            response = {'artifact': str(args.out.resolve())}
            if isinstance(result, dict) and 'payload' in result:
                response['kind'] = result['payload']['kind']
                response['status'] = result['payload'].get('status', 'issued')
        print(json.dumps(response, ensure_ascii=False))
        return 0 if response.get('status') != 'failed' else 2
    except (ProtocolError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'error_type': type(error).__name__, 'error': str(error)}, ensure_ascii=False))
        return 2
    finally:
        if service is not None:
            service.close()


if __name__ == '__main__':
    raise SystemExit(main())
