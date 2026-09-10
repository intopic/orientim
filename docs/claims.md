# Every claim, and what backs it

A README is a promise. This is the ledger.

Each row is something the documentation asserts, and what the test suite
actually demonstrates about it. Four verdicts:

| | |
|---|---|
| **PROVEN** | a test exercises it and would fail if it broke |
| **PARTIAL** | demonstrated, with a stated gap |
| **LIMIT** | a declared limitation, asserted so the claim and the behaviour cannot drift apart |
| **REMOVED** | the claim was not true and is gone |

The point of the fourth column is the third one: a limitation nobody tests is a
sentence, and a sentence rots. Where a limit is checkable, there is a check that
fails the day the behaviour changes — so either the code or the document has to
move, and neither can quietly stop matching the other.

## Capture

| claim | verdict | backed by |
|---|---|---|
| Captures `httpx` at the transport | PROVEN | `test_audit`: foreign client, async, concurrency |
| Captures `httpx2` (openai 3.x, anthropic 1.x) | PARTIAL | `t_httpx2_captured` — a **no-op where httpx2 is not installed**, and it says so when it skips |
| Captures `requests` via the adapter | PROVEN | `t_requests_captured`, `t_requests_streaming`, `t_mixed_libraries` |
| Notices `aiohttp` / `urllib` without capturing | PROVEN | `t_unseen_library`, and `UNCAPTURED_LIBRARY` blocks `IDENTICAL` |
| Streaming reaches the agent as it arrives | PROVEN | `t_streaming_passthrough`, `t_streaming_realtime` |
| Binary bodies survive intact | PROVEN | `t_binary_roundtrip` |
| Real `openai` / `anthropic` SDKs work | PARTIAL | `test_real_sdk` against a local server that speaks the protocol. **No vendor endpoint has ever been called.** |

## Determinism and replay

