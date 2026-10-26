"""README-only install/help checks, with no functional acceptance or model calls."""

from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import uuid

from .contracts import digest, validate_steps, InvalidCase
from .github_source import acquire, SourceError
from .runner import InfrastructureError


def documented_plan(source):
    root = Path(source)
    readmes = [p for p in root.iterdir() if p.name.lower() in {'readme.md', 'readme.rst', 'readme.txt', 'readme'} and p.is_file() and not p.is_symlink()]
    if len(readmes) != 1:
        return {'status': 'needs_input', 'reason': 'Choose one root README before checking commands.', 'setup': [], 'startup': None}
    readme = readmes[0]
    raw = readme.read_bytes()
    if len(raw) > 262144:
        return {'status': 'unsupported', 'reason': 'README exceeds 256 KiB.', 'setup': [], 'startup': None}
    text = raw.decode('utf-8', errors='replace')
    setups, startups, ignored = {}, {}, []
    fenced, fence, rst_block = False, None, False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        marker = re.match(r'^(`{3,}|~{3,})(.*)$', stripped)
        if marker:
            if not fenced:
                fenced, fence = True, marker[1][0]
            elif marker[1][0] == fence:
                fenced, fence = False, None
            continue
        if readme.suffix == '.rst':
            if stripped.startswith(('.. code-block::', '.. code::')) or stripped.endswith('::'):
                rst_block = True
                continue
            if stripped and not line.startswith((' ', '\t')):
                rst_block = False
        if not (fenced or (rst_block and line.startswith((' ', '\t')))):
            continue
        command = stripped[2:] if stripped.startswith('$ ') else stripped
        if not command or command.startswith('#'):
            continue
        if any(c in command for c in ('|', ';', '&', '`', '$', '\\')):
            if re.search(r'\b(?:pip|pip3|python|python3)\b', command):
                ignored.append(f'{readme.name}:L{number}')
            continue
        try:
            argv = shlex.split(command, comments=True)
        except ValueError:
            continue
        citation = f'{readme.name}:L{number}'
        if not argv:
            continue
        if argv[0] in {'pip', 'pip3'} or argv[:3] in (['python','-m','pip'], ['python3','-m','pip']):
            args = argv[1:] if argv[0] in {'pip','pip3'} else argv[3:]
            safe, target = True, False
            if not args or args[0] != 'install':
                continue
            i = 1
            while i < len(args):
                item = args[i]
                if item in {'-U','--upgrade'}:
                    i += 1
                    continue
                if item in {'-r','--requirement','-e','--editable'}:
                    i += 1
                    if i >= len(args) or (item in {'-r','--requirement'} and not (re.fullmatch(r'requirements[A-Za-z0-9_.-]*\.txt', args[i]) and (root/args[i]).is_file())) or (item in {'-e','--editable'} and args[i] != '.'):
                        safe = False
                        break
                elif item != '.' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~]=?[A-Za-z0-9.*+_-]+(?:,[<>=!~]=?[A-Za-z0-9.*+_-]+)*)?', item):
                    safe = False
                    break
                target = True
                i += 1
            if safe and target:
                try:
                    validate_steps([{'argv': argv, 'source': citation}])
                    setups.setdefault(tuple(argv), {'argv': argv, 'source': citation})
                except InvalidCase:
                    ignored.append(citation)
            else:
                ignored.append(citation)
        elif len(argv) == 2 and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', argv[0]) and argv[0] not in {'sh','bash','zsh','env','sudo','curl','wget','pip','pip3','python','python3','git'} and argv[1] in {'--help','-h','--version','-V'}:
            startups.setdefault(tuple(argv), {'argv': argv, 'source': citation})
        elif len(argv) == 4 and argv[0] in {'python','python3'} and argv[1] == '-m' and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', argv[2]) and argv[3] in {'--help','-h','--version','-V'}:
            startups.setdefault(tuple(argv), {'argv': argv, 'source': citation})
    common = {'readme': readme.name, 'readme_sha256': hashlib.sha256(raw).hexdigest(),
              'setup': list(setups.values()), 'startup': next(iter(startups.values())) if len(startups)==1 else None,
              'ignored_command_sources': ignored, 'executed': False, 'model_calls': 0,
              'acceptance_sha256': None, 'human_approval_granted': False,
              'scope': 'README installation and explicitly documented help/version command only'}
    python_project = any((root/name).is_file() for name in ('pyproject.toml','setup.py','setup.cfg','requirements.txt'))
    if not python_project:
        return {**common, 'status':'unsupported', 'reason':'No root Python project or requirements file is present.'}
    if ignored or len(setups) != 1 or len(startups) != 1:
        return {**common, 'status':'needs_input', 'reason':'A single unambiguous pip installation and help/version command must both be documented; no command was invented or executed.'}
    return {**common, 'status':'ready', 'reason':'The documented startup commands can be attempted inside isolation; no functional result is asserted.'}


