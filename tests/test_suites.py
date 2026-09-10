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
import test_execution
import test_cases
import test_cli
import test_contract
import test_redaction
import test_server
import test_stability
import test_storage
import test_diff
import test_evaluate
import test_tools
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
    ("httpx2 instrumented", test_audit.t_httpx2_captured),
    ("requests captured", test_audit.t_requests_captured),
    ("requests streaming", test_audit.t_requests_streaming),
    ("httpx and requests one order", test_audit.t_mixed_libraries),
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

# Execution model v2. Registered here rather than left in the file, because a
# check nothing runs is a check that does not exist — the lesson of every one
# of these that was added and forgotten.
EXECUTION = [
    ("typed steps: model vs tool", test_execution.t_step_roles),
    ("classification cannot decide a match",
     test_execution.t_classification_does_not_touch_matching),
    ("model metadata recorded", test_execution.t_model_metadata),
    ("response metadata recorded", test_execution.t_response_metadata),
    ("streamed response metadata", test_execution.t_streamed_response_metadata),
    ("metadata survives a hostile body",
     test_execution.t_metadata_survives_a_hostile_body),
    ("final output recorded and reproduced",
     test_execution.t_output_recorded_and_reproduced),
    ("same calls, different answer",
     test_execution.t_output_changed_is_its_own_verdict),
    ("undeclared output changes no verdict",
     test_execution.t_undeclared_output_changes_nothing),
    ("truncated output still compares honestly",
     test_execution.t_output_truncation_keeps_the_digest_honest),
    ("output is redacted", test_execution.t_output_is_redacted),
    ("unserialisable output is marked",
     test_execution.t_unserialisable_output_is_marked_not_dropped),
    ("agent metadata", test_execution.t_agent_metadata),
    ("runtime recorded", test_execution.t_runtime_recorded),
    ("runtime drift reported not enforced",
     test_execution.t_runtime_difference_is_reported_not_enforced),
    ("migration preserves the chain",
     test_execution.t_migration_preserves_chain),
    ("format 3 recording still replays",
     test_execution.t_v3_recording_still_replays),
    ("migration enriches old recordings",
     test_execution.t_migration_enriches_old_recordings),
    ("formats below 3 stay stale",
     test_execution.t_pre_migration_formats_stay_stale),
    ("migration never raises", test_execution.t_migration_never_raises),
    ("reading metadata skips enrichment",
     test_execution.t_reading_metadata_skips_enrichment),
    ("a GET to a provider is a tool call",
     test_execution.t_classification_ignores_non_post),
    ("a repr output does not drift",
     test_execution.t_repr_output_does_not_drift),
]

# Tool calls: what the model asked the agent to do.
TOOLS = [
    ("openai tool_calls", test_tools.t_openai_tool_calls),
    ("anthropic tool_use", test_tools.t_anthropic_tool_use),
    ("multiple tool calls in one response", test_tools.t_multiple_tool_calls),
    ("a response with no tool calls", test_tools.t_no_tool_calls),
    ("an unknown shape invents nothing",
     test_tools.t_unknown_shape_invents_nothing),
    ("unparseable arguments kept as text",
     test_tools.t_unparseable_arguments_are_kept_as_text),
    ("empty arguments are an empty object", test_tools.t_empty_arguments),
    ("a call links to the model step that asked",
     test_tools.t_call_links_to_the_model_step),
    ("streamed openai tool calls", test_tools.t_streamed_openai_tool_calls),
    ("streamed anthropic tool use", test_tools.t_streamed_anthropic_tool_use),
    ("a cut-off stream is marked partial",
     test_tools.t_cut_off_stream_is_marked_partial),
    ("credentials inside tool arguments are redacted",
     test_tools.t_credentials_inside_arguments_are_redacted),
    ("tool calls do not touch the chain",
     test_tools.t_tool_calls_do_not_touch_the_chain),
    ("extraction survives hostile shapes",
     test_tools.t_extraction_survives_hostile_shapes),
    ("runaway tool calls are capped",
     test_tools.t_runaway_tool_calls_are_capped),
]

