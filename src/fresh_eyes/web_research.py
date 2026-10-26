"""Opt-in Tavily web research for diagnosis (WIN-11): one cited lookup of how the pinned project is installed.

Off unless a key is configured and research is explicitly enabled in the control process. At most one search per
repair round and a fixed number per run; results are limited to an allowlist of documentation domains, truncated,
redacted and handed to the model as untrusted evidence. Every search is recorded (query, result URLs, credits, time).
"""
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .diagnostics import excerpt
from .models import ModelError, now
from .sandboxes import NoRedirect

ENDPOINT = "https://api.tavily.com/search"
MAX_BODY = 512 * 1024
DOC_DOMAINS = ("pypi.org", "github.com", "readthedocs.io", "readthedocs.org", "python.org")


@dataclass
class TavilyTransport:
    api_key: str = field(repr=False)
    enabled: bool = False
    data_origin: str = field(default="tavily_api", init=False)
    spend_guard: object = field(default=None, repr=False)

    def __post_init__(self):
        from .spending import SpendGuard
        self.spend_guard = self.spend_guard or SpendGuard()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def search(self, payload, *, timeout=20):
        self.preflight()
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ModelError("invalid_timeout", "Research timeout must be positive and at most 30 seconds.")
        if (payload.get('search_depth')!='basic' or payload.get('auto_parameters') is not False or
            payload.get('include_answer') is not False or payload.get('include_raw_content') is not False):
            raise ModelError('research_credit_policy','Only explicit basic searches without automatic upgrades or generated answers are allowed.')
        from .spending import SpendError
        try:
            reservation=self.spend_guard.reserve('tavily','1','basic-search')
            self.spend_guard.verify_reservation(reservation,'tavily')
        except SpendError as exc:
            raise ModelError(exc.code,str(exc)) from None
        actual=None
        request = Request(ENDPOINT, data=json.dumps(payload).encode(), method="POST", headers={
            "Authorization": "Bearer " + self.api_key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    raw = response.read(MAX_BODY + 1)
            except HTTPError as exc:
                exc.close()
                raise ModelError("research_request_error", "Web research HTTP request failed; further credit use is locked pending reconciliation.") from None
            except (OSError, URLError, TimeoutError):
                raise ModelError("research_request_error", "Web research request did not complete; no retry was made.") from None
            if len(raw) > MAX_BODY:
                raise ModelError("invalid_response", "Web research response exceeds 512 KiB.")
            try:
                result=json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ModelError("invalid_response", "Web research response is not valid JSON.") from None
            usage=result.get('usage',{}) if isinstance(result,dict) else {}
            credits=usage.get('credits') if isinstance(usage,dict) else None
            if type(credits) is int and credits>=0:
                actual=str(credits)
            if actual is None or int(actual)>1:
                raise ModelError('research_usage_unknown','Basic-search credit usage is unknown or above its reservation; further searches are blocked.')
            return result
        finally:
            self.spend_guard.finish(reservation,actual)

    def preflight(self):
        if self.enabled is not True:
            raise ModelError("research_disabled", "Web research is disabled.")
        if not isinstance(self.api_key, str) or not re.fullmatch(r"[!-~]{1,8192}", self.api_key):
            raise ModelError("invalid_key", "Configure a valid research key in the control process.")
        from .spending import SpendError
        try:
            self.spend_guard.check('tavily')
        except SpendError as exc:
            raise ModelError(exc.code,str(exc)) from None


def load_transport(env_file, *, enabled=False, spend_guard=None):
    """Read only the Tavily value, and only after explicit activation/funding checks."""
    from .spending import SpendGuard, SpendError
    guard=spend_guard or SpendGuard()
    if enabled is not True:
        return TavilyTransport('',enabled=False,spend_guard=guard)
    try:
        guard.check('tavily')
    except SpendError as exc:
        raise ModelError(exc.code,str(exc)) from None
    path=Path(env_file)
    if path.is_symlink() or path.stat().st_size>65536:
        raise ModelError('invalid_key','Use a bounded regular local env file.')
    values=[]
    for line in path.read_text().splitlines():
        match=re.fullmatch(r'\s*(?:export\s+)?TAVILY_API_KEY\s*=\s*(.*?)\s*',line)
        if match:
            value=match[1]
            if len(value)>1 and value[0]==value[-1] and value[0] in {'"',"'"}:
                value=value[1:-1]
            values.append(value)
    if len(values)!=1:
        raise ModelError('invalid_key','Configure one Tavily API key in the selected local env file.')
    transport=TavilyTransport(values[0],enabled=True,spend_guard=guard)
    transport.preflight()
    return transport


class WebResearcher:
    """Looks up the project's own installation docs; never fetches pages itself and never sees fixtures or answers."""

    def __init__(self, transport, *, max_results=3, max_searches=2, domains=DOC_DOMAINS):
        if type(max_results) is not int or not 1 <= max_results <= 5 or type(max_searches) is not int or not 1 <= max_searches <= 2:
            raise ModelError("invalid_research_limits", "Use 1–5 results and at most 2 searches per run.")
        self.transport, self.max_results, self.max_searches, self.domains = transport, max_results, max_searches, tuple(domains)
        self.records = []

    def _allowed(self, url, owner, project):
        try:
            parsed=urlsplit(url) if isinstance(url,str) else None
            if not parsed or parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.port not in {None,443}:
                return False
            host=parsed.hostname
            if not host or not any(host==d or host.endswith('.'+d) for d in self.domains):
                return False
            if host=='github.com':
                return parsed.path.lower().startswith(f'/{owner}/{project}/'.lower())
            if host.endswith(('.readthedocs.io','.readthedocs.org')):
                return host.split('.')[0] in {project.lower(),project.lower().replace('_','-')}
            return host in {'pypi.org','docs.python.org','packaging.python.org','pip.pypa.io','python.org'}
        except ValueError:
            return False

    def lookup(self, contract, round_index=1):
        if len(self.records) >= self.max_searches:
            return {"status": "skipped", "reason": "search_limit"}
        # Only the public repository identity goes out: no fixtures, expected outputs, logs or keys.
        owner, name = contract.data["repo"]["url"].rstrip("/").split("/")[-2:]
        query = f"{owner}/{name} {name} Python package installation command"
        payload = {"query": query, "search_depth": "basic", "max_results": self.max_results, "topic": "general",
                   "include_domains": list(self.domains), "include_answer": False, "include_raw_content": False,
                   "include_usage": True, 'auto_parameters':False, 'include_domains_mode':'restrict'}
        secrets = (getattr(self.transport, "api_key", ""),)
        started = time.monotonic()
        record = {"round": round_index, "query": query, "searched_at": now(), "data_origin": self.transport.data_origin}
        self.records.append(record)  # counts against the limit even if it fails: a failed search may still be billed
        response = self.transport.search(payload)
        if not isinstance(response,dict):
            raise ModelError('invalid_response','Web research must return a structured response.')
        results = []
        for item in (response.get("results") if isinstance(response, dict) and isinstance(response.get("results"), list) else []):
            if len(results) >= self.max_results:
                break
            if isinstance(item, dict) and self._allowed(item.get("url"),owner,name) and not any(secret and secret in item['url'] for secret in secrets):
                results.append({"title": excerpt(item.get("title") or "", 200, secrets), "url": item["url"],
                                "content": excerpt(item.get("content") or "", 800, secrets)})
        usage = response.get("usage") if isinstance(response, dict) else None
        credits = usage.get("credits") if isinstance(usage, dict) and type(usage.get("credits")) is int else None
        record.update(status="ok", results=results, credits=credits, latency_ms=round((time.monotonic() - started) * 1000),
                      request_id=response.get("request_id") if isinstance(response.get("request_id"), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}',response['request_id']) else None)
        return record
