"""A hash chain over the steps of a run.

Divergence detection falls out of the storage format rather than being bolted
on: two runs with the same root hash are identical, and if they differ, the
first differing link is the exact step where behaviour diverged.

The digest is computed at comparison time, from the step as it was observed,
and never carried over from the recording. A replay that copies the recorded
digest can only ever detect a difference in the number of steps — which is what
this used to do.
"""
import hashlib
import json

GENESIS = "0" * 64

# What "the same step" means. The lookup key already covers method, URL and
# request body; hdr_fp covers the request headers the key ignores, which is how
# two calls that differ only by header stop reading as identical.
DIGEST_FIELDS = ("method", "url", "hdr_fp", "status", "body_sha")


def digest(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def step_digest(step, field="key_strict") -> str:
    """The digest of one http step under the active matching mode.

    `field` selects the strict or the loose lookup key, so a run replayed with
    --loose is judged by the same rule that matched it. Judging a loose replay
    by strict bytes would report every whitespace change as a divergence.
    """
    payload = {k: step.get(k) for k in DIGEST_FIELDS}
    payload["key"] = step.get(field)
    payload["error"] = step.get("error", "")
    return digest(payload)


def link(prev: str, step_digest_: str) -> str:
    return hashlib.sha256((prev + step_digest_).encode("utf-8")).hexdigest()


def build(step_digests):
    """Cumulative hashes plus the root."""
    h = GENESIS
    out = []
    for d in step_digests:
        h = link(h, d)
        out.append(h)
    return out, h


# What the agent asked, ignoring what came back. A counterfactual replaces a
# response on purpose, so comparing responses would only rediscover the change
# you made; what you want to know is whether the agent then behaved differently.
REQUEST_FIELDS = ("method", "url", "hdr_fp")


def request_digest(step, field="key_strict") -> str:
    payload = {k: step.get(k) for k in REQUEST_FIELDS}
    payload["key"] = step.get(field)
    return digest(payload)


def build_steps(steps, field="key_strict", requests_only=False):
    fn = request_digest if requests_only else step_digest
    return build([fn(s, field) for s in steps])


def first_divergence(a, b):
    """Index of the first differing step, or None if the chains match."""
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    if len(a) != len(b):
        return min(len(a), len(b))
    return None
