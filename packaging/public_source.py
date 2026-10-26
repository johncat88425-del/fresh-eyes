"""Prepare a public source candidate without modifying original execution evidence."""
import hashlib
import html
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
from urllib.parse import unquote, urlsplit
import zipfile

from fresh_eyes.handoff import validate_links

FORMAT = 'fresh-eyes-public-source-v1'
HOME = re.compile(r'''/(?:Users|home)/[A-Za-z0-9._-]+/[^\s"'<>`)]*''')
MAC_TEMP = re.compile(r'''/(?:private/)?var/folders/[^\s"'<>`)]*''')
WINDOWS = re.compile(r'''[A-Za-z]:\\{1,2}Users\\{1,2}[^\\\s"'<>`]+\\{1,2}[^\s"'<>`)]*''')
TEXT = {'.py', '.md', '.json', '.js', '.mjs', '.html', '.css', '.srt', '.sh', '.diff', '.txt', '.toml', '.example', '.Dockerfile', '.yml', '.yaml'}
INTERNAL_DOCS = {'AGENTS.md', 'B_HANDOFF.md', 'DECISIONS.md', 'FEEDBACK.md', 'HANDOFF.md',
    'PROJECT2.md', 'PROJECT3.md', 'START_HERE.md', 'TEAM_TASKS.md', 'TEAM_WORK_PLAN.md', 'WIN_PLAN_1022.md',
    'api/SANDBOX_ACCESS.md', 'submission/EXTERNAL_FEEDBACK.md',
    'evidence/youtube-channel-creation-20261025.jpg', 'evidence/youtube-published-20261025.jpg',
    'evidence/youtube-english-subtitles-published-20261025.jpg'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def private_paths(text):
    matches = [m for pattern in (HOME, MAC_TEMP, WINDOWS) for m in pattern.finditer(text)]
    # This exact upstream package-description example is already in public pip logs.
    return [m for m in matches if not m.group().startswith('/home/default/projects/charset_normalizer/')]


def redact_docs(text):
    for match in sorted(private_paths(text), key=lambda m: m.start(), reverse=True):
        text = text[:match.start()] + '<local-path>' + text[match.end():]
    return text


def read_handoff(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() or sum(i.file_size for i in archive.infolist()) > 128 * 1024 * 1024:
            raise ValueError('Invalid or oversized handoff')
        manifest = json.loads(archive.read('fresh-eyes/handoff-manifest.json'))
        names = {'fresh-eyes/' + name for name in manifest['files']} | {'fresh-eyes/handoff-manifest.json'}
        if set(archive.namelist()) != names or len(archive.namelist()) != len(names):
            raise ValueError('Unlisted or duplicate handoff entries')
        files = {}
        for name, expected in manifest['files'].items():
            part = PurePosixPath(name)
            if (part.is_absolute() or '..' in part.parts or str(part) != name or
                    any(p in {'.git', '.fresh-eyes'} for p in part.parts) or part.name == '.env' or
                    part.suffix in {'.db', '.sqlite', '.sqlite3', '.whl', '.pyc'}):
                raise ValueError('Private state or escaping path in handoff')
            info = archive.getinfo('fresh-eyes/' + name)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Symlink in handoff')
            raw = archive.read(info)
            if sha(raw) != expected:
                raise ValueError('Handoff file hash mismatch: ' + name)
            files[name] = raw
    return files


def public_files(original):
    files, omitted, changed = {}, {}, {}
    for name, raw in original.items():
        if name in INTERNAL_DOCS or (name.endswith('.md') and re.match(r'api/(?:B[0-9_]|A_B_|A_HANDOFF|B_HANDOFF)', name)):
            omitted[name] = {'original_sha256': sha(raw), 'reason': 'Internal collaboration or account-support draft; retained privately.'}
            continue
        text = raw.decode('utf-8') if Path(name).suffix in TEXT or name in {'LICENSE', '.gitignore', 'scripts/fresh-eyes', 'scripts/docker-local'} else None
        if text is not None and private_paths(text):
            if name.endswith('.md'):
                raw = redact_docs(text).encode()
                changed[name] = 'Documentation local paths redacted; original is retained by the owner.'
            elif name.startswith(('reports/', 'evidence/')) and name.endswith('.json'):
                omitted[name] = {'original_sha256': sha(raw), 'reason': 'Local execution metadata contains host paths; original retained privately.'}
                continue
            else:
                raise ValueError('Unexpected local path in source or executable evidence: ' + name)
        files[name] = raw
    files['PUBLIC_EXPORT.md'] = (
        '# Public source candidate\n\n'
        'This exporter prepares files locally and does not publish them. Check any separate publication receipt for public availability. Original execution evidence remains unchanged in the owner workspace. '
        'Internal collaboration/account-support drafts and JSON support metadata containing host paths are omitted, not rewritten into a new execution record. '
        'Documentation redactions and redirected links are listed with original/exported hashes in public-source-manifest.json. '
        'Raw retained report JSON, run records, ledgers and patch bytes are unchanged. '
        'An omitted plan cannot be used to activate an old owner run; prepare a new local plan.\n\n'
        'The demo rebuild uses versioned public recordings, not private reports or a database. '
        'Normal API startup disables repository execution and paid models. This export is not an AI repair, '
        'formal evaluation, public URL or another-machine verification.\n'
    ).encode()

    def target_for(name, target):
        parsed = urlsplit(html.unescape(target))
        if parsed.scheme or parsed.netloc or not parsed.path:
            return target
        rel = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parsed.path)))
        if rel in omitted or '<local-path>' in target:
            return posixpath.relpath('PUBLIC_EXPORT.md', posixpath.dirname(name) or '.')
        return target

    for name, raw in list(files.items()):
        if not name.endswith(('.md', '.html')):
            continue
        text = raw.decode()
        if name.endswith('.md'):
            text = re.sub(r'\]\(([^)]+)\)', lambda m: '](' + target_for(name, m[1]) + ')', text)
        else:
            pattern = r'''(\b(?:href|src)\s*=\s*)(["'])(.*?)\2'''
            text = re.sub(pattern, lambda m: m[1] + m[2] + target_for(name, m[3]) + m[2], text)
        if text.encode() != raw:
            changed[name] = changed.get(name, '') + ' Links to withheld metadata point to PUBLIC_EXPORT.md.'
            files[name] = text.encode()
    files['README.md'] = (
        '> Public source candidate. See [export scope](PUBLIC_EXPORT.md). No API key is needed for tests or the read-only demo. '
        'From this directory: `python3.12 web/demo/build.py --replace`, then '
        '`python3.12 -m http.server 3001 --bind 127.0.0.1 --directory dist/demo`.\n\n'
    ).encode() + files['README.md']
    changed['README.md'] = changed.get('README.md', '') + ' Public export introduction added.'
    files['READ_ME_FIRST.txt'] = b'Read README.md and PUBLIC_EXPORT.md. This is a public source export, not the private owner handoff. Publication is a separate step.\n'
    changed['READ_ME_FIRST.txt'] = 'Public candidate instructions replace private teammate instructions.'
    if '.gitignore' in files:
        # Keep future local runs private while allowing only these reviewed report files into Git.
        ignore = [line for line in files['.gitignore'].decode().splitlines() if line.strip() not in {'reports/', '/reports/', 'reports', '/reports'}]
        reports = sorted(n for n in files if n.startswith('reports/'))
        directories = {str(parent) for name in reports for parent in PurePosixPath(name).parents if str(parent) not in {'.', 'reports'}}
        rules = ['/reports/*']
        for directory in sorted(directories, key=lambda n: (n.count('/'), n)):
            rules += ['!/' + directory + '/', '/' + directory + '/*']
        rules += ['!/' + name for name in reports]
        files['.gitignore'] = ('\n'.join(ignore + ['# Only the retained public report files below may be committed.'] + rules) + '\n').encode()
        changed['.gitignore'] = 'Allowlist retained public report files; new local report paths remain ignored.'
    for name, raw in files.items():
        if Path(name).suffix in TEXT and private_paths(raw.decode()):
            raise ValueError('Public export still contains host paths: ' + name)
    validate_links(files)
    for name, raw in files.items():
        if not name.endswith('.md'):
            continue
        for target in re.findall(r'\]\(([^)]+)\)', raw.decode()):
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            rel = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(url.path)))
            if rel not in files:
                raise ValueError('Broken public documentation link: ' + name + ' -> ' + target)
    manifest = {'format': FORMAT, 'published': False, 'new_provider_calls': 0, 'omitted': omitted,
        'transformed': {n: {'reason': reason, 'original_sha256': sha(original[n]), 'exported_sha256': sha(files[n])}
                        for n, reason in changed.items()},
        'files': {n: sha(raw) for n, raw in sorted(files.items())}}
    return files, manifest


def build(handoff, output):
    output = Path(output)
    if output.exists():
        raise ValueError('Preserve the previous export; select a new output file')
    files, manifest = public_files(read_handoff(handoff))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo('fresh-eyes/' + name)
            info.create_system = 3
            executable = name.startswith('scripts/') or name.endswith('.sh')
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
        archive.writestr('fresh-eyes/public-source-manifest.json', json.dumps(manifest, indent=2) + '\n')
    with zipfile.ZipFile(buffer) as archive:
        if archive.testzip():
            raise ValueError('Public ZIP CRC failed')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        stream.write(buffer.getvalue())
    return {'archive': str(output), 'sha256': sha(buffer.getvalue()), 'files': len(files) + 1,
            'omitted_metadata': len(manifest['omitted']), 'transformed_files': len(manifest['transformed']),
            'new_provider_calls': 0, 'published': False}