# Evaluators: asking a recorded execution a question.
EVALUATE = [
    ("output_equals, same and different",
     test_evaluate.t_output_equals_same_and_different),
    ("output_equals with nothing declared",
     test_evaluate.t_output_equals_without_a_declared_output),
    ("output_matches", test_evaluate.t_output_matches),
    ("output_matches on a truncated answer warns",
     test_evaluate.t_output_matches_truncated_is_a_warning),
    ("used_tool, present and missing",
     test_evaluate.t_used_tool_present_and_missing),
    ("used_tool evidence carries the arguments",
     test_evaluate.t_used_tool_evidence_answers_the_question),
    ("used_tool when nothing was asked for",
     test_evaluate.t_used_tool_when_nothing_asked),
    ("did_not_call a forbidden tool",
     test_evaluate.t_did_not_call_forbidden_tool),
    ("max_steps catches a loop", test_evaluate.t_max_steps_catches_a_loop),
    ("max_steps is honest about a truncated ring",
     test_evaluate.t_max_steps_is_honest_about_a_truncated_ring),
    ("no_step_failed", test_evaluate.t_no_step_failed),
    ("a call that raised counts as failed",
     test_evaluate.t_failed_step_from_an_exception),
    ("custom evaluator return forms",
     test_evaluate.t_custom_evaluator_forms),
    ("a custom evaluator that raises fails itself",
     test_evaluate.t_custom_evaluator_that_raises_fails_itself),
    ("a custom evaluator reads tool arguments",
     test_evaluate.t_custom_evaluator_reads_tool_arguments),
    ("evaluators combine into one report",
     test_evaluate.t_evaluators_combine_into_one_report),
    ("a report is machine and human readable",
     test_evaluate.t_report_is_machine_readable_and_human_readable),
    ("a warning does not fail a report",
     test_evaluate.t_a_warning_does_not_fail_a_report),
    ("evaluate accepts a path", test_evaluate.t_evaluate_accepts_a_path),
    ("evaluation reads a migrated recording",
     test_evaluate.t_evaluation_reads_a_migrated_recording),
]

# Saved cases, baselines, and `orientim test`.
SUITE = [
    ("a case is saved and loaded", test_cases.t_case_save_and_load),
    ("a case is validated before it is written",
     test_cases.t_case_save_validates_before_writing),
    ("a case name is checked, not trusted", test_cases.t_case_name_is_checked),
    ("case list and delete", test_cases.t_case_list_and_delete),
    ("a broken case is reported, not skipped",
     test_cases.t_broken_case_is_reported_not_skipped),
    ("a case passes when nothing moved",
     test_cases.t_case_run_passes_when_nothing_moved),
    ("a case fails on evaluation alone",
     test_cases.t_case_fails_on_evaluation_alone),
    ("a case fails on replay alone", test_cases.t_case_fails_on_replay_alone),
    ("a case evaluates the replay, not the recording",
     test_cases.t_case_evaluates_the_replay_not_the_recording),
    ("a case that cannot run is a failure, not a crash",
     test_cases.t_case_run_error_is_a_failure_not_a_crash),
    ("warnings do not fail a case", test_cases.t_warnings_do_not_fail_a_case),
    ("a baseline is a stored object", test_cases.t_baseline_is_a_stored_object),
    ("a baseline keeps no evidence", test_cases.t_baseline_keeps_no_evidence),
    ("baseline compare names what moved",
     test_cases.t_baseline_compare_names_what_moved),
    ("baseline separates new from pre-existing failures",
     test_cases.t_baseline_separates_new_from_pre_existing),
    ("baselines reuse ci.compare", test_cases.t_baseline_reuses_ci_compare),
    ("ci.compare still keys on run_id",
     test_cases.t_ci_compare_still_keys_on_run_id),
    ("a baseline accepts a report path",
     test_cases.t_baseline_accepts_a_report_path),
    ("the test report is machine readable",
     test_cases.t_report_is_machine_readable),
    ("the report can leave evidence out",
     test_cases.t_report_can_leave_evidence_out),
    ("the summary shows reason and failing evaluators",
     test_cases.t_summary_shows_reason_and_failing_evaluators),
    ("orientim case save/list/delete", test_cases.t_cli_case_commands),
    ("orientim case save refuses a bad case",
     test_cases.t_cli_case_save_refuses_a_bad_case),
    ("orientim case run and run-all", test_cases.t_cli_case_run_and_run_all),
    ("orientim test exit codes", test_cases.t_cli_test_exit_codes),
    ("orientim test writes a report", test_cases.t_cli_test_writes_a_report),
    ("orientim test --case runs one", test_cases.t_cli_test_single_case),
    ("orientim test --baseline fails only on new breakage",
     test_cases.t_cli_test_only_fails_on_what_this_change_broke),
    ("orientim baseline create/list/compare/delete",
     test_cases.t_cli_baseline_commands),
    ("orientim baseline compare fails on a new failure",
     test_cases.t_cli_baseline_compare_fails_on_a_new_failure),
    ("the definition of done, end to end",
     test_cases.t_the_definition_of_done),
]

