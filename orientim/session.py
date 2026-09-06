"""The public surface: record() and replay()."""
import contextlib
import os
import re
import sys
import threading
import time
import warnings
import httpx

from . import chain, detect, diagnose as _diag, scope, shims, store, transport

# --- global httpx patching --------------------------------------------------
# Patching a class that the whole process shares is the price of working with
# code nobody wrote for us. What it must never do is leave the process worse
# than it found it, which is what a plain save-and-restore does as soon as two
# blocks overlap: the second to enter restores the first one's wrapper, and
# every client built afterwards is quietly attached to a recording that ended.

_patch_lock = threading.RLock()
_sessions = []          # active scopes, innermost last
_orig = {}              # the true, unpatched __init__s
_warned = set()


def _pick():
    """Which recording a client built right now belongs to.

    scope.current() answers per asyncio task and per thread. Thread id was the
    old answer and it was wrong for every async server: all coroutines share one
    thread, so with five requests in flight the innermost block absorbed
    everybody's calls and four recordings came out empty.
    """
    region = scope.current()
    if region is not None:
        wrappers = region.data.get("httpx")
        if wrappers is not None:
            return wrappers
    # scope could not attribute this thread to a region — a worker thread spawned
    # inside a block, which starts with an empty context. Fall back to the sole
    # active session when there is exactly one; with several open concurrently,
    # guessing the innermost would file this client's traffic under a different
    # recording (its bodies and secrets included), so capture nothing instead.
    # Mirrors the same decision in scope.current().
    return _sessions[-1] if len(_sessions) == 1 else None


def _wrap_client(self, key):
    """Attach the active scope's transport to a freshly built client."""
    wrappers = _pick()
    if wrappers is None:
        return
    wrapper = wrappers[key]

    def one(t):
        if t is None or getattr(t, "orientim_wrapped", False):
            return t
        return wrapper(t)

    try:
        self._transport = one(self._transport)
        self._mounts = {k: one(v) for k, v in self._mounts.items()}
    except AttributeError:
        # httpx changed shape under us. Breaking every client the user builds
        # is a far worse outcome than capturing nothing, so say it once and
        # get out of the way.
        if "shape" not in _warned:
            _warned.add("shape")
            warnings.warn(
                "Orientim cannot instrument httpx %s (its Client internals "
                "changed); this run is not being captured."
                % getattr(httpx, "__version__", "?"),
                RuntimeWarning, stacklevel=3)


def _install():
    _orig["sync"] = httpx.Client.__init__
    _orig["async"] = httpx.AsyncClient.__init__

    def sync_init(self, *a, **kw):
        _orig["sync"](self, *a, **kw)
        _wrap_client(self, "sync")

    def async_init(self, *a, **kw):
        _orig["async"](self, *a, **kw)
        _wrap_client(self, "async")

    httpx.Client.__init__ = sync_init
    httpx.AsyncClient.__init__ = async_init


def _uninstall():
    httpx.Client.__init__ = _orig["sync"]
    httpx.AsyncClient.__init__ = _orig["async"]
    _orig.clear()


@contextlib.contextmanager
def _patch_httpx(wrap_sync, wrap_async, region=None):
    """Instrument every httpx client built inside this block.

    Without this, Orientim only sees traffic from a client it handed you, which
    means it sees nothing from the OpenAI or Anthropic SDKs, from LangChain, or
    from any tool a person already wrote. Requiring people to rewrite their
    agent around our client is the same as not shipping.
    """
    wrappers = {"sync": wrap_sync, "async": wrap_async}
    if region is not None:
        region.data["httpx"] = wrappers
    with _patch_lock:
        _sessions.append(wrappers)
        if len(_sessions) == 1:
            _install()
    try:
        yield
    finally:
        with _patch_lock:
            _sessions.remove(wrappers)
            if not _sessions:
                _uninstall()


# --- environment ------------------------------------------------------------

def _env_names(env):
    """Which environment variables a recording is allowed to hold.

    Nothing, unless asked. A blanket os.environ snapshot writes every API key
    in the process into a file people attach to bug reports — this used to do
    exactly that. Source 17 still works; you name the variables it covers.
    """
    if env is None:
        env = os.environ.get("ORIENTIM_CAPTURE_ENV") or None
    if not env:
        return []
    if isinstance(env, str):
        return [n.strip() for n in env.split(",") if n.strip()]
    return list(env)


# A variable name that looks like a secret. env= is opt-in, but "only name the
# ones that are not secrets" is a rule a copied snippet or a tired afternoon
# breaks, and the cost of breaking it is a live key in a shared file. Matching
# on the name — not the value — refuses OPENAI_API_KEY, AWS_SECRET_ACCESS_KEY,
# GITHUB_TOKEN and the like before the value is ever read.
_ENV_SECRET_RE = re.compile(
    r"SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|APIKEY|API_KEY|ACCESS_KEY"
    r"|_KEY$|^KEY$", re.IGNORECASE)


