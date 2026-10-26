"""Opt-in inference transport and a durable, per-run reservation ledger.

No environment loading, automatic retries, model defaults or paid activation.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from .sandboxes import NoRedirect


ENDPOINT = "https://api.tokenfactory.nebius.com/v1/chat/completions"
MAX_BODY = 1024 * 1024


class ModelError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat()


def money(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ModelError("invalid_budget", "Money values must be finite, non-negative decimals.") from None
    if not result.is_finite() or result < 0:
        raise ModelError("invalid_budget", "Money values must be finite, non-negative decimals.")
    return result


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    context_window: int
    max_completion_tokens: int
    input_per_million: str
    output_per_million: str
    price_reference: str
    cached_input_per_million: str | None = None
    # Extra chat-completion fields from REQUEST_OPTIONS, e.g. Nemotron's reasoning switches. Frozen with the spec,
    # recorded on every ledger call; never able to override the fixed request fields.
    request_options: dict | None = field(default=None, hash=False)

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}", self.model_id):
            raise ModelError("invalid_model", "Configure an exact catalog model ID.")
        if any(type(v) is not int or not 1 <= v <= 2_000_000 for v in
               (self.context_window, self.max_completion_tokens)) or self.max_completion_tokens >= self.context_window:
            raise ModelError("invalid_model", "Configure verified context and completion limits.")
        for v in [self.input_per_million, self.output_per_million, self.cached_input_per_million]:
            if v is not None:
                money(v)
        if not self.price_reference or len(self.price_reference) > 500:
            raise ModelError("invalid_model", "A dated price reference is required.")
        check_request_options(self.request_options)

    @property
    def reservation(self):
        return self.reserve_for_input(self.context_window)

    def reserve_for_input(self, input_tokens):
        if type(input_tokens) is not int or not 1 <= input_tokens <= self.context_window:
            raise ModelError("invalid_prompt_bound", "A positive input token upper bound is required.")
        rate = max(money(self.input_per_million), money(self.cached_input_per_million or self.input_per_million))
        return (input_tokens * rate + self.max_completion_tokens * money(self.output_per_million)) / 1_000_000


# Nemotron 3 reads these through the chat template (model card: enable_thinking, medium_effort for fewer reasoning
# tokens, force_nonempty_content so an answer follows the reasoning). Anything else is refused.
REQUEST_OPTIONS = {"chat_template_kwargs": {"enable_thinking", "medium_effort", "force_nonempty_content"},
                   "temperature": None, "top_p": None}


def spec_dict(spec):
    """The public fields as frozen; specs without request options keep the shape of freezes made before they existed."""
    data = asdict(spec)
    if data["request_options"] is None:
        del data["request_options"]
    return data


def check_request_options(options):
    if options is None:
        return
    bad = ModelError("invalid_model", "Request options allow only reviewed Nemotron template switches, temperature and top_p.")
    if not isinstance(options, dict) or not options or set(options) - set(REQUEST_OPTIONS):
        raise bad
    kwargs = options.get("chat_template_kwargs")
    if "chat_template_kwargs" in options and (not isinstance(kwargs, dict) or not kwargs or
                                              set(kwargs) - REQUEST_OPTIONS["chat_template_kwargs"] or
                                              any(type(v) is not bool for v in kwargs.values())):
        raise bad
    for name, low, high in [("temperature", 0, 2), ("top_p", 0, 1)]:
        value = options.get(name)
        if name in options and (type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high
                                or (name == "top_p" and value == 0)):
            raise bad


class ModelLedger:
    """One immutable budget per run; BEGIN IMMEDIATE serializes reservations."""

    def __init__(self, path, run_id, max_usd):
        self.path, self.run_id, self.budget = Path(path), run_id, money(max_usd)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS config (singleton INTEGER PRIMARY KEY, run_id TEXT, budget TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, held TEXT, body TEXT)")
            saved = db.execute("SELECT run_id, budget FROM config WHERE singleton=1").fetchone()
            if saved and (saved[0] != run_id or money(saved[1]) != self.budget):
                raise ModelError("ledger_mismatch", "An existing ledger cannot change run ID or budget.")
            if not saved:
                db.execute("INSERT INTO config VALUES (1, ?, ?)", (run_id, str(self.budget)))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def reserve(self, stage, spec, origin, prompt_hash, *, input_bound=None, bound_reference=None):
        if input_bound is not None and (not isinstance(bound_reference, str) or not 1 <= len(bound_reference) <= 500):
            raise ModelError("invalid_prompt_bound", "A request bound needs a reviewed tokenizer reference.")
        amount = spec.reservation if input_bound is None else spec.reserve_for_input(input_bound)
        with self.connection() as db:
            rows = db.execute("SELECT held, body FROM calls").fetchall()
            for row in rows:
                previous = json.loads(row[1])
                if previous["finished_at"] and previous.get("calculated_usd") is None:
                    raise ModelError("billing_unresolved", "No request was sent: a previous charge is unresolved.")
            if any(json.loads(row[1])["stage"] == stage for row in rows):
                raise ModelError("stage_already_attempted", "No request was sent: this stage already has a durable reservation; explicit rounds need distinct stage IDs.")
            held = sum((money(r[0]) for r in rows), Decimal(0))
            if amount <= 0 or held + amount > self.budget:
                raise ModelError("budget_exhausted", "No request was sent: the per-run reservation budget is insufficient.")
            call_id = uuid.uuid4().hex
            entry = {"call_id": call_id, "run_id": self.run_id, "stage": stage, "data_origin": origin,
                     "requested_model": spec.model_id, "returned_model": None, "provider_request_id": None,
                     "started_at": now(), "finished_at": None, "status": "reserved", "usage": None,
                     "dispatch_started_at": None,
                     "price_reference": spec.price_reference, "input_per_million": str(spec.input_per_million),
                     "output_per_million": str(spec.output_per_million), "cached_input_per_million": spec.cached_input_per_million,
                     "reservation_usd": str(amount), "calculated_usd": None, "billed_usd": None,
                     "input_tokens_upper_bound": input_bound if input_bound is not None else spec.context_window,
                     "input_bound_reference": bound_reference or "full_model_context",
                     "billing_status": "unknown", "prompt_sha256": prompt_hash,
                     "request_options": spec.request_options}
            db.execute("INSERT INTO calls VALUES (?, ?, ?)", (call_id, str(amount), json.dumps(entry)))
        return call_id

    def mark_dispatched(self, call_id):
        with self.connection() as db:
            row = db.execute("SELECT body FROM calls WHERE id=?", (call_id,)).fetchone()
            if not row:
                raise ModelError("ledger_missing", "The reserved call is missing.")
            entry = json.loads(row[0])
            if entry.get("dispatch_started_at") or entry["finished_at"]:
                raise ModelError("ledger_finalized", "A reserved call cannot be dispatched twice.")
            entry.update(dispatch_started_at=now(), status="dispatching")
            db.execute("UPDATE calls SET body=? WHERE id=?", (json.dumps(entry), call_id))

    def finish(self, call_id, **fields):
        with self.connection() as db:
            row = db.execute("SELECT body FROM calls WHERE id=?", (call_id,)).fetchone()
            if not row:
                raise ModelError("ledger_missing", "The reserved call is missing.")
            entry = json.loads(row[0])
            if entry["finished_at"] is not None:
                raise ModelError("ledger_finalized", "The call has already been finalized.")
            entry.update(fields, finished_at=now())
            amount = money(entry["reservation_usd"])
            if entry.get("calculated_usd") is not None:
                amount = money(entry["calculated_usd"])
                # Cached pricing and unknown outcomes retain a conservative hold.
                if entry.get("billing_status") != "usage_calculated_pending_reconciliation":
                    amount = max(amount, money(entry["reservation_usd"]))
            if entry.get("dispatch_started_at") is None and entry.get("billing_status") == "not_dispatched":
                amount = Decimal(0)
            db.execute("UPDATE calls SET held=?, body=? WHERE id=?", (str(amount), json.dumps(entry), call_id))

    def summary(self):
        with self.connection() as db:
            rows = db.execute("SELECT held, body FROM calls ORDER BY rowid").fetchall()
        entries = [json.loads(row[1]) for row in rows]
        known = all(entry["calculated_usd"] is not None for entry in entries)
        total = sum((money(e["calculated_usd"]) for e in entries), Decimal(0)) if known else None
        return {"run_id": self.run_id, "currency": "USD", "max_usd": str(self.budget),
                "held_usd": str(sum((money(row[0]) for row in rows), Decimal(0))),
                "model_calls": sum(e["data_origin"] == "nebius_api" and bool(e.get("dispatch_started_at")) for e in entries),
                "engineering_test_calls": sum(e["data_origin"] == "engineering_test" and bool(e.get("dispatch_started_at")) for e in entries),
                "calculated_usd": str(total) if total is not None else None,
                "billed_usd": None if entries else "0", "calls": entries}


@dataclass(repr=False)
class NebiusTransport:
    api_key: str = field(repr=False)
    enabled: bool = False
    data_origin: str = field(default="nebius_api", init=False)
    spend_guard: object = field(default=None, repr=False)

    def __post_init__(self):
        from .spending import SpendGuard
        self.spend_guard = self.spend_guard or SpendGuard()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def complete(self, payload, *, timeout=60, reservation=None):
        self.preflight()
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ModelError("invalid_timeout", "Inference timeout must be positive and at most 60 seconds.")
        from .spending import SpendError
        try:
            self.spend_guard.verify_reservation(reservation, 'nebius')
        except SpendError as exc:
            raise ModelError(exc.code,str(exc)) from None
        request = Request(ENDPOINT, data=json.dumps(payload).encode(), method="POST", headers={
            "Authorization": "Bearer " + self.api_key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_BODY + 1)
        except HTTPError as exc:
            exc.close()
            raise ModelError("request_error", "Inference HTTP request failed; possible charge remains reserved.") from None
        except (OSError, URLError, TimeoutError):
            raise ModelError("request_error", "Inference request did not complete; possible charge remains reserved.") from None
        if len(raw) > MAX_BODY:
            raise ModelError("invalid_response", "Inference response exceeds 1 MiB.")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ModelError("invalid_response", "Inference response is not valid JSON.") from None

    def preflight(self):
        if self.enabled is not True:
            raise ModelError("paid_calls_disabled", "Paid inference is disabled.")
        if not isinstance(self.api_key, str) or not re.fullmatch(r"[!-~]{1,8192}", self.api_key):
            raise ModelError("invalid_key", "Configure a valid API key in the control process.")
        from .spending import SpendError
        try:
            self.spend_guard.check('nebius')
        except SpendError as exc:
            raise ModelError(exc.code,str(exc)) from None


def usage_and_cost(response, spec):
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None, None, "unknown"
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in keys):
        return None, None, "unknown"
    if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
        return None, None, "unknown"
    counts = {k: usage[k] for k in keys}
    cached = 0
    for field_name, name, bound in [("prompt_tokens_details", "cached_tokens", usage["prompt_tokens"]),
                                     ("completion_tokens_details", "reasoning_tokens", usage["completion_tokens"])]:
        detail = usage.get(field_name)
        if detail is not None:
            if not isinstance(detail, dict) or (detail.get(name) is not None and
                 (type(detail[name]) is not int or not 0 <= detail[name] <= bound)):
                return counts, None, "unknown"
            value = detail.get(name) or 0
            counts[name] = value
            if name == "cached_tokens": cached = value
    if cached and spec.cached_input_per_million is None:
        return counts, None, "unknown_cached_rate"
    cost = ((usage["prompt_tokens"] - cached) * money(spec.input_per_million) +
            cached * money(spec.cached_input_per_million or spec.input_per_million) +
            usage["completion_tokens"] * money(spec.output_per_million)) / 1_000_000
    # Reasoning tokens are included in completion_tokens, never charged twice.
    status = "usage_calculated_pending_reconciliation"
    if usage["total_tokens"] > spec.context_window or usage["completion_tokens"] > spec.max_completion_tokens:
        return counts, None, "usage_exceeds_configured_limits"
    return counts, str(cost), status


class ModelCaller:
    def __init__(self, transport, ledger, *, prompt_bound=None, deadline=None):
        if deadline is not None and (type(deadline) not in {int, float} or not math.isfinite(deadline)):
            raise ModelError("invalid_deadline", "A finite monotonic deadline is required.")
        self.transport, self.ledger, self.prompt_bound = transport, ledger, prompt_bound
        self.deadline = deadline

    def remaining(self):
        if self.deadline is None:
            return 60
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ModelError("time_limit_exceeded", "No request was sent: the shared run time limit is exhausted.")
        return min(60, remaining)

    def call(self, stage, spec, messages, validator=None):
        self.remaining()
        # ModelSpec is frozen, but nested dictionaries are not immutable.
        check_request_options(spec.request_options)
        if self.transport.enabled is not True:
            raise ModelError("paid_calls_disabled", "No request was sent: inference is disabled.")
        if self.transport.data_origin not in {"nebius_api", "engineering_test"}:
            raise ModelError("invalid_origin", "The model transport has no supported evidence origin.")
        if hasattr(self.transport, "preflight"):
            self.transport.preflight()
        if not isinstance(messages, list) or not 1 <= len(messages) <= 4 or any(
            not isinstance(m, dict) or set(m) != {"role", "content"} or m["role"] not in {"user", "system"} or
            not isinstance(m["content"], str) for m in messages
        ):
            raise ModelError("invalid_messages", "Expected bounded system/user text messages.")
        secret = getattr(self.transport, "api_key", "")
        messages = [{"role": m["role"], "content": m["content"].replace(secret, "[REDACTED]") if secret else m["content"]} for m in messages]
        payload = {**(spec.request_options or {}), "model": spec.model_id, "messages": messages, "stream": False,
                   "max_completion_tokens": spec.max_completion_tokens, "response_format": {"type": "json_object"}}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        if len(raw) > 196608:
            raise ModelError("context_too_large", "No request was sent: the diagnostic context exceeds its limit.")
        bound, reference = self.prompt_bound(spec, messages) if self.prompt_bound else (None, None)
        if bound is not None and (type(bound) is not int or bound + spec.max_completion_tokens > spec.context_window):
            raise ModelError("context_too_large", "No request was sent: bounded input and output exceed the model context.")
        call_id = self.ledger.reserve(stage, spec, self.transport.data_origin, hashlib.sha256(raw).hexdigest(),
                                      input_bound=bound, bound_reference=reference)
        started = time.monotonic()
        fields = {"status": "invalid_response"}
        global_reservation, dispatched = None, False
        try:
            try:
                timeout = self.remaining()
            except ModelError:
                fields.update(calculated_usd="0", billed_usd="0", billing_status="not_dispatched")
                raise
            if isinstance(self.transport,NebiusTransport):
                from .spending import SpendError
                try:
                    global_reservation=self.transport.spend_guard.reserve('nebius',spec.reserve_for_input(bound or spec.context_window),'model:'+stage)
                except SpendError as exc:
                    raise ModelError(exc.code,str(exc)) from None
            self.ledger.mark_dispatched(call_id)
            dispatched=True
            if isinstance(self.transport,NebiusTransport):
                response=self.transport.complete(payload,timeout=timeout,reservation=global_reservation)
            else:
                response = self.transport.complete(payload, timeout=timeout) if self.deadline is not None else self.transport.complete(payload)
            if not isinstance(response, dict):
                raise ModelError("invalid_response", "The inference response must be an object.")
            usage, cost, billing = usage_and_cost(response, spec)
            if bound is not None and usage and usage["prompt_tokens"] > bound:
                cost, billing = None, "prompt_bound_exceeded"
            returned = response.get("model")
            if returned != spec.model_id:
                cost, billing = None, "model_price_mismatch"
            fields.update(usage=usage, calculated_usd=cost, billing_status=billing,
                          returned_model=returned if isinstance(returned, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,200}", returned) else None)
            provider_id = response.get("id")
            if isinstance(provider_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", provider_id):
                fields["provider_request_id"] = provider_id
            if billing == "prompt_bound_exceeded":
                raise ModelError("prompt_bound_exceeded", "Provider usage exceeded the reviewed input bound; further dispatch is blocked.")
            if returned != spec.model_id:
                raise ModelError("model_mismatch", "The returned model does not match the configured price reference.")
            choices = response.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ModelError("invalid_response", "Expected exactly one inference choice.")
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("refusal") or message.get("tool_calls"):
                raise ModelError("model_refused", "The model did not return an admissible JSON answer.")
            finish = choice.get("finish_reason")
            content = message.get("content")
            fields["response_finish_reason"] = finish if finish in {"stop", "length", "content_filter", "tool_calls"} else "unknown"
            fields["response_content_bytes"] = len(content.encode()) if isinstance(content, str) else None
            if finish != "stop":
                reasoning = (usage or {}).get("reasoning_tokens", 0)
                fields["output_diagnostic"] = ("reasoning_exhausted_completion" if finish == "length" and not content and reasoning and reasoning == (usage or {}).get("completion_tokens")
                                               else "completion_truncated" if finish == "length" else "non_stop_finish")
                raise ModelError("incomplete_output", "The model answer did not finish normally.")
            if not isinstance(content, str) or len(content.encode()) > 131072:
                raise ModelError("invalid_response", "The model answer is missing or exceeds its limit.")
            try:
                answer = validator(content) if validator else content
            except ModelError:
                raise
            except (ValueError, KeyError, TypeError):
                raise ModelError("invalid_model_output", "The model answer failed local validation.") from None
            fields["status"] = "completed"
            return answer
        except ModelError as exc:
            fields["status"] = exc.code
            if not dispatched:
                fields.update(calculated_usd='0',billed_usd='0',billing_status='not_dispatched')
            raise
        finally:
            fields["latency_ms"] = round((time.monotonic() - started) * 1000)
            self.ledger.finish(call_id, **fields)
            if global_reservation:
                self.transport.spend_guard.finish(global_reservation,fields.get('calculated_usd'),dispatched=dispatched)