# The explanatory diff.
DIFF = [
    ('an unchanged execution', test_diff.t_unchanged_execution),
    ('an insertion does not smear', test_diff.t_insertion_does_not_smear),
    ('a deletion', test_diff.t_deletion),
    ('a reorder is one change, not four', test_diff.t_reorder_is_one_change_not_four),
    ('a changed step is not a delete and an insert', test_diff.t_changed_step_is_not_a_delete_and_insert),
    ('empty sides', test_diff.t_empty_sides),
    ('model config change is named by field', test_diff.t_model_config_change_is_named_by_field),
    ('an unanswered call reports no response change', test_diff.t_unanswered_call_reports_no_response_change),
    ('a tool added and removed', test_diff.t_tool_added_and_removed),
    ('a tool argument change', test_diff.t_tool_argument_change),
    ('multiple tool calls in one response', test_diff.t_multiple_tool_calls_in_one_response),
    ('a tool reorder is one change', test_diff.t_tool_reorder_is_one_change),
    ('partial arguments are flagged', test_diff.t_partial_arguments_are_flagged_not_compared_away),
    ('json body field diff', test_diff.t_json_body_field_diff),
    ('nested and list paths', test_diff.t_nested_and_list_paths),
    ('text body diff', test_diff.t_text_body_diff),
    ('identical bodies have no diff', test_diff.t_identical_bodies_have_no_diff),
    ('the diff shows only redacted values', test_diff.t_diff_shows_only_redacted_values),
    ('output change reported with digests', test_diff.t_output_change_is_reported_with_digests),
    ('output unchanged', test_diff.t_output_unchanged),
    ('output truncation makes no false claim', test_diff.t_output_truncation_makes_no_false_claim),
    ('output declared on one side only', test_diff.t_output_declared_on_one_side_only_is_unknown),
    ('structured output difference', test_diff.t_output_structured_difference),
    ('consequence never claims a cause', test_diff.t_consequence_never_claims_a_cause),
    ('consequence marks an established link', test_diff.t_consequence_marks_a_link_the_records_establish),
    ('unknown provenance stays unknown', test_diff.t_unknown_provenance_stays_unknown),
    ('replay, evaluation and diff together', test_diff.t_replay_evaluation_and_diff_together),
    ('unmatched requests are not deletions', test_diff.t_unmatched_requests_are_not_reported_as_deletions),
    ('a case that did not move diffs as unchanged', test_diff.t_a_case_that_did_not_move_diffs_as_unchanged),
    ('the diff json is stable and complete', test_diff.t_json_output_is_stable_and_complete),
    ('alignment is bounded at 10, 100 and 1000 steps', test_diff.t_alignment_is_bounded),
    ('degradation is announced', test_diff.t_degradation_is_announced),
    ('small runs are never degraded', test_diff.t_small_runs_are_never_degraded),
]

