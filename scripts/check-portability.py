#!/usr/bin/env python3
"""Verify a handoff in a fresh Linux container with no network, keys or host mounts."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]


def save_summary(output, summary):
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as stream:
        stream.write(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({'output': str(output), 'passed': summary['result'].get('passed', False),
                      'container_removed': summary.get('container_removed'), 'new_provider_calls': 0}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=ROOT / 'dist/fresh-eyes-teammate.zip')
    parser.add_argument('--docker', default='docker')
    parser.add_argument('--image', required=True, help='Immutable ID of a local portability toolchain image')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', args.image):
        parser.error('Use a full immutable image ID')
    if args.output.exists():
        parser.error('Preserve prior evidence; select a new output file')
    archive = args.archive.read_bytes()
    summary = {'scope': 'portable_core_api_frontend', 'planned_image': args.image,
               'archive_sha256': hashlib.sha256(archive).hexdigest(), 'network': 'none', 'host_mounts': [],
               'key_read': False, 'new_provider_calls': 0, 'executed': False}
    if not args.execute:
        print(json.dumps(summary, indent=2))
        return
    summary['started_at'] = datetime.now(timezone.utc).isoformat()
    try:
        image = json.loads(subprocess.check_output([args.docker, 'image', 'inspect', args.image], text=True, stderr=subprocess.PIPE, timeout=15))[0]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        summary.update(phase='preflight', container_created=False, container_removed=None,
                       finished_at=datetime.now(timezone.utc).isoformat(),
                       result={'passed': False, 'error': str(exc)})
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            summary['stderr'] = str(exc.stderr)[-2000:]
        save_summary(args.output, summary)
        raise SystemExit(1) from None
    name = 'fresh-eyes-portability-' + uuid.uuid4().hex[:12]
    command = [args.docker, 'run', '--rm', '--name', name, '--network', 'none', '--read-only',
        '--tmpfs', '/work:rw,exec,nosuid,nodev,mode=1777,size=536870912', '--tmpfs', '/tmp:rw,exec,nosuid,nodev,mode=1777,size=67108864',
        '--user', '65532:65532', '--pids-limit', '256', '--cpus', '2', '--memory', '1g', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--workdir', '/work', '-i', args.image,
        'python3.12', '-B', '-W', 'error::ResourceWarning', '-c', (ROOT / 'packaging/check_inside.py').read_text()]
    summary.update(executed=True, phase='container_check', image_id=image['Id'], image_os=image['Os'], image_architecture=image['Architecture'])
    try:
        run = subprocess.run(command, input=archive, capture_output=True, timeout=600)
        summary['exit_code'] = run.returncode
        try:
            summary['result'] = json.loads(run.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            summary['result'] = {'passed': False, 'error': 'Container returned no valid verification result'}
        if run.stderr:
            summary['stderr'] = run.stderr.decode(errors='replace')[-2000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        summary['result'] = {'passed': False, 'error': str(exc)}
    finally:
        summary['container_removed'] = False
        try:
            subprocess.run([args.docker, 'rm', '-f', name], capture_output=True, timeout=20)
            check = subprocess.run([args.docker, 'ps', '-aq', '--filter', 'name=^/' + name + '$'], capture_output=True, text=True, timeout=15)
            summary['container_removed'] = check.returncode == 0 and not check.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            summary['cleanup_error'] = str(exc)
        summary['finished_at'] = datetime.now(timezone.utc).isoformat()
        save_summary(args.output, summary)
    if not summary['result'].get('passed') or not summary['container_removed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
