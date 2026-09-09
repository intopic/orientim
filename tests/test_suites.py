# -*- coding: utf-8 -*-
"""pytest entry points for the two suites.

One test per check, so a failure names the check rather than the file.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
import test_audit
from orientim import conformance

CHECKS = [
    ("ring buffer vs hash chain", test_audit.t_ring),
    ("step index under concurrency", test_audit.t_index_race),
    ("viewer with non-local locator", test_audit.t_viewer_s3),
    ("secrets in the recording", test_audit.t_secrets),
    ("nested record()", test_audit.t_nested),
    ("corrupt recording", test_audit.t_corrupt),
    ("missing entry point", test_audit.t_bad_entry),
    ("python 3.9 floor", test_audit.t_py_floor),
    ("foreign client", test_audit.t_foreign_client),
    ("async agents", test_audit.t_async),
    ("environment not snapshotted", test_audit.t_env_not_captured),
    ("named env var replays", test_audit.t_env_opt_in),
    ("nothing captured", test_audit.t_nothing_captured),
    ("uncaptured library noticed", test_audit.t_unseen_library),
    ("replay that raises", test_audit.t_replay_raises),
    ("recorded exception replays", test_audit.t_recorded_exception_replays),
    ("fixed vs still broken", test_audit.t_fixed_and_still_broken),
    ("fixed via a quality check", test_audit.t_fixed_via_check),
    ("new call is not an uncaptured source", test_audit.t_new_call),
    ("header-only difference", test_audit.t_header_blind_match),
    ("overlapping record()", test_audit.t_patch_leak),
    ("exception building a client leaves httpx clean", test_audit.t_init_exception),
    ("viewer injection", test_audit.t_viewer_injection),
    ("form and response credentials", test_audit.t_oauth_secrets),
    ("secret in a response header", test_audit.t_resp_header_secret),
    ("credential in a URL inside a JSON value", test_audit.t_body_url_userinfo),
    ("secret-shaped env var refused", test_audit.t_env_secret_blocked),
    ("binary round-trip", test_audit.t_binary_roundtrip),
    ("streaming not buffered", test_audit.t_streaming_passthrough),
    ("realtime replay", test_audit.t_streaming_realtime),
    ("live server csrf", test_audit.t_server_csrf),
    ("always saves untriggered run", test_audit.t_always),
    ("on_capture hook", test_audit.t_on_capture),
    ("random state only when used", test_audit.t_random_state_conditional),
    ("counterfactual branch", test_audit.t_counterfactual),
    ("counterfactual never ok", test_audit.t_counterfactual_never_ok),
    ("prune by signature", test_audit.t_prune_by_signature),
    ("diff two recordings", test_audit.t_diff),
    ("trace link", test_audit.t_trace_link),
    ("holder closes clients", test_audit.t_holder_closes_clients),
    ("opt-in retention cap", test_audit.t_auto_cap),
    ("async server isolation", test_audit.t_async_server_isolation),
    ("concurrent record() no cross-contamination", test_audit.t_concurrent_no_crosstalk),
    ("orientim ci", test_audit.t_ci_command),
]


@pytest.mark.parametrize("name,fn", CHECKS, ids=[c[0] for c in CHECKS])
def test_audit_check(name, fn):
    ok, note = fn()
    assert ok, note


REAL_SDK = [
    ("openai traffic captured", "t_openai_sdk_captured"),
    ("openai agent replays", "t_openai_sdk_replays"),
    ("openai streaming", "t_openai_streaming"),
    ("anthropic captured", "t_anthropic_sdk"),
    ("counterfactual on real sdk", "t_counterfactual_on_real_sdk"),
]


@pytest.mark.parametrize("name,attr", REAL_SDK, ids=[c[0] for c in REAL_SDK])
def test_real_sdk(name, attr, real_sdk_server):
    ok, note = getattr(real_sdk_server, attr)()
    assert ok, note


@pytest.mark.parametrize("strict", [False, True], ids=["loose", "strict"])
def test_conformance_has_no_undeclared_failures(strict):
    rep = conformance.run(quiet=True, strict=strict)
    bad = ["%s %s (%s)" % (r["n"], r["name"], r["detail"])
           for r in rep["undeclared_failures"]]
    assert not bad, "undeclared failures: " + "; ".join(bad)
    # The published numbers, asserted rather than described.
    assert rep["captured"] == (15 if strict else 16), rep["captured"]
