"""Explicit local paid activation, verified catalog, and one ledger per slot."""
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

from .diagnostics import RepairPlanner
from .models import ModelCaller, ModelError, ModelLedger, NebiusTransport, money
from .prompt_budget import NemotronPromptBound
from .protocols import PROTOCOLS
from .sandboxes import SandboxConfig, NoRedirect

CATALOG = "https://api.tokenfactory.nebius.com/v1/models?verbose=true"


class OwnerModelFactory:
    def __init__(self, specs, env_file, reference_dir, *, enabled=False, opener=None, web_research_enabled=False):
        if enabled is not True:
            raise ModelError("paid_calls_disabled", "Local paid activation must be explicit; no key was read.")
        if type(web_research_enabled) is not bool:
            raise ModelError("research_disabled", "Web research activation must be an explicit boolean.")
        self.web_research_enabled = web_research_enabled
        self._env_file = env_file
        self.specs = specs
        self.bound = NemotronPromptBound(reference_dir)
        config = SandboxConfig.load(env_file, environ={})
        if not config.api_key:
            raise ModelError("missing_key", "Configure the key in the explicitly selected local env file.")
        self._key = config.api_key
        opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        request = Request(CATALOG, headers={"Authorization": "Bearer " + self._key, "Accept": "application/json"})
        try:
            with opener.open(request, timeout=20) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError()
            data = json.loads(raw)
            available = {m["id"]: m for m in data["data"]}
            checked = []
            for spec in specs.values():
                model = available[spec.model_id]
                prices = model["pricing"]
                cached = prices.get("input_cache_read")
                if (model["context_length"] != spec.context_window or
                    money(prices["prompt"]) * 1_000_000 != money(spec.input_per_million) or
                    money(prices["completion"]) * 1_000_000 != money(spec.output_per_million) or
                    money(prices["request"]) != 0 or
                    (cached is None) != (spec.cached_input_per_million is None) or
                    (cached is not None and money(cached) * 1_000_000 != money(spec.cached_input_per_million))):
                    raise ValueError()
                checked.append({"id": spec.model_id, "context_length": model["context_length"], "pricing": prices})
        except HTTPError as exc:
            exc.close()
            raise ModelError("catalog_unverified", "Catalog access failed; no inference was sent.") from None
        except (OSError, URLError, ValueError, KeyError, TypeError):
            raise ModelError("catalog_unverified", "Model IDs, context or prices could not be matched to the freeze; no inference was sent.") from None
        self.preflight = {"checked_at": datetime.now(timezone.utc).isoformat(), "source": CATALOG,
                          "metadata_only": True, "models": checked, "key_included": False,
                          "web_research": {"enabled": web_research_enabled,
                              "max_searches_per_run": 2 if web_research_enabled else 0}}

    def caller(self, ledger_path, run_id, max_usd, *, deadline=None):
        """A budgeted caller for one optional task outside the repair loop (maintainer note, acceptance draft)."""
        return ModelCaller(NebiusTransport(self._key, enabled=True), ModelLedger(ledger_path, run_id, max_usd),
                           prompt_bound=self.bound, deadline=deadline)

    def __call__(self, contract, location, *, group, max_usd, deadline, run_id):
        ledger = ModelLedger(Path(location) / "private-ledger.sqlite3", run_id, max_usd)
        caller = ModelCaller(NebiusTransport(self._key, enabled=True), ledger, prompt_bound=self.bound, deadline=deadline)
        researcher = None
        if self.web_research_enabled:
            from .web_research import load_transport, WebResearcher
            researcher = WebResearcher(load_transport(self._env_file, enabled=True), max_searches=2)
        return RepairPlanner(caller, self.specs["nano"], self.specs["ultra"], route=PROTOCOLS[group]["route"], researcher=researcher)
