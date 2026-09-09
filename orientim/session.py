"""The public surface: record() and replay()."""
import contextlib
import os
import re
import sys
import threading
import time
import warnings
import httpx

from . import (chain, detect, diagnose as _diag, model, reqs, scope, shims, store,
               transport)

# --- global httpx patching --------------------------------------------------
# Patching a class that the whole process shares is the price of working with
# code nobody wrote for us. What it must never do is leave the process worse
# than it found it, which is what a plain save-and-restore does as soon as two
# blocks overlap: the second to enter restores the first one's wrapper, and
# every client built afterwards is quietly attached to a recording that ended.

_patch_lock = threading.RLock()
_sessions = []          # active scopes, innermost last
_orig = {}              # the true, unpatched transport methods, per library
_patched = []           # libraries we actually installed into
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


# The interception point is the transport, not the client. Every request ends up
# in HTTPTransport.handle_request, which owns the connection pool, however the
# client above it was built. Patching Client.__init__ instead missed any SDK that
# stopped subclassing httpx.Client — and missed httpx2 entirely.


class _OrigInner(httpx.BaseTransport):
    """The real network call, so RecordTransport can delegate to it.

    Holds the transport instance and the saved, unpatched handle_request, so the
    call reaches the socket exactly once and never re-enters the patch.
    """

    def __init__(self, real, orig):
        self._real = real
        self._orig = orig

    def handle_request(self, request):
        return self._orig(self._real, request)


class _AsyncOrigInner(httpx.AsyncBaseTransport):
    def __init__(self, real, orig):
        self._real = real
        self._orig = orig

    async def handle_async_request(self, request):
        return await self._orig(self._real, request)


def _http_libs():
    """Every httpx-shaped library installed, not only httpx.

    openai 3.x and anthropic 1.x build on httpx2 — a separate package with the
    same transport API and its own, incompatible stream types. Instrumenting
    only httpx means their model traffic is never seen at all.
    """
    libs = [httpx]
    try:
        import httpx2
    except ImportError:
        pass
    else:
        libs.append(httpx2)
    return libs


def _handlers(hx):
    """The patched pair for one library, closed over that library."""
    key = hx.__name__

    def sync_h(self, request):
        info = _pick()
        if info is None:
            return _orig[(key, "sync")](self, request)
        if info["mode"] == "record":
            rt = transport.RecordTransport(
                _OrigInner(self, _orig[(key, "sync")]), info["rec"], hx)
            return rt.handle_request(request)
        return info["sync"].handle_request(request, hx)   # replay: no socket

    async def async_h(self, request):
        info = _pick()
        if info is None:
            return await _orig[(key, "async")](self, request)
        if info["mode"] == "record":
            rt = transport.AsyncRecordTransport(
                _AsyncOrigInner(self, _orig[(key, "async")]), info["rec"], hx)
            return await rt.handle_async_request(request)
        return await info["async"].handle_async_request(request, hx)

    return sync_h, async_h


def _install():
    for hx in _http_libs():
        key = hx.__name__
        try:
            _orig[(key, "sync")] = hx.HTTPTransport.handle_request
            _orig[(key, "async")] = hx.AsyncHTTPTransport.handle_async_request
        except AttributeError:
            # Shaped differently from what we expect. Capturing nothing from it
            # is far better than breaking every client the host application
            # builds, so say it once and leave that library alone.
            if key not in _warned:
                _warned.add(key)
                warnings.warn(
                    "Orientim cannot instrument %s %s (its transport internals "
                    "changed); traffic through it is not being captured."
                    % (key, getattr(hx, "__version__", "?")),
                    RuntimeWarning, stacklevel=3)
            continue
        sync_h, async_h = _handlers(hx)
        hx.HTTPTransport.handle_request = sync_h
        hx.AsyncHTTPTransport.handle_async_request = async_h
        _patched.append(hx)
    # requests has no transport in the httpx sense, but HTTPAdapter.send is the
    # same seam one layer down, and it draws from the same recorded queue.
    reqs.install(_pick)


def _uninstall():
    for hx in _patched:
        key = hx.__name__
        hx.HTTPTransport.handle_request = _orig[(key, "sync")]
        hx.AsyncHTTPTransport.handle_async_request = _orig[(key, "async")]
    del _patched[:]
    _orig.clear()
    reqs.uninstall()


