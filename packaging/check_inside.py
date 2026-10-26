"""Executed only inside the disposable portability container; source ZIP is stdin."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import sys
import time
import unittest
from urllib.request import urlopen
import zipfile


def main():
    root = Path('/work/source')
    root.mkdir()
    Path('/work/tmp').mkdir()
    os.environ['TMPDIR'] = '/work/tmp'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    import tempfile
    tempfile.tempdir = '/work/tmp'
    raw = sys.stdin.buffer.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError('Archive exceeds 20 MiB')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.testzip() or sum(i.file_size for i in archive.infolist()) > 128 * 1024 * 1024:
            raise ValueError('Invalid or oversized archive')
        manifest = json.loads(archive.read('fresh-eyes/handoff-manifest.json'))
        for name, expected in manifest['files'].items():
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or str(path) != name:
                raise ValueError('Archive path escapes source')
            body = archive.read('fresh-eyes/' + name)
            if hashlib.sha256(body).hexdigest() != expected:
                raise ValueError('Archive manifest mismatch')
            if path.name == '.env' or path.suffix in {'.sqlite3', '.db', '.whl'}:
                raise ValueError('Unexpected private state')
            if path.parts[0] == 'reports':
                continue
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            target.chmod((archive.getinfo('fresh-eyes/' + name).external_attr >> 16) & 0o777)
    os.chdir(root)
    os.environ['PYTHONPATH'] = str(root / 'src')
    sys.path.insert(0, str(root / 'src'))
    build = subprocess.run([sys.executable, '-B', 'web/demo/build.py'], capture_output=True, text=True, timeout=45)
    if build.returncode:
        raise RuntimeError('Demo rebuild failed: ' + build.stderr[-3000:])
    build_result = json.loads(build.stdout)
    captured = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        tests = unittest.TextTestRunner(stream=captured, verbosity=0).run(suite)
    if not tests.wasSuccessful():
        raise RuntimeError('Python verification failed: ' + captured.getvalue()[-14000:])
    web = subprocess.run(['node', '--test', *[str(p) for p in sorted((root / 'web/tests').glob('*.test.mjs'))]],
                         capture_output=True, text=True, timeout=45)
    if web.returncode:
        raise RuntimeError('Web verification failed: ' + web.stdout[-6000:] + web.stderr[-2000:])
    processes = []
    try:
        for argv in ([sys.executable, '-B', 'scripts/fresh-eyes', 'serve', '--port', '8872', '--data-dir', '/work/api-state'],
                     [sys.executable, '-B', 'web/serve.py', '5173']):
            processes.append(subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        deadline = time.monotonic() + 8
        while True:
            try:
                with urlopen('http://127.0.0.1:8872/api/health', timeout=.5) as response:
                    health = json.load(response)
                with urlopen('http://127.0.0.1:5173/', timeout=.5) as response:
                    frontend = response.read().decode()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Default API/frontend failed to start')
                time.sleep(.05)
        with urlopen('http://127.0.0.1:8872/api/cases', timeout=2) as response:
            cases = json.load(response)['items']
        if health['execution_enabled'] or health['paid_calls_enabled'] or len(cases) != 3 or 'Fresh Eyes' not in frontend:
            raise RuntimeError('Unexpected default capabilities or frontend/case response')
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
    return {'data_origin': 'engineering_test', 'scope': 'portable_core_api_frontend',
        'platform': {'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version(),
                     'node': subprocess.check_output(['node', '--version'], text=True).strip(),
                     'git': subprocess.check_output(['git', '--version'], text=True).strip()},
        'source_archive_sha256': hashlib.sha256(raw).hexdigest(), 'source_manifest_files': len(manifest['files']),
        'python': {'passed': tests.testsRun, 'failures': len(tests.failures), 'errors': len(tests.errors), 'skipped': len(tests.skipped)},
        'web': {'passed': True, 'output': web.stdout}, 'demo': build_result,
        'api': {'started': True, 'execution_enabled': health['execution_enabled'], 'paid_calls_enabled': health['paid_calls_enabled'],
                'registered_cases': len(cases)}, 'frontend_http': 'passed',
        'owner_reports_present': False, 'owner_database_present': False, 'credentials_present': False,
        'new_provider_calls': 0, 'third_party_repositories_executed': 0,
        'limitations': 'Same physical Mac, separate Linux container. No real repository runner or model call in this check.'}


if __name__ == '__main__':
    try:
        result = main()
    except Exception as exc:
        print(json.dumps({'passed': False, 'error': str(exc)}))
        raise SystemExit(1)
    print(json.dumps({'passed': True, **result}, ensure_ascii=False))
