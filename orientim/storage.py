# -*- coding: utf-8 -*-
"""Where recordings live.

One setting decides: the scheme of the store URL picks the backend.

    ./runs                       local disk (default, no dependencies)
    s3://bucket/prefix/          S3, R2, MinIO, B2, Spaces — anything S3-shaped
    memory://                    in-process, for tests

Credentials are never ours and never travel. The S3 backend uses boto3's normal
chain: environment, ~/.aws/credentials, or an instance role. For R2 or MinIO set
ORIENTIM_S3_ENDPOINT. We hold nothing.
"""
import io
import os
import posixpath


class Backend:
    scheme = ""

    def write(self, key, data):
        raise NotImplementedError

    def read(self, key):
        raise NotImplementedError

    def list(self):
        raise NotImplementedError

    def exists(self, key):
        raise NotImplementedError

    def delete(self, key):
        """Remove one recording. Used only by prune(), never automatically."""
        raise NotImplementedError

    def stat(self, key):
        """(size_in_bytes, modified_epoch). Used to decide what to prune."""
        raise NotImplementedError

    def url(self, key, expires=3600):
        """A URL a browser can read directly. Local files return a file:// path."""
        raise NotImplementedError

    def describe(self):
        return self.scheme


# --- local ------------------------------------------------------------------

class LocalBackend(Backend):
    scheme = "file"

    def __init__(self, root):
        self.root = root

    def _p(self, key):
        return os.path.join(self.root, key)

    def write(self, key, data):
        path = self._p(key)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def read(self, key):
        with open(self._p(key), "rb") as f:
            return f.read()

    def list(self):
        if not os.path.isdir(self.root):
            return []
        return sorted(n for n in os.listdir(self.root) if n.endswith(".jsonl"))

    def exists(self, key):
        return os.path.exists(self._p(key))

    def delete(self, key):
        try:
            os.remove(self._p(key))
            return True
        except OSError:
            return False

    def stat(self, key):
        st = os.stat(self._p(key))
        return st.st_size, st.st_mtime

    def url(self, key, expires=3600):
        return "file://" + os.path.abspath(self._p(key)).replace("\\", "/")

    def describe(self):
        return "local disk: %s" % os.path.abspath(self.root)


# --- s3-shaped --------------------------------------------------------------

class S3Backend(Backend):
    scheme = "s3"

    def __init__(self, bucket, prefix=""):
        try:
            import boto3
        except ImportError:
            raise RuntimeError(
                "S3 storage needs boto3.\n"
                "    pip install 'Orientim[s3]'\n"
                "Recordings stay in your bucket; we never see them.")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        endpoint = os.environ.get("ORIENTIM_S3_ENDPOINT")  # R2, MinIO, B2...
        self.s3 = boto3.client("s3", endpoint_url=endpoint or None)

    def _k(self, key):
        return posixpath.join(self.prefix, key) if self.prefix else key

    def write(self, key, data):
        self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data,
                           ContentType="application/x-ndjson")
        return "s3://%s/%s" % (self.bucket, self._k(key))

    def read(self, key):
        obj = self.s3.get_object(Bucket=self.bucket, Key=self._k(key))
        return obj["Body"].read()

    def list(self):
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self._k("")}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            for it in r.get("Contents", []):
                name = posixpath.basename(it["Key"])
                if name.endswith(".jsonl"):
                    out.append(name)
            if not r.get("IsTruncated"):
                break
            token = r.get("NextContinuationToken")
        return sorted(out)

    def exists(self, key):
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except Exception:
            return False

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=self._k(key))
        return True

    def stat(self, key):
        h = self.s3.head_object(Bucket=self.bucket, Key=self._k(key))
        return h["ContentLength"], h["LastModified"].timestamp()

    def url(self, key, expires=3600):
        """Signed, short-lived, read-only. This is what the viewer opens.

        The bytes go browser to bucket. They do not pass through us, and there
        is nothing for us to hold even if we wanted to.
        """
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._k(key)},
            ExpiresIn=expires)

    def describe(self):
        ep = os.environ.get("ORIENTIM_S3_ENDPOINT")
        where = ep or "aws"
        return "your bucket: s3://%s/%s (%s)" % (self.bucket, self.prefix, where)


# --- memory (tests) ---------------------------------------------------------

class MemoryBackend(Backend):
    scheme = "memory"

    def __init__(self):
        self.blobs = {}

    def write(self, key, data):
        self.blobs[key] = data
        return "memory://" + key

    def read(self, key):
        return self.blobs[key]

    def list(self):
        return sorted(k for k in self.blobs if k.endswith(".jsonl"))

    def exists(self, key):
        return key in self.blobs

    def delete(self, key):
        return self.blobs.pop(key, None) is not None

    def stat(self, key):
        return len(self.blobs[key]), 0.0

    def url(self, key, expires=3600):
        return "memory://" + key

    def describe(self):
        return "in memory (%d objects)" % len(self.blobs)


# --- routing ----------------------------------------------------------------

_CACHE = {}


def open_store(root=None):
    """Pick the backend from the store URL. Local disk unless told otherwise.

    Cached per root: a new boto3 client on every call would be pure waste, and
    the memory backend has to keep what it was given.
    """
    root = _norm(root or os.environ.get("ORIENTIM_STORE") or "runs")
    if root in _CACHE:
        return _CACHE[root]
    _CACHE[root] = backend = _build(root)
    return backend


def _norm(root):
    """One spelling per store, so the cache key matches on write and on read."""
    r = root.rstrip("/")
    return r or root


def _build(root):
    if root.startswith("s3://"):
        rest = root[5:]
        bucket, _, prefix = rest.partition("/")
        return S3Backend(bucket, prefix)
    if root.startswith("memory://"):
        return MemoryBackend()
    if root.startswith("file://"):
        root = root[7:]
    return LocalBackend(root)


def reset_cache():
    _CACHE.clear()