@contextlib.contextmanager
def _patch_httpx(info, region=None):
    """Instrument every request made inside this block, in any httpx-shaped library.

    Without this, Orientim only sees traffic from a client it handed you, which
    means it sees nothing from the OpenAI or Anthropic SDKs, from LangChain, or
    from any tool a person already wrote. Requiring people to rewrite their
    agent around our client is the same as not shipping.

    `info` says what this block does, and the transport patch reads it per
    request: {"mode": "record", "rec": ...} or {"mode": "replay", "sync": ...,
    "async": ...}.
    """
    if region is not None:
        region.data["httpx"] = info
    with _patch_lock:
        _sessions.append(info)
        if len(_sessions) == 1:
            _install()
    try:
        yield
    finally:
        with _patch_lock:
            _sessions.remove(info)
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
           on_capture=None, agent=None):
    """Record a run.

    agent: who this agent is — a name, or a dict of whatever identifies it
    ("name", "version", "framework", a commit sha). Orientim cannot infer this;
    a process does not know which product it belongs to. Unset, it falls back to
    ORIENTIM_AGENT and ORIENTIM_AGENT_VERSION, so a deployment can declare it
    once in the environment instead of at every call site.

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
    rec.agent = model.normalise_agent(agent)
    rec.env = {n: os.environ[n]
               for n in _safe_env_names(_env_names(env)) if n in os.environ}
    rec.trace = _trace_ids()
    httpx.HTTPTransport()   # warms the system trust store (~1s on Windows) before
                            # the clock starts, so step one is not billed for it
    with scope.bound(rec, "record") as region, \
            shims.active("record", rec, region=region), \
            detect.watching(rec, region=region), \
            _patch_httpx({"mode": "record", "rec": rec}, region=region):
        holder = _Holder(rec)
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
            # Read after the block has run, so `run.output = ...` set anywhere
            # inside it is seen — including on the path where the agent raised,
            # where a partial answer is often the most interesting thing there
            # is. redact_body is reused rather than reinvented: an answer is a
            # body like any other and gets the same rule.
            rec.outcome = model.capture_output(holder.output,
                                               redactor=transport.redact_body)
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
    """The handle a record() or replay() block yields.

    `output` is the one thing Orientim cannot see for itself. Everything else in
    a recording is observed at the HTTP boundary, but the value an agent returns
    never crosses that boundary — record() is a context manager, so the return
    value goes to the caller's own variable and nothing passes through us.

    There is no way to capture it implicitly, and the ways to fake one (walking
    frames, re-invoking the callable) fail in exactly the situations where a
    person most needs to trust the recording. So it is declared:

        with orientim.record() as run:
            run.output = my_agent(question)

    Left unset it stays None, which is recorded as "not declared" rather than
    as "returned nothing" — a distinction a report has to be able to make.
    """

    def __init__(self, rec):
        self.rec = rec
        self._clients = []
        self.path = None
        self.output = None

    def client(self, **kw):
        """A plain client, captured by the transport patch and closed with the block.

        An agent that calls this in a loop used to leave one connection pool
        per call open until the garbage collector felt like it.
        """
        c = httpx.Client(timeout=10.0, **kw)
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
                 unseen=(), unseen_n=0, incomplete=False, patched=(),
                 failure=None, recurred=None, output_changed=False,
                 recorded_output=None, replay_output=None, runtime_changed=(),
                 replay_steps=()):
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
        self.failure = failure          # what this recording was kept for
        self.recurred = recurred        # did that failure happen again
        # The execution model. output_changed is False both when the answer is
        # the same and when there is nothing to compare — a recording made
        # before output was declared, or one where it never was. Those are told
        # apart by recorded_output being None, not by a third truth value that
        # every caller would have to remember to handle.
        self.output_changed = output_changed
        self.recorded_output = recorded_output
        self.replay_output = replay_output
        # Informational, never part of ok: we cannot replay a library version,
        # so reporting one as a failure would be a verdict nobody can act on.
        # It belongs in the message of a divergence that has another cause.
        self.runtime_changed = list(runtime_changed)
        # What the replay itself produced, so an evaluator can be pointed at
        # what the code did *this time* rather than at the recording. Held by
        # reference, not copied: these steps already exist for the duration of
        # the replay, and copying a run of them per divergence would be paid
        # for by every caller, most of whom never look.
        self.replay_steps = list(replay_steps)

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
        if not self.ok and self.failure:
            # The headline is a divergence, but the question that sent
            # someone here was "did I fix it". Answer it anyway.
            lines.append("      %s the recorded failure (%s) %s"
                         % ("!!" if self.recurred else "ok", self.failure,
                            "happened again" if self.recurred
                            else "did not happen again"))
        if not self.ok and self.runtime_changed:
            # Never a verdict of its own — we cannot replay a library version,
            # so failing a build on one would be a result nobody can act on.
            # But when something *did* diverge this is often the whole answer,
            # and leaving the reader to find it themselves is unkind.
            moved = ", ".join("%s %s->%s" % (c["what"], c["was"] or "-",
                                             c["now"] or "-")
                              for c in self.runtime_changed[:4])
            lines.append("      note: the runtime moved since the recording: "
                         + moved)
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


def replay(path, fn, strict=True, on_step=None, realtime=False, patch=None,
           check=None):
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

    check=fn(result) turns a replay from "did anything change" into "is it
    fixed". Pass the same quality check that made you keep the recording; it
    is re-run against what your function returns this time, and the verdict
    becomes FIXED or STILL_BROKEN. Without it, a recording kept because the
    run raised is judged the same way automatically, on the exception it
    ended with.
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
    outcome = None
    with _recorded_env(recorded_env):
        for name, value in recorded_env.items():
            os.environ[name] = value
        with scope.bound(rec, "replay") as region, \
                shims.active("replay", rec, shim_steps, region=region), \
                _patch_httpx({"mode": "replay", "sync": tr,
                              "async": tr_async}, region=region):
            holder = _Holder(rec)
            try:
                outcome = fn(holder)
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

    # A recording is not only a fixture; it is a bug report. The trigger says
    # what went wrong, so a replay can re-check that specific answer instead
    # of only reporting that something moved.
    failure, recurred = None, None
    if check is not None:
        failure = "the quality check"
        try:
            recurred = not bool(check(outcome))
        except Exception:
            recurred = True          # a check that cannot run has not passed
    elif expected_exc:
        failure = expected_exc
        recurred = (raised == expected_exc)

    # What the agent answered this time. The return value of fn is the usual
    # source, but a replay body written to mirror a recording — run.output = ...
    # — sets it on the holder instead, so both spellings work.
    replay_output = model.capture_output(
        holder.output if holder.output is not None else outcome,
        redactor=transport.redact_body)
    recorded_output = meta.get("outcome")
    # None when either side never declared one. Not a difference; an absence.
    output_changed = bool(model.outputs_differ(recorded_output, replay_output))
    runtime_changed = model.runtime_differences(meta.get("runtime"),
                                                model.runtime_info())

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

    reproduced = (idx is None and not rec.uncaptured and not unexpected_raise
                  and not dropped and not no_steps and not stale and not unseen
                  and not incomplete)
    # A declared answer that changed is not a faithful reproduction, whatever
    # the chain says. This can only fire for a recording that declared an
    # output, so no recording made before format 4 changes verdict because of
    # it — which is the whole reason it is safe to let it decide `ok`.
    clean = reproduced and not output_changed
    if clean:
        return Divergence(True, None, None, root_a, root_b, [], rec.blocked,
                          nk, nr, npm, dropped,
                          failure=failure, recurred=recurred,
                          recorded_output=recorded_output,
                          replay_output=replay_output,
                          runtime_changed=runtime_changed,
                          replay_steps=replayed)

    reason = (rec.uncaptured[0]["detail"] if rec.uncaptured
              else (f"replay raised {unexpected_raise}" if unexpected_raise
                    else unseen[0]["detail"] if unseen
                    else "the agent returned a different answer" if reproduced
                    else "chain hash differs"))
    if idx is None and not reproduced:
        idx = incomplete[0].get("i", 0) if incomplete else len(replayed)
    # When every step reproduced and only the answer moved, there is no step to
    # point at, and inventing one would send the reader to a step that is fine.
    # index stays None, which is what diagnose reads to say OUTPUT_CHANGED.
    return Divergence(False, idx, reason, root_a, root_b, rec.uncaptured,
                      rec.blocked, nk, nr, npm, dropped, raised=unexpected_raise,
                      no_steps=no_steps, stale=stale, headers_changed=hdr_changed,
                      unseen=unseen, unseen_n=meta.get("unseen_n", 0),
                      incomplete=bool(incomplete),
                      failure=failure, recurred=recurred,
                      output_changed=output_changed,
                      recorded_output=recorded_output,
                      replay_output=replay_output,
                      runtime_changed=runtime_changed,
                      replay_steps=replayed)