def _safe_env_names(names):
    """Drop secret-shaped variable names, and say which and why.

    Blocking a variable that is not really a secret is recoverable — rename it,
    or set the value in the replay environment yourself. Writing a real key into
    a recording is not. So the ambiguous case errs towards refusing.
    """
    safe, blocked = [], []
    for n in names:
        (blocked if _ENV_SECRET_RE.search(n) else safe).append(n)
    if blocked:
        warnings.warn(
            "Orientim refused to record environment variable(s) whose name "
            "looks like a secret: %s. The value is NOT in the recording. If one "
            "is genuinely not a secret, rename it or set it in the replay "
            "environment yourself." % ", ".join(sorted(blocked)),
            RuntimeWarning, stacklevel=3)
    return safe


def _trace_ids():
    """Tie a recording to the trace the host application is already emitting.

    Nothing is imported that is not already there, and no dependency is added.
    An on-call engineer looking at a failed span in Grafana can then find the
    run id without knowing Orientim exists.
    """
    otel = sys.modules.get("opentelemetry.trace")
    if otel is not None:
        try:
            ctx = otel.get_current_span().get_span_context()
            if getattr(ctx, "is_valid", False):
                return {"trace_id": format(ctx.trace_id, "032x"),
                        "span_id": format(ctx.span_id, "016x")}
        except Exception:
            pass
    # W3C traceparent, which is how CI systems and sidecars pass it along.
    tp = os.environ.get("TRACEPARENT", "")
    parts = tp.split("-")
    if len(parts) >= 3 and len(parts[1]) == 32:
        return {"trace_id": parts[1], "span_id": parts[2]}
    return {}


@contextlib.contextmanager
def record(root="runs", tags=None, ring=512, env=None, always=False,
           on_capture=None):
    """Record a run.

    env: names of environment variables to store, so that a replay sees the
    values the recording saw (source 17). Defaults to none — set it explicitly,
    or through ORIENTIM_CAPTURE_ENV, and only for variables that are not
    secrets.

    always: write the file even if nothing triggered. The default is to keep
    steps in memory and write only on an exception, a 5xx, an uncaptured
    source, or an explicit run.rec.trigger(...) — which is right for a
    long-running agent and wrong for the case this tool exists for. An agent
    that returns a *wrong* answer raises nothing and returns 200, so nothing
    triggers and nothing is saved. Either call trigger() from your own quality
    check, or set always=True (or ORIENTIM_ALWAYS=1) while developing.

    on_capture: called as fn(path, meta) after a file is written, so a
    recording can announce itself to your alerting instead of waiting to be
    found by somebody running `orientim ls`.
    """
    if not always:
        always = os.environ.get("ORIENTIM_ALWAYS", "").lower() in ("1", "true", "yes")
    rec = store.Recording(tags=tags, ring=ring)
    rec.env = {n: os.environ[n]
               for n in _safe_env_names(_env_names(env)) if n in os.environ}
    rec.trace = _trace_ids()
    inner = httpx.HTTPTransport()   # loads the system trust store, ~1s on Windows
    wrap_s = lambda t: transport.RecordTransport(t, rec)
    wrap_a = lambda t: transport.AsyncRecordTransport(t, rec)
    with scope.bound(rec, "record") as region, \
            shims.active("record", rec, region=region), \
            detect.watching(rec, region=region), \
            _patch_httpx(wrap_s, wrap_a, region=region):
        holder = _Holder(rec, transport.RecordTransport(inner, rec))
        # monotonic is NOT shimmed, so it is safe here. time.time() would
        # consume a recorded shim entry and shift the whole replay queue by
        # one, which silently drops conformance from 85% to 75%.
        rec.t0 = time.monotonic()   # clock starts with the agent, not with the
                                    # transport that took a second to build
        try:
            yield holder
        except Exception as e:
            rec.status = "failed"
            rec.trigger(f"exception:{type(e).__name__}")
            raise
        finally:
            rec.ended_at = time.time()
            shims.settle(rec)
            holder.close()
            holder.path = rec.save(root, force=always)
            if holder.path and on_capture:
                try:
                    on_capture(holder.path, rec.meta())
                except Exception as hook_error:
                    # The hook is the user's alerting. A broken alert must not
                    # become the exception their agent raises.
                    warnings.warn("Orientim on_capture raised %s: %s"
                                  % (type(hook_error).__name__, hook_error),
                                  RuntimeWarning, stacklevel=2)


