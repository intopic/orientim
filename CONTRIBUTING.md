# Contributing

## The most useful contribution is source twenty-one

If a replay diverges on your machine for a reason that is not one of the
declared limits in [docs/limits.md](docs/limits.md), that is a gap in the
taxonomy, not just a bug in the code. Open an issue with the shape of it.

A good report is a probe: the smallest code that makes the source fire, and what
changes about the world between the recording and the replay. If you can write
it as a function in the style of
[`orientim/patterns.py`](orientim/patterns.py), that is the whole fix.

The entry that gets added helps everyone who installs this afterwards, which is
more than the fix does.

## Running it

```bash
pip install -e ".[dev]"

pytest tests/                                 # everything
python tests/test_audit.py                    # the audit suite, readable output
python -m orientim.cli conformance            # loose key, this machine
python -m orientim.cli conformance --strict   # identical bytes
```

Both suites run against a real HTTP server over a real socket. There are no
mocks in this repository on purpose: a recorder tested against a mock is tested
against the wrong thing.

## The one rule about tests

**A test that cannot fail is worse than no test.** Before adding one, break the
thing it covers on purpose and watch it go red. If it stays green, it is
measuring nothing.

This is not a general principle borrowed from somewhere. It is what the
pre-release audit found: ten green checks were hiding eight defects, two of the
twenty conformance probes could not fail under any circumstances, and one of the
hidden defects was writing API keys into every recording. The relevant history is
in [CHANGELOG.md](CHANGELOG.md).

So:

- A probe without a **mutation** — something that changes between the recording
  and the replay — tests the plumbing, not the source.
- A check that asserts "did not crash" should usually assert what the correct
  behaviour *is*. `nested record()` passed for weeks by asserting it did not
  crash, while it silently switched the outer recording's shims off.
- Assert the number, not the description. The published coverage figures are
  asserted in [`tests/test_suites.py`](tests/test_suites.py), so a coverage
  change breaks CI instead of quietly making the README wrong.

## Adding a source to the taxonomy

1. A probe in `orientim/patterns.py` that triggers the source through a real
   socket.
2. A mutation, if the source depends on the world changing.
3. A route in `orientim/_probe.py` if you need server behaviour that does not
   exist yet.
4. A declared expectation — `CAPTURED`, `LOOSE`, or `LIMIT` — decided *before*
   you run it. A source may not be reclassified because it failed.
5. An entry in [docs/nondeterminism.md](docs/nondeterminism.md).
6. Updated numbers in `tests/test_suites.py` and the README.

## Style

Match what is there. Concretely:

- **Comments say why, never what.** The code says what it does. A comment earns
  its place by recording a decision, a trade-off, or a bug that came back.
- **English in the source and in everything a user sees.** The commit history
  and issues can be any language.
- Standard library first; `httpx` is the only runtime dependency and it should
  stay that way. `boto3` is optional and imported lazily.
- Python 3.9 floor. No `match`, no `X | Y` annotations, no parenthesised context
  managers. There is a test for this.
- Lines under 88 characters.

## Changing the recording format

Bump `store.FORMAT` whenever the *meaning* of a stored field changes, not just
when a field is added. A file written under an older meaning is refused with
`STALE_FORMAT` rather than judged by a rule that did not exist when it was
written — which is the only way `IDENTICAL` keeps meaning one thing.

## Before opening a pull request

```bash
pytest tests/ -q
python -m orientim.cli conformance --strict
```

Both must be clean, and `conformance` exits non-zero if anything claimed as
captured failed. If your change moves a coverage number, say so in the PR and
update the README, `docs/nondeterminism.md` and the assertion in
`tests/test_suites.py` in the same commit. A number that lives in three places
and is changed in one is how "17 of 20" survived being wrong.

## What is out of scope

- Capturing libraries other than `httpx`. It has been considered; the honest
  answer today is detection, not capture — see
  [docs/limits.md](docs/limits.md). If you want to argue for it, open an issue
  first, because it doubles the surface that has to stay correct.
- A hosted service, an account system, telemetry, or a phone-home update check.
  "Nothing leaves your machine" is a promise, not a default.
- Dashboards and aggregation. That is the observability category and it is well
  served already.