# Trust and release hardening: every advertised surface, proven.
STORAGE = [
    ('describe says where without leaking', test_storage.t_describe_says_where_without_leaking),
    ('file scheme is stripped', test_storage.t_file_scheme_is_stripped),
    ('local backend round trip', test_storage.t_local_backend_round_trip),
    ('locator round trip', test_storage.t_locator_round_trip),
    ('memory backend round trip', test_storage.t_memory_backend_round_trip),
    ('routing picks the backend', test_storage.t_routing_picks_the_backend),
    ('s3 end to end through the recorder', test_storage.t_s3_end_to_end_through_the_recorder),
    ('s3 list ignores other objects', test_storage.t_s3_list_ignores_other_objects),
    ('s3 list pages past one thousand', test_storage.t_s3_list_pages_past_one_thousand),
    ('s3 missing boto3 says what to install', test_storage.t_s3_missing_boto3_says_what_to_install),
    ('s3 prefix is applied and stripped', test_storage.t_s3_prefix_is_applied_and_stripped),
    ('s3 stat and delete', test_storage.t_s3_stat_and_delete),
    ('s3 url is signed and expiring', test_storage.t_s3_url_is_signed_and_expiring),
    ('s3 write read exists', test_storage.t_s3_write_read_exists),
    ('store cache returns the same backend', test_storage.t_store_cache_returns_the_same_backend),
]


STABILITY = [
    ('a failing run is counted not hidden', test_stability.t_a_failing_run_is_counted_not_hidden),
    ('a lone extreme outlier can hide inside its own limits', test_stability.t_a_lone_extreme_outlier_can_hide_inside_its_own_limits),
    ('a record failure does not double count', test_stability.t_a_record_failure_does_not_double_count),
    ('a single run reports without limits', test_stability.t_a_single_run_reports_without_limits),
    ('branching agent shows the branches', test_stability.t_branching_agent_shows_the_branches),
    ('control limits follow the individuals chart', test_stability.t_control_limits_follow_the_individuals_chart),
    ('control limits need two points', test_stability.t_control_limits_need_two_points),
    ('control limits never go below zero', test_stability.t_control_limits_never_go_below_zero),
    ('control limits of a constant series are the mean', test_stability.t_control_limits_of_a_constant_series_are_the_mean),
    ('deterministic agent is one path', test_stability.t_deterministic_agent_is_one_path),
    ('every result points at its recording', test_stability.t_every_result_points_at_its_recording),
    ('loose outcomes ignore numbers', test_stability.t_loose_outcomes_ignore_numbers),
    ('nothing to measure does not crash', test_stability.t_nothing_to_measure_does_not_crash),
    ('out of control points are identified', test_stability.t_out_of_control_points_are_identified),
    ('report says what it measured', test_stability.t_report_says_what_it_measured),
    ('step counts and paths agree', test_stability.t_step_counts_and_paths_agree),
]


SERVER = [
    ('a malformed body is a clean error', test_server.t_a_malformed_body_is_a_clean_error),
    ('authorized replay runs and reports', test_server.t_authorized_replay_runs_and_reports),
    ('clean shutdown', test_server.t_clean_shutdown),
    ('progress is also gated', test_server.t_progress_is_also_gated),
    ('replay from another origin is refused', test_server.t_replay_from_another_origin_is_refused),
    ('replay with a wrong token is refused', test_server.t_replay_with_a_wrong_token_is_refused),
    ('replay without a token is refused', test_server.t_replay_without_a_token_is_refused),
    ('server starts on an ephemeral port', test_server.t_server_starts_on_an_ephemeral_port),
    ('the page is served', test_server.t_the_page_is_served),
    ('unknown path is not found', test_server.t_unknown_path_is_not_found),
]


