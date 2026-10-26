"""Export the validated read-only demo as one portable HTML file.

No global fetch replacement, eval, service worker, API credentials or network
fallback. Each module uses its own immutable embedded recording reader.
"""
import base64
import hashlib
import json
from pathlib import Path


def module_url(source):
    return 'data:text/javascript;base64,' + base64.b64encode(source.encode()).decode()


def build_single(source, output):
    source, output = Path(source), Path(output)
    if output.exists():
        raise ValueError('Preserve existing single-file releases; choose a new output')
    manifest = json.loads((source / 'data/manifest.json').read_text())
    # The normal builder has already scrubbed recordings. Verify its complete
    # byte inventory again before putting the recordings in public HTML.
    hashes = manifest['files']
    for name, checksum in hashes.items():
        path = source / name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink():
            raise ValueError('Unsafe demo manifest path')
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError('Demo file checksum changed: ' + name)
    assets = {}
    for name in hashes:
        if name.endswith(('.json', '.diff')):
            assets[name] = (source / name).read_text()
    reader = '''let recordings;
export async function recordedFetch(path) {
  recordings ??= JSON.parse(document.getElementById('fresh-eyes-recordings').textContent);
  const name = String(path);
  return Object.hasOwn(recordings, name)
    ? new Response(recordings[name], {status: 200})
    : new Response('Recording not found', {status: 404});
}
'''
    prelude = "import { recordedFetch as fetch } from 'fresh-eyes/recordings';\n"
    client = (source / 'client.js').read_text()
    app = (source / 'app.js').read_text()
    if client.count('await fetch(') != 1 or app.count('await fetch(') != 2:
        raise ValueError('Fetch structure changed; review single-file export')
    modules = {'fresh-eyes/recordings': module_url(reader),
        'fresh-eyes/client': module_url(prelude + client),
        'fresh-eyes/evaluation': module_url((source / 'evaluation.js').read_text()),
        'fresh-eyes/i18n': module_url((source / 'i18n.js').read_text()),
        'fresh-eyes/repair_path': module_url((source / 'repair_path.js').read_text().replace("'./evaluation.js'", "'fresh-eyes/evaluation'")),
        # "Try your own project" reads GitHub with the browser's real fetch, never the recordings.
        'fresh-eyes/readme_plan': module_url((source / 'readme_plan.js').read_text()),
        'fresh-eyes/github_readme': module_url((source / 'github_readme.js').read_text().replace("'./readme_plan.js'", "'fresh-eyes/readme_plan'"))}
    for name in ('client', 'evaluation', 'i18n', 'repair_path', 'github_readme'):
        old = "'./" + name + ".js'"
        if app.count(old) != 1:
            raise ValueError('Import structure changed; review single-file export')
        app = app.replace(old, "'fresh-eyes/" + name + "'")
    html = (source / 'index.html').read_text()
    marker = '<script type="module" src="app.js"></script>'
    if html.count(marker) != 1:
        raise ValueError('HTML module entry changed; review single-file export')
    safe_json = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    embedded = '<script type="application/json" id="fresh-eyes-recordings">' + safe_json(assets) + '</script>\n'
    embedded += '<script type="importmap">' + safe_json({'imports': modules}) + '</script>\n'
    embedded += '<script type="module" src="' + module_url(prelude + app) + '"></script>'
    html = html.replace(marker, embedded)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as f:
        f.write(html)
    return {'output': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'bytes': output.stat().st_size, 'embedded_recordings': len(assets),
        'network_api_calls_on_load': 0, 'optional_browser_github_reads': True,
        'notice': 'Recorded replay plus opt-in GitHub README planning; no repository execution or inference.'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('dist/demo'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_single(args.source, args.output), indent=2))
