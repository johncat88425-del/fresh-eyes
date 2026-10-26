import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readRepoReadme } from '../github_readme.js';

const SHA = 'a'.repeat(40);
const README = '# Tool\n```sh\npip install .\ntool --help\n```\n';

// A stand-in for GitHub's anonymous API and raw files; records every URL asked for.
function fakeGitHub({ files = ['README.md', 'pyproject.toml'], readme = README, status = {} } = {}) {
  const asked = [];
  const reply = (body, code = 200) => ({ status: code, ok: code < 300, json: async () => body,
    arrayBuffer: async () => new TextEncoder().encode(body).buffer });
  const fetchImpl = async url => {
    asked.push(url);
    for (const [part, code] of Object.entries(status)) if (url.includes(part)) return reply({}, code);
    if (url === 'https://api.github.com/repos/owner/tool') return reply({ default_branch: 'main', private: false });
    if (url.endsWith('/commits/main')) return reply({ sha: SHA });
    if (url.endsWith(`/contents?ref=${SHA}`)) return reply([...files.map(name => ({ name, type: 'file' })), { name: 'src', type: 'dir' }]);
    if (url.startsWith('https://raw.githubusercontent.com/owner/tool/')) return reply(readme);
    return reply({}, 404);
  };
  return { fetchImpl, asked };
}

test('a public repository is read at one pinned commit and planned without running anything', async () => {
  const gh = fakeGitHub();
  const r = await readRepoReadme('https://github.com/owner/tool/', gh.fetchImpl);
  assert.equal(r.sha, SHA);
  assert.equal(r.plan.status, 'ready');
  assert.deepEqual(r.plan.setup.map(s => s.source), ['README.md:L3']);
  assert.equal(r.plan.startup.source, 'README.md:L4');
  assert.equal(r.plan.executed, false);
  assert.equal(r.lines[2], 'pip install .');
  assert.ok(gh.asked.includes(`https://raw.githubusercontent.com/owner/tool/${SHA}/README.md`));
  assert.equal(gh.asked.length, 4);
});

test('the reasons a person is needed come from the README itself', async () => {
  const two = await readRepoReadme('https://github.com/owner/tool', fakeGitHub({ readme: '```\npip install .\npip install tool\n```\n' }).fetchImpl);
  assert.equal(two.plan.status, 'needs_input');
  assert.equal(two.plan.setup.length, 2);
  assert.equal(two.plan.startup_candidates.length, 0);
  const none = await readRepoReadme('https://github.com/owner/tool', fakeGitHub({ files: ['README.md', 'README.rst', 'setup.py'] }).fetchImpl);
  assert.equal(none.readme, null);
  assert.equal(none.plan.status, 'needs_input');
  const notPython = await readRepoReadme('https://github.com/owner/tool', fakeGitHub({ files: ['README.md', 'package.json'] }).fetchImpl);
  assert.equal(notPython.plan.status, 'unsupported');
});

test('bad links, missing projects and GitHub limits become plain error codes', async () => {
  for (const [url, options, code] of [
    ['https://gitlab.com/owner/tool', {}, 'bad_url'],
    ['https://github.com/owner', {}, 'bad_url'],
    ['https://github.com/owner/tool', { status: { 'repos/owner/tool': 404 } }, 'not_found'],
    ['https://github.com/owner/tool', { status: { '/commits/': 403 } }, 'rate_limited'],
    ['https://github.com/owner/tool', { status: { '/contents': 500 } }, 'github_error'],
  ]) {
    await assert.rejects(readRepoReadme(url, fakeGitHub(options).fetchImpl), e => e.code === code, url);
  }
  await assert.rejects(readRepoReadme('https://github.com/owner/tool', async () => { throw new TypeError('offline'); }), e => e.code === 'network');
});

test('known oversized README metadata stops before downloading raw contents', async () => {
  const gh = fakeGitHub();
  const r = await readRepoReadme('https://github.com/owner/tool', async (url, options) => {
    const response = await gh.fetchImpl(url, options);
    if (url.includes('/contents?')) {
      const files = await response.json();
      return { ...response, json: async () => files.map(f => ({ ...f, size: f.name === 'README.md' ? 262145 : 0 })) };
    }
    return response;
  });
  assert.equal(r.plan.status, 'unsupported');
  assert.equal(gh.asked.length, 3);
  assert.equal(gh.asked.some(url => url.startsWith('https://raw.')), false);
});

test('an underestimated streaming README is cancelled at the limit without credentials', async () => {
  const gh = fakeGitHub(); let cancelled = false, released = false;
  await assert.rejects(readRepoReadme('https://github.com/owner/tool', async (url, options) => {
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
    assert.ok(options.signal instanceof AbortSignal);
    if (!url.startsWith('https://raw.')) return gh.fetchImpl(url, options);
    return { ok: true, status: 200, body: { getReader: () => ({
      read: async () => ({ done: false, value: new Uint8Array(262145) }),
      cancel: async () => { cancelled = true; }, releaseLock: () => { released = true; },
    }) } };
  }), error => error.code === 'too_large');
  assert.equal(cancelled, true); assert.equal(released, true);
});

test('traversal or credential-bearing URLs never dispatch a GitHub request', async () => {
  for (const url of ['https://github.com/../tool', 'https://github.com/owner/..', 'https://user:secret@github.com/owner/tool']) {
    let calls = 0;
    await assert.rejects(readRepoReadme(url, async () => { calls++; }), error => error.code === 'bad_url');
    assert.equal(calls, 0);
  }
});
