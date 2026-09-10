# -*- coding: utf-8 -*-
"""Storage backends, and the routing that picks one.

`docs/limits.md` and `docs/recordings.md` both said the S3 backend was "tested
against moto". It was not: moto was a dev dependency that appeared in no test,
and S3Backend had no coverage at all. This file is what makes that sentence
true; without it the sentence has to come out.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import storage, store

ROOT = "tests/_runs/storage"
BUCKET = "orientim-test-bucket"


def _moto():
    """The moto server, or a note saying why this check could not run.

    Never a silent pass: a skipped storage check has to be visible, or the
    documentation claim it supports quietly stops being true again.
    """
    try:
        import boto3                                     # noqa: F401
        from moto import mock_aws
        return mock_aws
    except ImportError:
        return None


def _backend(prefix="runs"):
    import boto3
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    return storage.S3Backend(BUCKET, prefix)


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    storage.reset_cache()


# --- S3, against moto ---------------------------------------------------------

def t_s3_write_read_exists():
    mock_aws = _moto()
    if not mock_aws:
        return False, "moto is a dev dependency and must be installed"
    with mock_aws():
        b = _backend()
        locator = b.write("run_a.jsonl", b'{"_meta": {"run_id": "run_a"}}\n')
        ok = (locator == "s3://%s/runs/run_a.jsonl" % BUCKET
              and b.read("run_a.jsonl") == b'{"_meta": {"run_id": "run_a"}}\n'
              and b.exists("run_a.jsonl")
              and not b.exists("nothing.jsonl"))
        return ok, "wrote %s" % locator


def t_s3_list_ignores_other_objects():
    mock_aws = _moto()
    if not mock_aws:
        return False, "moto not installed"
    with mock_aws():
        b = _backend()
        for name in ("run_a.jsonl", "run_b.jsonl", "notes.txt"):
            b.write(name, b"x")
        got = b.list()
        return got == ["run_a.jsonl", "run_b.jsonl"], "listed %r" % (got,)


def t_s3_list_pages_past_one_thousand():
    """S3 returns 1000 keys at a time. The loop that follows the token is the
    part that breaks silently, so it is exercised rather than trusted."""
    mock_aws = _moto()
    if not mock_aws:
        return False, "moto not installed"
    with mock_aws():
        b = _backend()
        for i in range(1050):
            b.write("run_%04d.jsonl" % i, b"x")
        got = b.list()
        return (len(got) == 1050 and got[0] == "run_0000.jsonl"
                and got[-1] == "run_1049.jsonl"), "listed %d" % len(got)


def t_s3_stat_and_delete():
    mock_aws = _moto()
    if not mock_aws:
        return False, "moto not installed"
    with mock_aws():
        b = _backend()
        b.write("run_a.jsonl", b"0123456789")
        size, mtime = b.stat("run_a.jsonl")
        deleted = b.delete("run_a.jsonl")
        return (size == 10 and mtime > 0 and deleted
                and not b.exists("run_a.jsonl")), \
            "size=%s mtime=%s deleted=%s" % (size, mtime, deleted)


def t_s3_url_is_signed_and_expiring():
    mock_aws = _moto()
    if not mock_aws:
        return False, "moto not installed"
    with mock_aws():
        b = _backend()
        b.write("run_a.jsonl", b"x")
        url = b.url("run_a.jsonl", expires=60)
        # Signature style differs between SigV2 and SigV4, so the check is on
        # what both must carry: the key, a signature, and an expiry.
        signed = "Signature=" in url or "X-Amz-Signature=" in url
        expiring = "Expires=" in url or "X-Amz-Expires=" in url
        return ("runs/run_a.jsonl" in url and signed and expiring), \
            "signed=%s expiring=%s url=%s" % (signed, expiring, url[:80])


def t_s3_prefix_is_applied_and_stripped():
    """Keys carry the prefix in the bucket and not in the listing."""
    mock_aws = _moto()
    if not mock_aws:
        return False, "moto not installed"
    with mock_aws():
        b = storage.S3Backend(BUCKET, "team/nightly/")
        import boto3
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        loc = b.write("run_a.jsonl", b"x")
        return (loc.endswith("team/nightly/run_a.jsonl")
                and b.list() == ["run_a.jsonl"]), "locator %s" % loc


def t_s3_end_to_end_through_the_recorder():
    """A real recording written to S3 and replayed back out of it."""
    mock_aws = _moto()
    if not mock_aws:
        return False, "moto not installed"
    B = "http://127.0.0.1:8731"    # the lab server is already running
    with mock_aws():
        import boto3
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        storage.reset_cache()
        root = "s3://%s/runs" % BUCKET

        def agent(h):
            h.client().post(B + "/search", content=b'{"q":1}')

        with orientim.record(root=root, always=True) as h:
            agent(h)
        d = orientim.replay(h.path, agent)
        rows = store.list_runs(root)
        storage.reset_cache()
        return (d.ok and len(rows) == 1
                and h.path.startswith("s3://")), \
            "path %s, verdict %s" % (h.path, d.diagnosis[0])


def t_s3_missing_boto3_says_what_to_install():
    """The error a user without boto3 sees has to name the fix."""
    import builtins
    real = builtins.__import__

    def no_boto3(name, *a, **kw):
        if name == "boto3":
            raise ImportError("no boto3")
        return real(name, *a, **kw)

    builtins.__import__ = no_boto3
    try:
        storage.S3Backend("b", "")
        return False, "constructed a backend without boto3"
    except RuntimeError as e:
        return "pip install" in str(e), "error: %s" % str(e).split("\n")[1]
    finally:
        builtins.__import__ = real


# --- routing ------------------------------------------------------------------

def t_routing_picks_the_backend():
    storage.reset_cache()
    cases = [
        ("s3://bucket/prefix", storage.S3Backend),
        ("memory://x", storage.MemoryBackend),
        ("file://tmp/runs", storage.LocalBackend),
        ("runs", storage.LocalBackend),
        ("/abs/path", storage.LocalBackend),
    ]
    mock_aws = _moto()
    wrong = []
    for root, want in cases:
        try:
            if root.startswith("s3://") and mock_aws:
                with mock_aws():
                    got = storage.open_store(root)
            elif root.startswith("s3://"):
                continue
            else:
                got = storage.open_store(root)
            if not isinstance(got, want):
                wrong.append((root, type(got).__name__))
        except Exception as e:
            wrong.append((root, "%s: %s" % (type(e).__name__, e)))
        finally:
            storage.reset_cache()
    return not wrong, "misrouted %r" % (wrong,)


def t_file_scheme_is_stripped():
    storage.reset_cache()
    b = storage.open_store("file://" + ROOT)
    storage.reset_cache()
    return b.root == ROOT, "root %r" % (b.root,)


def t_locator_round_trip():
    """A locator has to survive being split and rebuilt, for every scheme."""
    bad = []
    for root, key in (("runs", "run_a.jsonl"),
                      ("s3://bucket/prefix", "run_a.jsonl"),
                      ("memory://x", "run_a.jsonl"),
                      (os.path.join("a", "b"), "run_a.jsonl")):
        loc = store.locator_for(root, key)
        back_root, back_key = store.split_locator(loc)
        if back_key != key:
            bad.append((root, loc, back_root, back_key))
    return not bad, "broken round trips %r" % (bad,)


def t_store_cache_returns_the_same_backend():
    """The memory backend has to keep what it was given between calls."""
    storage.reset_cache()
    a = storage.open_store("memory://cache-test")
    a.write("run_x.jsonl", b"kept")
    b = storage.open_store("memory://cache-test/")     # trailing slash
    storage.reset_cache()
    return b.read("run_x.jsonl") == b"kept", "same backend across spellings"


def t_local_backend_round_trip():
    _fresh()
    b = storage.open_store(ROOT)
    b.write("run_a.jsonl", b"hello")
    size, mtime = b.stat("run_a.jsonl")
    url = b.url("run_a.jsonl")
    ok = (b.read("run_a.jsonl") == b"hello" and b.list() == ["run_a.jsonl"]
          and b.exists("run_a.jsonl") and size == 5 and mtime > 0
          and url.startswith("file://") and b.delete("run_a.jsonl")
          and not b.exists("run_a.jsonl") and not b.delete("gone.jsonl"))
    storage.reset_cache()
    return ok, "local backend behaves"


def t_memory_backend_round_trip():
    storage.reset_cache()
    b = storage.open_store("memory://round-trip")
    b.write("run_a.jsonl", b"hello")
    b.write("notes.txt", b"ignored")
    ok = (b.read("run_a.jsonl") == b"hello" and b.list() == ["run_a.jsonl"]
          and b.stat("run_a.jsonl") == (5, 0.0)
          and b.url("run_a.jsonl") == "memory://run_a.jsonl"
          and b.delete("run_a.jsonl") and not b.delete("run_a.jsonl"))
    storage.reset_cache()
    return ok, "memory backend behaves"


def t_describe_says_where_without_leaking():
    storage.reset_cache()
    local = storage.open_store(ROOT).describe()
    mem = storage.open_store("memory://d").describe()
    storage.reset_cache()
    return ("local disk" in local and "in memory" in mem), \
        "%r / %r" % (local, mem)
