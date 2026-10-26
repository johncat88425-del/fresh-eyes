// Read a README the way the backend's startup check does (src/fresh_eyes/startup.py, documented_plan), in the browser.
// Pure: no network, no execution. tests/test_readme_plan_parity.py runs both versions on the same READMEs.

const README_NAMES = new Set(['readme.md', 'readme.rst', 'readme.txt', 'readme']);
const PYTHON_FILES = ['pyproject.toml', 'setup.py', 'setup.cfg', 'requirements.txt'];
const SHELL_CHARS = ['|', ';', '&', '`', '$', '\\'];
const HELP = new Set(['--help', '-h', '--version', '-V']);
const NOT_A_TOOL = new Set(['sh', 'bash', 'zsh', 'env', 'sudo', 'curl', 'wget', 'pip', 'pip3', 'python', 'python3', 'git']);
const PACKAGE = /^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~]=?[A-Za-z0-9.*+_-]+(?:,[<>=!~]=?[A-Za-z0-9.*+_-]+)*)?$/;

// Python's str.splitlines(): every line break it knows, and no empty last line.
export function splitLines(text) {
  const lines = text.split(/\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/);
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

// shlex.split(command, comments=True) for the commands that reach it (no $, `, \ or ; | &): quotes, ' \t\r\n' as the
// only separators, and '#' outside quotes ending the line (even mid-word). Returns null where shlex raises.
export function shlexSplit(command) {
  const out = []; let word = null; let quote = null;
  for (const ch of command) {
    if (quote) {
      if (ch === quote) quote = null; else word += ch;
    } else if (ch === "'" || ch === '"') {
      quote = ch; word = word ?? '';
    } else if (ch === ' ' || ch === '\t' || ch === '\r' || ch === '\n') {
      if (word !== null) { out.push(word); word = null; }
    } else if (ch === '#') {
      break;
    } else {
      word = (word ?? '') + ch;
    }
  }
  if (quote) return null;
  if (word !== null) out.push(word);
  return out;
}

// Python's str.strip(): exactly the characters str.isspace() accepts (not U+FEFF, unlike JavaScript's \s).
const PY_SPACE = '[\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]';
const pythonStrip = s => s.replace(new RegExp(`^${PY_SPACE}+|${PY_SPACE}+$`, 'g'), '');

// rootFiles: names of the regular files at the repository root; text: the README decoded like bytes.decode('utf-8', errors='replace'); size: its bytes.
export function documentedPlan({ rootFiles, readmeName, text, size }) {
  const readmes = rootFiles.filter(name => README_NAMES.has(name.toLowerCase()));
  if (readmes.length !== 1 || (readmeName && readmes[0] !== readmeName)) {
    return { status: 'needs_input', reason: 'Choose one root README before checking commands.', setup: [], startup: null };
  }
  const readme = readmes[0];
  if (size > 262144) return { status: 'unsupported', reason: 'README exceeds 256 KiB.', setup: [], startup: null };
  const rst = /\.rst$/.test(readme);
  const setups = new Map(); const startups = new Map(); const ignored = [];
  let fenced = false, fence = null, rstBlock = false;
  splitLines(text).forEach((line, index) => {
    const number = index + 1;
    const stripped = pythonStrip(line);
    const marker = /^(`{3,}|~{3,})(.*)$/.exec(stripped);
    if (marker) {
      if (!fenced) { fenced = true; fence = marker[1][0]; }
      else if (marker[1][0] === fence) { fenced = false; fence = null; }
      return;
    }
    if (rst) {
      if (stripped.startsWith('.. code-block::') || stripped.startsWith('.. code::') || stripped.endsWith('::')) { rstBlock = true; return; }
      if (stripped && !line.startsWith(' ') && !line.startsWith('\t')) rstBlock = false;
    }
    if (!(fenced || (rstBlock && (line.startsWith(' ') || line.startsWith('\t'))))) return;
    const command = stripped.startsWith('$ ') ? stripped.slice(2) : stripped;
    if (!command || command.startsWith('#')) return;
    const citation = `${readme}:L${number}`;
    if (SHELL_CHARS.some(c => command.includes(c))) {
      if (/\b(?:pip|pip3|python|python3)\b/.test(command)) ignored.push(citation);
      return;
    }
    const argv = shlexSplit(command);
    if (!argv || !argv.length) return;
    const isPip = argv[0] === 'pip' || argv[0] === 'pip3';
    const isPythonPip = (argv[0] === 'python' || argv[0] === 'python3') && argv[1] === '-m' && argv[2] === 'pip' && argv.length >= 3;
    if (isPip || isPythonPip) {
      const args = isPip ? argv.slice(1) : argv.slice(3);
      if (!args.length || args[0] !== 'install') return;
      let safe = true, target = false;
      for (let i = 1; i < args.length; i++) {
        const item = args[i];
        if (item === '-U' || item === '--upgrade') continue;
        if (['-r', '--requirement', '-e', '--editable'].includes(item)) {
          i += 1;
          const value = args[i];
          const requirement = item === '-r' || item === '--requirement';
          if (value === undefined || (requirement && !(/^requirements[A-Za-z0-9_.-]*\.txt$/.test(value) && rootFiles.includes(value)))
              || (!requirement && value !== '.')) { safe = false; break; }
        } else if (item !== '.' && !PACKAGE.test(item)) { safe = false; break; }
        target = true;
      }
      const key = JSON.stringify(argv);
      if (safe && target) { if (!setups.has(key)) setups.set(key, { argv, source: citation }); }
      else ignored.push(citation);
    } else if (argv.length === 2 && /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(argv[0]) && !NOT_A_TOOL.has(argv[0]) && HELP.has(argv[1])) {
      const key = JSON.stringify(argv); if (!startups.has(key)) startups.set(key, { argv, source: citation });
    } else if (argv.length === 4 && (argv[0] === 'python' || argv[0] === 'python3') && argv[1] === '-m'
               && /^[A-Za-z_][A-Za-z0-9_.]*$/.test(argv[2]) && HELP.has(argv[3])) {
      const key = JSON.stringify(argv); if (!startups.has(key)) startups.set(key, { argv, source: citation });
    }
  });
  const common = { readme, setup: [...setups.values()], startup: startups.size === 1 ? [...startups.values()][0] : null,
                   ignored_command_sources: ignored, executed: false, model_calls: 0,
                   // Browser-only addition (the backend keeps just one): every help/version command found, so the page
                   // can say whether there were none or too many.
                   startup_candidates: [...startups.values()] };
  if (!PYTHON_FILES.some(name => rootFiles.includes(name))) {
    return { ...common, status: 'unsupported', reason: 'No root Python project or requirements file is present.' };
  }
  if (ignored.length || setups.size !== 1 || startups.size !== 1) {
    return { ...common, status: 'needs_input', reason: 'A single unambiguous pip installation and help/version command must both be documented; no command was invented or executed.' };
  }
  return { ...common, status: 'ready', reason: 'The documented startup commands can be attempted inside isolation; no functional result is asserted.' };
}