class _Holder:
    def __init__(self, rec, tr):
        self.rec = rec
        self._tr = tr
        self._clients = []
        self.path = None

    def client(self, **kw):
        """A client wired to this run. Closed for you when the block ends.

        An agent that calls this in a loop used to leave one connection pool
        per call open until the garbage collector felt like it.
        """
        c = httpx.Client(transport=self._tr, timeout=10.0, **kw)
        self._clients.append(c)
        return c

    def close(self):
        for c in self._clients:
            try:
                c.close()
            except Exception:
                pass
        self._clients = []


class Divergence:
    def __init__(self, ok, index, reason, recorded_root, replay_root, uncaptured,
                 blocked, n_attempted=0, n_recorded=0, n_matched=0, dropped=0,
                 raised=None, no_steps=False, stale=False, headers_changed=False,
                 unseen=(), unseen_n=0, incomplete=False, patched=()):
        self.n_attempted = n_attempted
        self.n_recorded = n_recorded
        self.n_matched = n_matched
        self.dropped = dropped
        self.ok = ok
        self.index = index
        self.reason = reason
        self.recorded_root = recorded_root
        self.replay_root = replay_root
        self.uncaptured = uncaptured
        self.blocked = blocked
        self.raised = raised            # exception the replay ended on, if any
        self.no_steps = no_steps        # the recording holds no http at all
        self.stale = stale              # written by an older recording format
        self.headers_changed = headers_changed
        self.unseen = list(unseen)      # traffic the recording never captured
        self.unseen_n = unseen_n or len(self.unseen)
        self.incomplete = incomplete    # a response that was never drained
        self.patched = list(patched)    # steps whose response we replaced

    @property
    def diagnosis(self):
        return _diag.diagnose(self, self.n_attempted, self.n_recorded,
                              self.dropped)

    def report(self):
        code, title, msg, action = self.diagnosis
        mark = "OK" if self.ok else "!!"
        head = f"  {mark}  {title}" + ("" if self.ok else f"   [{code}]")
        lines = [head, f"      {msg}"]
        if action:
            lines.append(f"      -> {action}")
        return "\n".join(lines)

    def __repr__(self):
        if self.ok:
            return f"<identical root={self.recorded_root[:12]}>"
        return f"<{self.diagnosis[0]} @ step {self.index}>"


@contextlib.contextmanager
def _recorded_env(names):
    """Apply the recorded variables, then put the process back as it was.

    The old version cleared os.environ outright and restored it after the call
    returned — so an interrupt in the middle left the agent's environment
    replaced by the recording's, permanently.
    """
    saved = {n: os.environ.get(n) for n in names}
    try:
        yield
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v


def assert_replays(path, fn, strict=True, **kw):
    """Fail a test with the whole diagnosis, not with `assert False`.

        def test_no_regression():
            orientim.assert_replays("runs/run_2bea9035.jsonl",
                                    lambda _: my_agent())

    Returns the report when it passes, so a test can go on to check something
    else about it.
    """
    report = replay(path, fn, strict=strict, **kw)
    if not report.ok:
        raise AssertionError(chr(10) + report.report())
    return report


PATCHABLE = ("status", "body", "b64", "headers", "error")


def _apply_patch(http_steps, patch):
    """Replace what a step returned, so you can ask what would have happened.

    Indices are positions among the HTTP steps — the numbers `orientim play`
    and the timeline show — not the raw step index, which also counts clock and
    randomness entries.
    """
    import copy
    import hashlib
    from .transport import body_bytes

    out = [copy.deepcopy(s) for s in http_steps]
    applied = []
    for idx, changes in sorted(patch.items()):
        if not isinstance(idx, int) or not 0 <= idx < len(out):
            raise ValueError(
                "patch step %r is not one of the %d recorded steps (0-%d)"
                % (idx, len(out), len(out) - 1))
        unknown = set(changes) - set(PATCHABLE)
        if unknown:
            raise ValueError("cannot patch %s; patchable fields are %s"
                             % (", ".join(sorted(unknown)), ", ".join(PATCHABLE)))
        step = out[idx]
        step.update(changes)
        if "error" in changes and not changes["error"]:
            step.pop("error", None)
        if "body" in changes:
            # The recorded chunk shape belongs to the recorded body. A replaced
            # body is one chunk, and its hash has to match what is handed back
            # or the step would claim bytes it does not have.
            raw = body_bytes(step)
            step["chunks"] = [[0.0, len(raw)]]
            step["body_sha"] = hashlib.sha256(raw).hexdigest()[:32]
        step["patched"] = True
        applied.append(idx)
    return out, applied


