"""Bounded, credential-free GitHub source acquisition. Never imports repo code."""

from datetime import datetime, timezone
import hashlib
import gzip
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .sandboxes import NoRedirect

MAX_ARCHIVE = 8 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_FILES = 2000


class SourceError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def github_repo(url):
    if not isinstance(url, str):
        raise SourceError("invalid_repo_url", "Use an HTTPS GitHub owner/repository URL.")
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9_-]{0,38})/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/?", url)
    if not match or match[2].endswith('.git'):
        raise SourceError("invalid_repo_url", "Use https://github.com/owner/repository without credentials, .git, query or fragment.")
    return match[1], match[2]


def commit_ref(ref):
    if ref is not None and (not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{40}", ref)):
        raise SourceError("invalid_ref", "Use a full lowercase 40-character commit SHA, or omit ref for the default branch.")
    return ref


class GitHubReader:
    """Fixed HTTPS origins, no key/environment proxies, no redirect or retry."""
    def __init__(self, timeout=20, total_timeout=60):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.timeout = min(20, timeout)
        self.deadline = time.monotonic() + min(60, total_timeout)

    def get(self, url, limit):
        if not re.fullmatch(r"https://(?:api\.github\.com/repos/[^?#]+|codeload\.github\.com/[^?#]+)", url):
            raise SourceError("source_origin", "Only fixed public GitHub source endpoints are allowed.")
        req = Request(url, headers={"User-Agent": "Fresh-Eyes-public-source-check", "Accept": "application/vnd.github+json"})
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise SourceError("source_timeout", "The bounded source download expired.")
        try:
            with self.opener.open(req, timeout=min(self.timeout, remaining)) as response:
                length = response.headers.get('Content-Length')
                if length and (not length.isdigit() or int(length) > limit):
                    raise SourceError("source_too_large", "Source response exceeds its byte limit.")
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= self.deadline:
                        raise SourceError("source_timeout", "The bounded source download expired.")
                    block = response.read1(min(65536, limit+1-size))
                    if not block:
                        break
                    chunks.append(block)
                    size += len(block)
                    if size > limit:
                        raise SourceError("source_too_large", "Source response exceeds its byte limit.")
                return b''.join(chunks)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise SourceError("source_http_error", f"Public GitHub returned HTTP {status}; private repositories, redirects and authentication are not supported.") from None
        except (OSError, URLError, TimeoutError):
            raise SourceError("source_unavailable", "Public GitHub source could not be downloaded; no retry was made.") from None


def archive_files(raw):
    """Validate the whole archive before writing a single member to disk."""
    if len(raw) > MAX_ARCHIVE:
        raise SourceError("source_too_large", "Compressed repository exceeds 8 MiB.")
    files, kinds, root, expanded = {}, {}, None, 0
    try:
        # Bound the entire decoded tar, including PAX/long-name headers, before
        # tarfile parses them. A file-size check alone misses metadata bombs.
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
            decoded = compressed.read(MAX_EXPANDED+4*1024*1024+1)
        if len(decoded) > MAX_EXPANDED+4*1024*1024:
            raise SourceError('source_too_large', 'The complete decoded source archive exceeds 36 MiB.')
        with tarfile.open(fileobj=io.BytesIO(decoded), mode='r|') as archive:
            for index, member in enumerate(archive):
                if index >= MAX_FILES:
                    raise SourceError("source_too_large", "Repository exceeds 2,000 archive members.")
                path = PurePosixPath(member.name)
                parts = path.parts
                if (not parts or path.is_absolute() or '..' in parts or '\\' in member.name or '\x00' in member.name or
                    str(path) != member.name.rstrip('/') or not (member.isdir() or member.isfile())):
                    raise SourceError("unsafe_source", "Source contains a link, special file or unsafe path.")
                root = root or parts[0]
                if parts[0] != root:
                    raise SourceError("unsafe_source", "Source must have one archive root.")
                if len(parts) == 1:
                    if not member.isdir():
                        raise SourceError("unsafe_source", "Archive root must be a directory.")
                    continue
                name = PurePosixPath(*parts[1:]).as_posix()
                if name in kinds or any(p in {'.git', '.hg', '.svn'} for p in parts[1:]):
                    raise SourceError("unsafe_source", "Duplicate paths or embedded VCS metadata are unsupported.")
                kinds[name] = 'directory' if member.isdir() else 'file'
                if member.isfile():
                    expanded += member.size
                    if member.size > MAX_FILE or expanded > MAX_EXPANDED:
                        raise SourceError("source_too_large", "Source exceeds the 2 MiB file or 32 MiB expanded limit.")
                    stream = archive.extractfile(member)
                    with stream:
                        content = stream.read(member.size+1)
                    if len(content) != member.size:
                        raise SourceError("unsafe_source", "Source member is truncated.")
                    files[name] = content
    except (tarfile.TarError, EOFError, OSError):
        raise SourceError("unsafe_source", "Source is not a complete supported gzip tar archive.") from None
    for name in kinds:
        if any(kinds.get(p.as_posix()) == 'file' for p in PurePosixPath(name).parents if str(p) != '.'):
            raise SourceError("unsafe_source", "Source places a member underneath a regular file.")
    if not files:
        raise SourceError("unsupported_source", "Repository has no supported regular files.")
    return files


def acquire(url, ref, destination, *, reader=None):
    owner, repo = github_repo(url)
    commit_ref(ref)
    destination = Path(destination)
    if destination.exists():
        raise SourceError("source_exists", "Use a new source directory; an older acquisition is never overwritten.")
    reader = reader or GitHubReader()
    raw_meta = reader.get(f'https://api.github.com/repos/{owner}/{repo}/commits/{ref or "HEAD"}', 131072)
    try:
        metadata = json.loads(raw_meta)
        sha = metadata['sha']
        if not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{40}', sha) or (ref and sha != ref):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise SourceError("source_identity", "GitHub did not resolve the requested immutable commit.") from None
    archive_url = f'https://codeload.github.com/{owner}/{repo}/tar.gz/{sha}'
    raw = reader.get(archive_url, MAX_ARCHIVE)
    files = archive_files(raw)
    destination.mkdir(parents=True)
    for name, content in files.items():
        target = destination/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o644)
    return {'schema_version': 1, 'repo_url': f'https://github.com/{owner}/{repo}', 'commit_sha': sha,
            'requested_ref': ref, 'resolved_at': datetime.now(timezone.utc).isoformat(),
            'archive_url': archive_url, 'archive_sha256': hashlib.sha256(raw).hexdigest(),
            'source_files': {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())},
            'file_count': len(files), 'download_bytes': len(raw), 'expanded_bytes': sum(map(len, files.values())),
            'executed': False, 'model_calls': 0, 'credentials_used': False,
            'limitations': ['Public GitHub only; default branch resolved once to a fixed commit.',
                            'Symlinks, submodules, embedded VCS metadata and large repositories are unsupported.']}