| claim | verdict | backed by |
|---|---|---|
| 16 of 20 non-determinism sources captured (15 strict) | PROVEN | `test_conformance_has_no_undeclared_failures` asserts the published numbers |
| Four sources are declared limits | LIMIT | same check: zero *undeclared* failures |
| Replay forwards nothing to the network | PROVEN | `t_requests_captured` (the server's call counter does not move across a replay), `t_openai_sdk_replays` (a real side effect is not repeated); a miss gets a synthetic 599 |
| Parallel calls replay in the recorded order | PROVEN | `t_index_race`, `t_async` |
| Concurrent `record()` blocks do not cross-contaminate | PROVEN | `t_concurrent_no_crosstalk` |
| Twenty verdicts, none a bare "diverged" | PROVEN | every code has a check; `t_fixed_and_still_broken`, `t_new_call`, OUTPUT_CHANGED |
| A replay cannot test a path the recording never took | LIMIT | `NEW_CALL`, stated in `limits.md` |
| Library version drift cannot be replayed | LIMIT | recorded and reported, never counted as a divergence (`t_runtime_difference_is_reported_not_enforced`) |
| **A replay shims the clock process-wide** | LIMIT | newly documented in `limits.md`. Found while testing the live server, which was consuming the agent's own clock entries — fixed there, and stated for everyone else's threads |

## Secrets

| claim | verdict | backed by |
|---|---|---|
| Request headers are never stored | PROVEN | `t_request_headers_are_never_stored`, greps the file |
| Credential-shaped fields redacted in requests and responses | PROVEN | `test_redaction`: nested, in lists, form-encoded, response bodies |
| Credentials in a JSON document nested **as a string** | PROVEN | `t_secret_in_json_that_arrived_as_a_string` — this was a real leak, found by the tool-call work |
| `user:password@` stripped from URLs anywhere | PROVEN | `t_userinfo_in_a_url_inside_a_body`, `t_resp_header_secret` |
| Response `Set-Cookie` dropped | PROVEN | `t_response_set_cookie_is_dropped` |
| Environment not captured unless named | PROVEN | `t_environment_is_not_captured_by_default` |
| A secret-shaped env name is refused even when named | PROVEN | `t_a_secret_shaped_env_name_is_refused_even_when_asked_for` |
| **A secret in a URL path is not redacted** | LIMIT | `t_a_secret_in_a_url_path_is_a_declared_limit` asserts both the behaviour **and** that the docs still say so |
| Nothing leaks into the viewer, the diff or a report | PROVEN | `t_no_secret_reaches_any_surface` greps all three |

Every redaction check asserts against the **bytes on disk**. None recomputes the
expected redaction, because a helper that mirrors the production rule passes for
the same reason the production code fails.

## The execution model

| claim | verdict | backed by |
|---|---|---|
| Steps typed `model` / `tool` | PROVEN | `t_step_roles`, and `t_openai_sdk_typed_steps` on real SDK traffic |
| Typing is a heuristic and can be wrong | LIMIT | `limits.md`, and `t_classification_does_not_touch_matching` proves a wrong label cannot move a verdict |
| Model metadata by field | PROVEN | `t_model_metadata`, `t_response_metadata`, `t_streamed_response_metadata` |
| Tool calls: OpenAI, Anthropic, Responses API, Gemini | PROVEN | `test_tools`, including streamed reassembly |
| An unknown response shape yields nothing | PROVEN | `t_unknown_shape_invents_nothing` |
| No link from a tool request to the HTTP call that ran it | LIMIT | stated in `execution-model.md`; deliberately not inferred |
| The final output must be declared | LIMIT | `limits.md`; `t_undeclared_output_changes_nothing` |
| Format 3 recordings still replay | PROVEN | `t_v3_recording_still_replays`, `t_migration_preserves_chain` |
| Formats below 3 are not migrated | LIMIT | `t_pre_migration_formats_stay_stale` |

## Evaluation, cases, baselines

| claim | verdict | backed by |
|---|---|---|
| Seven evaluators, structured results with evidence | PROVEN | `test_evaluate`, 20 checks |
| `warn` never fails a build | PROVEN | `t_a_warning_does_not_fail_a_report`, `t_warnings_do_not_fail_a_case` |
| A case is a recording + entry + expectations | PROVEN | `test_cases`, 31 checks |
| A baseline is a stored object, not a kept file | PROVEN | `t_baseline_is_a_stored_object` |
| `ci.compare` is reused, not reimplemented | PROVEN | `t_baseline_reuses_ci_compare` asserts the equality |
| Exit 0 / 1 / 2 mean different things | PROVEN | `t_cli_test_exit_codes` |
| With a baseline, only new breakage fails | PROVEN | `t_cli_test_only_fails_on_what_this_change_broke` |
| A failing case explains itself, without a second command | PROVEN | `t_a_failing_case_explains_itself`, `t_evidence_reaches_the_machine_readable_report`; measured on the lab in `lab/VALIDATION.md` (4 of 10 fully explained by CI alone) |
| A passing case computes no explanation | PROVEN | `t_a_passing_case_carries_no_evidence` |
| The evidence claims no cause | PROVEN | `t_evidence_claims_no_cause` |
| The evidence does not out-claim the evaluator beside it | PROVEN | `t_evidence_does_not_invent_a_tool_decision` |
| No verdict the trace does not license | PROVEN | `test_observation`, 21 checks including the five counterexamples |
| Completeness is per question, not per run | PROVEN | `t_completeness_is_relative_to_the_question` |
| A prohibition is UNKNOWN, not PASS, without the model's responses | PROVEN | `t_A_did_not_call_is_unknown_without_the_model_response` |
| An observed violation still fails, whatever else is missing | PROVEN | `t_B_did_not_call_fails_on_an_observed_request`, `t_E_a_real_error_fails_even_beside_a_divergence` |
| A replay's synthetic 599 is not counted as the agent's failure | PROVEN | `t_D_no_step_failed_is_unknown_on_a_synthetic_599`, and `t_E_no_step_failed_still_fails_on_a_real_error` for the other half |
| Observed absence, unobserved and observed violation are three verdicts | PROVEN | `t_the_three_claims_are_distinguishable` |
| Custom checks keep their behaviour unless they declare | PROVEN | `t_a_custom_check_without_a_declaration_still_runs` |
| A response arriving is not a response being readable | PROVEN | `t_P0_1_the_tool_view_is_its_own_domain` |
| An unreadable envelope decides no tool question, either way | PROVEN | `t_P0_1_an_unreadable_envelope_does_not_prove_a_prohibition`, `t_P0_1_an_unreadable_envelope_does_not_prove_an_absence` |
| A stream with no terminator decides no prohibition | PROVEN | `t_P0_1_a_cut_stream_does_not_prove_a_prohibition` |
| A misspelt observation domain is refused, not ignored | PROVEN | `t_P0_1_an_unknown_domain_is_refused_at_declaration`, `t_P0_1_an_unknown_domain_is_not_silently_complete` |
| A regex match on a truncated answer is not a witness | PROVEN | `t_P0_2_a_prefix_match_is_not_a_witness` |
| An answer that could not be captured is not an empty string | PROVEN | `t_P0_2_an_uncaptured_answer_is_not_an_empty_string` |
| A new violation inside an already-failing case is surfaced | PROVEN | `t_P0_4_a_new_violation_inside_a_failing_case_is_surfaced` |
| A rule that left the suite, or lost its proof, is surfaced | PROVEN | `t_P0_4_an_obligation_that_stopped_being_checked_is_surfaced`, `t_P0_4_an_obligation_that_lost_its_proof_is_surfaced` |
| A replay by a different principal is **not** detected | LIMITATION, PINNED | `t_P0_3_a_principal_change_is_not_detected` — see [limits](limits.md) |
| A clean replay says so instead of printing step counts | PROVEN | `t_evidence_says_when_the_replay_is_clean` |
| `--no-evidence` reaches the output, not only the report | PROVEN | `t_no_evidence_keeps_prompts_out_of_the_output_too` |

## The explanatory diff

| claim | verdict | backed by |
|---|---|---|
| Alignment, not index-by-index | PROVEN | `t_insertion_does_not_smear`, `t_deletion`, `t_reorder_is_one_change_not_four` |
| Model config, tool calls, bodies and answers by field | PROVEN | `test_diff`, 36 checks |
| A side with no answers makes no tool claim | PROVEN | `t_a_side_with_no_answers_makes_no_tool_claim`, `t_two_answered_runs_still_compare_their_tools`, `t_a_partly_answered_run_is_not_blind` |
| Truncation never produces a false claim | PROVEN | `t_output_truncation_makes_no_false_claim` |
| No invented cause | PROVEN | `t_consequence_never_claims_a_cause`, `t_unknown_provenance_stays_unknown` |
| No language model anywhere in it | PROVEN | there is no model call in the codebase; `pip freeze` on a clean install is `httpx` and its closure |
| Bounded on large runs | PROVEN | `t_alignment_is_bounded` (10/100/1000 steps), `t_degradation_is_announced` |
| Move detection is dropped on very large repetitive runs | LIMIT | announced in the report, asserted by `t_degradation_is_announced` |

## Concurrency

| claim | verdict | backed by |
|---|---|---|
| Single-agent execution | PROVEN | `t_single_agent_execution_is_all_sequential` |
| Parallel calls are observable | PROVEN | `t_three_parallel_calls_are_one_group`, `t_parallel_child_agents_over_http` |
| Replay stays deterministic | PROVEN | `t_replay_determinism_is_unchanged`, and `t_replay_matching_does_not_read_the_concurrency_layer` strips the fields and demands the same verdict |
| Concurrency metadata is not in the hash chain | PROVEN | `t_no_concurrency_field_is_in_the_digest` |
| A parallel reorder is not automatically a regression | PROVEN | `t_policy_decides_whether_order_matters`, `t_orientim_test_semantics_are_unchanged_by_a_reorder` |
| Strong sequential and concurrency changes are reportable | PROVEN | `t_a_provable_sequencing_change_is_strong`, `t_concurrency_change_is_its_own_finding` |
| No causal claim | PROVEN | `t_nothing_claims_a_cause` greps for "root cause", "caused by", "because of" |
| `run.client(timeout=...)` | PROVEN | `t_client_timeout_is_backward_compatible` — silent callers still get 10s |
| A case carries its own input | PROVEN | `t_case_input_is_per_case_and_needs_no_environment` — two cases, one process, two inputs |
| **A nested record drops worker-thread calls** | LIMIT | `t_nested_record_drops_calls_from_worker_threads` pins both halves |
| **Fleet / parent-child execution** | **NOT BUILT** | described in `concurrency.md`, claimed nowhere |

## Storage and operations

| claim | verdict | backed by |
|---|---|---|
| S3 / R2 / MinIO via one setting | PARTIAL | `test_storage` against moto, including a full record-and-replay round trip. **A mock is not a bucket** — see `limits.md` |
| ~~"The S3 backend is tested against moto"~~ (as written before) | REMOVED → PROVEN | moto appeared in **zero tests** when that sentence was written. It is now true, and `limits.md` records that it was not |
| Local, memory and S3 backends route by URL | PROVEN | `t_routing_picks_the_backend` |
| Retention removes nothing without a rule | PROVEN | `t_cli_prune_needs_a_rule` |
| Stability: paths, agreement, control limits | PROVEN | `test_stability`, 16 checks |
| **A lone outlier can hide inside its own limits** | LIMIT | `t_a_lone_extreme_outlier_can_hide_inside_its_own_limits` — found while writing these tests |
| The live server refuses unauthorised replays | PROVEN | `test_server`: no token, wrong token, foreign origin |
| Nothing has run in production | LIMIT | stated in `limits.md`. Still true. |

## Packaging

| claim | verdict | backed by |
|---|---|---|
| One runtime dependency | PROVEN | clean-install closure is `httpx` + `httpcore`, `h11`, `anyio`, `certifi`, `idna`, `typing_extensions` |
| Installs clean and the CLI runs | PROVEN | CI `package` job: build, `twine check`, install the wheel into an empty venv, run it |
| No server, no account, no telemetry | PROVEN | nothing in the runtime closure speaks to anything; `orientim conformance` runs offline |
| Python 3.9 – 3.13, three operating systems | PROVEN | CI matrix, 14 combinations |

## What was found while writing this

Six defects, all in code that had a passing suite around it:

1. **`measure(runs=0)` raised `IndexError`** — `Counter.most_common(1)[0]` on an
   empty sequence, inside the command somebody runs to find out whether their
   agent is stable.
2. **The live server consumed the agent's clock entries.** Every response it
   sent computed a `Date` header with `time.time()`, which a running replay had
   shimmed. It now holds the real clock for its own bookkeeping.
3. **An unauthorised POST got a connection reset, not a 403.** The server
   answered without draining the request body. A person told "forbidden" can act
   on it; a person shown a network error cannot.
4. **A malformed body raised instead of returning 400.**
5. **`no_step_failed()` passed vacuously** on a run where every request came
   back 599 — found during Phase 3, listed here because it is the same class:
   a check that could not fail.
6. **Twenty-one lint findings** — unused imports, dead locals, lost exception
   context — now clean and enforced in CI.

## Quality gates, and the ones deliberately not adopted

| | |
|---|---|
| **linter** | `ruff`, rules `F`, `E9`, `B`. Enforced in CI |
| **packaging** | `python -m build`, `twine check`, clean-venv install. Enforced |
| **dependency audit** | `pip-audit` over the runtime closure. Enforced |
| **conformance** | the published numbers, regenerated on every push. Enforced |
| formatter | **not adopted.** The source is hand-aligned where the alignment carries meaning |
| type checking | **not adopted.** The package ships no annotations, so a type checker would verify that nothing is annotated. `py.typed` is deliberately absent for the same reason: it would tell other people's checkers something untrue |
| **coverage** | measured at **88%**, gated at 80% in CI. A floor, not a target: it catches a deleted test module and leaves ordinary movement alone. Chasing the last percent rewards tests that execute lines, which is not the same as tests that can fail |

The default `ruff` rule set finds 751 problems here, roughly 700 of them
`UP031`: this package supports Python 3.9 and uses percent formatting on
purpose. A gate silenced 700 times is not a gate.