def replay(path, fn, strict=True, on_step=None, realtime=False, patch=None):
    """Run fn() again, fed by the past.

    Returns a divergence report: identical, or the first step that differed.

    realtime=True reproduces the recorded gaps between streamed chunks, for a
    bug that depends on how long a stream went quiet. Off by default, because
    replay being fast is half the point of replay.

    patch={step: {...}} replaces what a step returned, which turns a replay
    into a question rather than a check:

        orientim.replay(path, agent, patch={
            3: {"body": '{"hits": ["order 4471 shipped"]}'},   # if search had found it
            5: {"status": 429},                                # if we had been rate limited
        })

    A patched replay is never IDENTICAL — it is not a reproduction. It reports
    COUNTERFACTUAL and tells you where the agent's own requests started to
    differ from the recorded ones, which is the blast radius of the change.
    """
    meta, steps = store.load(path)
    meta = meta or {}
    http_steps = [s for s in steps if s.get("t") == "http"]
    shim_steps = [s for s in steps if s.get("t") == "shim"]
    field = "key_strict" if strict else "key_loose"

    original_steps = http_steps
    patched_idx = []
    if patch:
        http_steps, patched_idx = _apply_patch(http_steps, patch)

    rec = store.Recording(run_id=meta.get("run_id", "run") + "_replay")
    rec.replay_random_state = meta.get("random_state")
    tr = transport.ReplayTransport(http_steps, rec, strict=strict, on_step=on_step,
                                   realtime=realtime)
    tr_async = transport.AsyncReplayTransport(http_steps, rec, strict=strict,
                                              on_step=on_step, realtime=realtime)
    # Sync and async share one queue, so an agent that mixes them still replays
    # in the recorded order.
    tr_async.sync = tr

    recorded_env = meta.get("env") or {}
    raised = None
    with _recorded_env(recorded_env):
        for name, value in recorded_env.items():
            os.environ[name] = value
        with scope.bound(rec, "replay") as region, \
                shims.active("replay", rec, shim_steps, region=region), \
                _patch_httpx(lambda t: tr, lambda t: tr_async, region=region):
            holder = _Holder(rec, tr)
            try:
                fn(holder)
            except Exception as e:
                rec.status = "failed"
                rec.trigger(f"exception:{type(e).__name__}")
                raised = type(e).__name__
            finally:
                holder.close()

    replayed = [s for s in rec.steps if s.get("t") == "http"]
    a, root_a = chain.build_steps(http_steps, field)
    b, root_b = chain.build_steps(replayed, field)
    idx = chain.first_divergence(a, b)

    if patched_idx:
        # Judge a counterfactual on what the agent asked, not on what we handed
        # it: the responses differ because we made them differ.
        qa, _ = chain.build_steps(original_steps, field, requests_only=True)
        qb, _ = chain.build_steps(replayed, field, requests_only=True)
        return Divergence(
            False, chain.first_divergence(qa, qb), "counterfactual",
            root_a, root_b, rec.uncaptured, rec.blocked,
            rec.attempts, len(original_steps), len(replayed),
            raised=raised, patched=patched_idx)

    # An exception the recording ended on is part of the recording. Raising the
    # same one again is a faithful replay, not a divergence.
    expected_exc = (meta.get("trigger") or "")
    expected_exc = expected_exc[10:] if expected_exc.startswith("exception:") else None
    unexpected_raise = raised if raised != expected_exc else None

    nk, nr, npm = rec.attempts, len(http_steps), len(replayed)
    dropped = meta.get("dropped", 0)
    stale = meta.get("format", 1) < store.FORMAT
    no_steps = not http_steps
    # Part of the recorded run left the process through a library we do not
    # intercept. Whatever the chain says, this recording does not hold the whole
    # run, and "identical" would be a claim about a fraction of it.
    unseen = meta.get("unseen") or []
    incomplete = [s for s in http_steps if s.get("complete") is False]

    hdr_changed = False
    if idx is not None and idx < len(http_steps) and idx < len(replayed):
        old, new = http_steps[idx], replayed[idx]
        hdr_changed = (old.get(field) == new.get(field)
                       and old.get("hdr_fp") != new.get("hdr_fp"))

    clean = (idx is None and not rec.uncaptured and not unexpected_raise
             and not dropped and not no_steps and not stale and not unseen
             and not incomplete)
    if clean:
        return Divergence(True, None, None, root_a, root_b, [], rec.blocked,
                          nk, nr, npm, dropped)

    reason = (rec.uncaptured[0]["detail"] if rec.uncaptured
              else (f"replay raised {unexpected_raise}" if unexpected_raise
                    else unseen[0]["detail"] if unseen
                    else "chain hash differs"))
    if idx is None:
        idx = incomplete[0].get("i", 0) if incomplete else len(replayed)
    return Divergence(False, idx, reason, root_a, root_b, rec.uncaptured,
                      rec.blocked, nk, nr, npm, dropped, raised=unexpected_raise,
                      no_steps=no_steps, stale=stale, headers_changed=hdr_changed,
                      unseen=unseen, unseen_n=meta.get("unseen_n", 0),
                      incomplete=bool(incomplete))
