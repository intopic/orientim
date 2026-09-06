---
name: Bug report
about: A replay behaves in a way the docs do not describe
title: ''
labels: bug
assignees: ''
---

**What happened**
A clear description of the behaviour, and what you expected instead.

**The verdict you got**
The verdict code from the report (e.g. `UNCAPTURED_SOURCE`, `HEADERS_CHANGED`),
and the step it pointed at.

**Minimal reproduction**
The smallest agent function and recording that shows it. A recording is
line-delimited JSON — you can attach one, but read `docs/recordings.md` first
and make sure it holds no secrets.

```python
# your agent + how you record and replay it
```

**Your machine**

```
# paste the output of:
orientim conformance
```

- OS:
- Python version:
- httpx version:
- Orientim version:

**Is this a declared limit?**
Check `docs/limits.md`. A divergence on your machine that is *not* one of the
declared limits is the most useful bug there is — see `CONTRIBUTING.md`.