CLI = [
    ('ci annotations name the failure', test_cli.t_ci_annotations_name_the_failure),
    ('ci emit is silent outside a workflow', test_cli.t_ci_emit_is_silent_outside_a_workflow),
    ('ci emit writes the github files', test_cli.t_ci_emit_writes_the_github_files),
    ('ci step summary is markdown', test_cli.t_ci_step_summary_is_markdown),
    ('cli ci cannot run', test_cli.t_cli_ci_cannot_run),
    ('cli ci green and red', test_cli.t_cli_ci_green_and_red),
    ('cli ci reports and no fail', test_cli.t_cli_ci_reports_and_no_fail),
    ('cli conformance', test_cli.t_cli_conformance),
    ('cli conformance saves', test_cli.t_cli_conformance_saves),
    ('cli diff json', test_cli.t_cli_diff_json),
    ('cli diff needs two things', test_cli.t_cli_diff_needs_two_things),
    ('cli diff two recordings', test_cli.t_cli_diff_two_recordings),
    ('cli ls', test_cli.t_cli_ls),
    ('cli ls filters', test_cli.t_cli_ls_filters),
    ('cli play', test_cli.t_cli_play),
    ('cli prune dry run removes nothing', test_cli.t_cli_prune_dry_run_removes_nothing),
    ('cli prune keeps the newest', test_cli.t_cli_prune_keeps_the_newest),
    ('cli prune needs a rule', test_cli.t_cli_prune_needs_a_rule),
    ('cli rejects an unknown command', test_cli.t_cli_rejects_an_unknown_command),
    ('cli stability', test_cli.t_cli_stability),
    ('cli view writes a timeline', test_cli.t_cli_view_writes_a_timeline),
    ('every subcommand has help', test_cli.t_every_subcommand_has_help),
]


REDACTION = [
    ('a secret in a url path is a declared limit', test_redaction.t_a_secret_in_a_url_path_is_a_declared_limit),
    ('a secret shaped env name is refused even when asked for', test_redaction.t_a_secret_shaped_env_name_is_refused_even_when_asked_for),
    ('credential in a query parameter', test_redaction.t_credential_in_a_query_parameter),
    ('environment is not captured by default', test_redaction.t_environment_is_not_captured_by_default),
    ('form encoded secret', test_redaction.t_form_encoded_secret),
    ('malformed json body is stored without crashing', test_redaction.t_malformed_json_body_is_stored_without_crashing),
    ('no secret reaches any surface', test_redaction.t_no_secret_reaches_any_surface),
    ('redaction does not break replay', test_redaction.t_redaction_does_not_break_replay),
    ('request headers are never stored', test_redaction.t_request_headers_are_never_stored),
    ('response set cookie is dropped', test_redaction.t_response_set_cookie_is_dropped),
    ('secret in a response body', test_redaction.t_secret_in_a_response_body),
    ('secret in json that arrived as a string', test_redaction.t_secret_in_json_that_arrived_as_a_string),
    ('secret inside a list of objects', test_redaction.t_secret_inside_a_list_of_objects),
    ('secret nested in json', test_redaction.t_secret_nested_in_json),
    ('secret survives a long value', test_redaction.t_secret_survives_a_long_value),
    ('userinfo in a url inside a body', test_redaction.t_userinfo_in_a_url_inside_a_body),
]


CONTRACT = [
    ('baseline feeds orientim test', test_contract.t_baseline_feeds_orientim_test),
    ('case feeds a baseline', test_contract.t_case_feeds_a_baseline),
    ('corpus covers every diff shape', test_contract.t_corpus_covers_every_diff_shape),
    ('corpus evaluation failure shape', test_contract.t_corpus_evaluation_failure_shape),
    ('corpus is deterministic', test_contract.t_corpus_is_deterministic),
    ('corpus unmatched request shape', test_contract.t_corpus_unmatched_request_shape),
    ('evaluation feeds a case', test_contract.t_evaluation_feeds_a_case),
    ('record produces a replayable execution model', test_contract.t_record_produces_a_replayable_execution_model),
    ('replay feeds evaluation', test_contract.t_replay_feeds_evaluation),
    ('test feeds the explanatory diff', test_contract.t_test_feeds_the_explanatory_diff),
    ('the whole chain in one flow', test_contract.t_the_whole_chain_in_one_flow),
]

CHECKS = (CHECKS + EXECUTION + TOOLS + EVALUATE + SUITE + DIFF
          + STORAGE + STABILITY + SERVER + CLI + REDACTION + CONTRACT)


@pytest.mark.parametrize("name,fn", CHECKS, ids=[c[0] for c in CHECKS])
def test_audit_check(name, fn):
    ok, note = fn()
    assert ok, note


REAL_SDK = [
    ("openai traffic captured", "t_openai_sdk_captured"),
    ("typed steps on real sdk traffic", "t_openai_sdk_typed_steps"),
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
