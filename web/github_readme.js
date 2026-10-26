// "Try your own project": read a public repository's root README from GitHub, in the visitor's browser, and plan it
// with the backend's rules (readme_plan.js). Three anonymous API calls and one raw file; nothing is run.
import { documentedPlan, splitLines } from './readme_plan.js';

const API = 'https://api.github.com/repos/';
const RAW = 'https://raw.githubusercontent.com/';
export const REPO_URL = /^https:\/\/github\.com\/([A-Za-z0-9_-][A-Za-z0-9_.-]*)\/([A-Za-z0-9_-][A-Za-z0-9_.-]*?)(?:\.git)?\/?$/;
const README_NAMES = new Set(['readme.md', 'readme.rst', 'readme.txt', 'readme']);
const MAX_README = 262144;

export class LookupError extends Error {
  constructor(code) { super(code); this.code = code; }
}

export async function readRepoReadme(url, fetchImpl = fetch) {
  const match = REPO_URL.exec(String(url).trim());
  if (!match) throw new LookupError('bad_url');
  const [, owner, repo] = match;
  const get = async (target, kind = 'json') => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetchImpl(target, { signal: controller.signal, credentials: 'omit', redirect: 'error',
        referrerPolicy: 'no-referrer', ...(kind === 'json' ? { headers: { Accept: 'application/vnd.github+json' } } : {}) });
      if (response.status === 404) throw new LookupError('not_found');
      if (response.status === 403 || response.status === 429) throw new LookupError('rate_limited');
      if (!response.ok) throw new LookupError('github_error');
      if (kind === 'json') return await response.json();
      if (Number(response.headers?.get('content-length')) > MAX_README) throw new LookupError('too_large');
      if (!response.body?.getReader) {
        const raw = await response.arrayBuffer();
        if (raw.byteLength > MAX_README) throw new LookupError('too_large');
        return raw;
      }
      const reader = response.body.getReader(), chunks = [];
      let length = 0;
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          length += value.byteLength;
          if (length > MAX_README) { await reader.cancel(); throw new LookupError('too_large'); }
          chunks.push(value);
        }
      } finally { reader.releaseLock(); }
      const bytes = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      return bytes.buffer;
    } catch (error) {
      if (error instanceof LookupError) throw error;
      throw new LookupError(controller.signal.aborted ? 'timeout' : 'network');
    } finally { clearTimeout(timer); controller.abort(); }
  };
  const info = await get(`${API}${owner}/${repo}`);
  if (info.private || typeof info.default_branch !== 'string') throw new LookupError('not_found');
  const sha = (await get(`${API}${owner}/${repo}/commits/${encodeURIComponent(info.default_branch)}`)).sha;
  if (!/^[a-f0-9]{40}$/.test(sha || '')) throw new LookupError('github_error');
  const listing = await get(`${API}${owner}/${repo}/contents?ref=${sha}`);
  if (!Array.isArray(listing)) throw new LookupError('github_error');
  const rootFiles = listing.filter(item => item.type === 'file').map(item => item.name);
  const readmes = rootFiles.filter(name => README_NAMES.has(name.toLowerCase()));
  const base = { owner, repo, sha, branch: info.default_branch, rootFiles };
  if (readmes.length !== 1) return { ...base, readme: null, lines: [], plan: documentedPlan({ rootFiles, text: '', size: 0 }) };
  const size = listing.find(item => item.type === 'file' && item.name === readmes[0])?.size;
  if (Number.isFinite(size) && size > MAX_README) {
    return { ...base, readme: readmes[0], lines: [], plan: documentedPlan({ rootFiles, text: '', size }) };
  }
  const raw = await get(`${RAW}${owner}/${repo}/${sha}/${encodeURIComponent(readmes[0])}`, 'raw');
  const text = new TextDecoder('utf-8').decode(raw);  // replaces bad bytes, like bytes.decode(errors='replace')
  return { ...base, readme: readmes[0], lines: splitLines(text),
           plan: documentedPlan({ rootFiles, readmeName: readmes[0], text, size: raw.byteLength }) };
}