def inspect_repo(url, ref, source, *, reader=None):
    receipt = acquire(url, ref, source, reader=reader)
    plan = documented_plan(source)
    return {'schema_version':1, 'source':receipt, 'plan':plan, 'plan_sha256':digest(plan),
            'executed':False, 'model_calls':0, 'data_origin':'github_readme_inspection'}


def save(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output/'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Fresh Eyes README startup check</title><h1>README startup check</h1><p>Installation and CLI startup only. No functional acceptance or model repair.</p><pre>'+html.escape(json.dumps(report,indent=2,ensure_ascii=False))+'</pre></html>'
    (output/'report.html').write_text(page)


def run_startup(inspection, source, output, *, runner, temp_root=None):
    """The only execution path is the existing non-root, PyPI-only Docker runner."""
    if Path(output).exists():
        raise SourceError('output_exists', 'Use a new startup report directory.')
    plan = inspection['plan']
    report = {'schema_version':1, 'run_id':uuid.uuid4().hex, 'data_origin':'github_startup_execution',
              'started_at':datetime.now(timezone.utc).isoformat(), 'finished_at':None,
              'verification_level':'startup', 'repo':inspection['source'], 'readme_plan':plan,
              'goal':'Install and start the CLI exactly as documented; no functional acceptance.',
              'acceptance_sha256':None, 'human_approval_granted':False, 'model_calls':0,
              'executed':False,
              'repair_author':None, 'attempts':[], 'status':plan['status'], 'reason':plan['reason'],
              'limitations':['startup_only never means functional correctness or a verified fix.',
                             'Only one explicit README pip installation and help/version command is supported.',
                             'Pip may install the published package rather than the pinned checkout when the README says so.']}
    try:
        if plan['status'] != 'ready':
            return report
        if runner.dependency_mode != 'pypi_live':
            raise InfrastructureError('Startup checks require the controlled live PyPI runner; offline packages are not assumed.')
        root = Path(source)
        expected = inspection['source']['source_files']
        actual = {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and not p.is_symlink()}
        if actual != expected or any(p.is_symlink() for p in root.rglob('*')) or digest(plan) != inspection['plan_sha256'] or documented_plan(root) != plan:
            raise SourceError('source_changed', 'The inspected source or derived plan changed; inspect it again.')
        report['environment'] = runner.preflight()
        with tempfile.TemporaryDirectory(prefix='fresh-eyes-startup-', dir=temp_root) as directory:
            bundle = Path(directory)/'bootstrap'
            bundle.mkdir()
            shutil.copytree(root, bundle/'repo')
            shutil.copyfile(Path(__file__).with_name('worker.py'), bundle/'worker.py')
            (bundle/'wheels').mkdir()
            payload = {'setup':plan['setup'], 'workflow':{'argv':plan['startup']['argv'], 'output':'stdout'},
                       'fixtures':[{'name':'startup-check.txt','input':''}]}
            (bundle/'payload.json').write_text(json.dumps(payload))
            result = runner.run(bundle)
        report['executed'] = bool(result.get('steps', [])[2:] or result.get('outputs'))
        report['attempts'].append({'phase':'original_readme','status':'failed','result':result})
        if result.get('infrastructure_error'):
            raise InfrastructureError(result['infrastructure_error'])
        outputs = result.get('outputs', [])
        passed = result.get('setup_ok') is True and len(outputs)==1 and outputs[0].get('exit_code')==0 and not outputs[0].get('truncated')
        report.update(status='startup_only' if passed else 'unresolved',
                      reason='The documented installation and CLI startup exited successfully; functional output has not been verified.' if passed else 'The documented installation or startup failed; see the saved commands and logs.')
        report['attempts'][0]['status'] = 'passed' if passed else 'failed'
    except SourceError as exc:
        report.update(status='needs_review', reason=f'{exc.code}: {exc}')
        raise
    except InfrastructureError as exc:
        report.update(status='infrastructure_error', reason=str(exc))
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save(report, output)
    return report
