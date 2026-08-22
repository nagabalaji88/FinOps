# The model layer

How a request reaches a provider, what happens when one fails, and which parts are
deliberate rather than incidental.

## Shape

```
.env / environment          credentials and tuning
      ↓
app/core/config.py          typed settings, read once at import
      ↓
app/llm/catalog.py          the model catalogue: capabilities and list prices
      ↓
app/llm/router.py           selection, fallback, retries, metering
      ↓
app/llm/providers/*.py      native clients (no SDK, no LiteLLM)
```

Providers are hand-written against each vendor's HTTP API rather than routed through an
abstraction layer. The cost is four client implementations; the benefit is that a provider's
own error statuses survive to the point where the platform decides what to do about them,
which is what the classification below depends on.

## Credentials

`configured` means the settings are present. It does not mean a call will succeed — a
rotated, mistyped or expired key is still "configured". Two methods answer the two
questions separately:

| Method | Answers |
|---|---|
| `configured_providers()` | which providers have settings |
| `usable_providers()` | which are configured *and* not circuit-broken |

`select()` prefers usable providers and falls back to the merely-configured set when every
breaker is open, so an operator whose provider is down is told the provider is down —
not told to set a key that is already set. `PROVIDER_KEY_ENV_VARS` names the variable for
each provider, and `ProviderNotConfiguredError` carries it: "unavailable" on its own leaves
an operator guessing which of nine credentials is missing.

## Failure classification

The distinction that matters most in this layer:

```python
RETRYABLE_PROVIDER_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
```

Everything else a provider returns in the 4xx range is a defect in the request or the
credential and is identical on every attempt. `is_transient()` in `app/core/errors.py` is
the single predicate; **both** the retry ladder and the circuit breaker consult it.

Retrying a 401 burns the full ladder to arrive at the same rejection. Worse, counting those
attempts as outages opens the breaker, and every subsequent caller then gets

```
CircuitOpenError: Circuit 'llm:anthropic' is open
```

instead of the one message that says how to fix it. Two requests with a bad key were enough
to reach the five-failure threshold, so the actual cause disappeared before anyone read it.
Now a rejected credential fails on the first attempt, leaves the breaker closed, and reports
what the provider said.

Transport errors carry no status and stay retryable: a connection reset never reached the
provider, so nothing has been ruled out.

An unrecognised exception is treated as transient. A genuine outage this list does not name
yet must still trip the breaker; the failure mode of guessing wrong in that direction is a
slow call, and in the other direction it is an unprotected dependency.

## Errors that reach a client

`app/core/redaction.py` reduces a provider's error body to the part an operator can act on.
Provider bodies routinely echo the offending request — which here means customer data — and
embed SDK internals, absolute paths and key fragments.

The goal is a redaction rather than a blank: `authentication_error: invalid x-api-key`
survives, which is the difference between a one-minute fix and an hour of guessing.

## Accounting

Every call returns tokens, cost, latency and provider through `_meter()`, which writes to
Prometheus and to the cost ledger via registered sinks. When a provider omits its usage
block — common behind a gateway — tokens are estimated from the text rather than reported as
zero. Reporting zero would silently make every call through that gateway free, and the
per-execution budget cap is computed from these numbers.

## JSON replies

`extract_json_object()` (`app/llm/jsonio.py`) recovers an object from a reply that is not
only JSON: fenced blocks, leading prose, trailing commentary. The scan tracks string state,
so a brace inside a value does not end the object early — which a plain `\{.*\}` match gets
wrong in exactly the cases that carry customer text.

`router.chat_json()` issues **one** corrective call if the first reply still does not parse,
and the returned response carries the summed tokens and cost of both. Charging only the
second would under-report the run. The alternative to repairing is what the planner used to
do: fall back to an empty structure, producing a plausible-looking execution with none of
the requested reasoning in it and no error to explain why.

## Testing without a provider

`tests/conftest.py` sets `FINOPS_IGNORE_DOTENV=1` before importing settings. Clearing
`os.environ` cannot reach a `.env` file on disk, so without it a developer's working
configuration — real keys, a real database URL — leaks into the suite.

`python -m app.cli verify-provider` reports which model-dependent paths are actually proven
and returns non-zero while any remain unproven. Any OpenAI-compatible endpoint works as a
local target via `OLLAMA_BASE_URL`, which requires no key.
