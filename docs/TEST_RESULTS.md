# Test results

Command (from a clean checkout, after `pip install -r requirements-dev.txt`):

```
python -m pytest -v
```

Run on Windows 11, Python 3.12.10, PostgreSQL 16.2 (embedded via pgserver), 16 Oct 2026. All tests run against PostgreSQL; the schema is created by the Alembic migration.

| File | What it covers | Brief's minimum test |
|---|---|---|
| test_unit_rules.py | UOM conversion, partial receipts, deadline boundaries, cancellation, posting status, identity mismatches, tolerance | 1, 6 |
| test_unit_mapping_routing.py | Canonical mapping, ambiguity, mapping version, routing eligibility, fallback, manual queue, supervision scope, state machine | 4, 5 |
| test_ingestion.py | Row-level validation, duplicates, idempotent re-import, conflicting ids, collisions, concurrent imports | 3, 6 |
| test_detection_integration.py | Fixture pack end to end: six required scenarios, provenance, neutral wording, idempotent re-runs, concurrent runs, data-quality paths, late receipts | 2, 3, 4, 6 |
| test_api_workflow.py | HTTP contract, lifecycle, invalid transitions, retries, reassignment, verification, audit history | 5 |
| test_ai_boundary.py | AI output validation and safe fallback with fake providers | AI boundary |

```
tests/test_ai_boundary.py::test_ai_output_is_validated_and_fails_safe[valid] PASSED [  1%]
tests/test_ai_boundary.py::test_ai_output_is_validated_and_fails_safe[invented-record] PASSED [  2%]
tests/test_ai_boundary.py::test_ai_output_is_validated_and_fails_safe[asserts-fault] PASSED [  3%]
tests/test_ai_boundary.py::test_ai_output_is_validated_and_fails_safe[bad-schema] PASSED [  5%]
tests/test_ai_boundary.py::test_ai_output_is_validated_and_fails_safe[timeout] PASSED [  6%]
tests/test_ai_boundary.py::test_disabled_provider_returns_deterministic_summary PASSED [  7%]
tests/test_api_workflow.py::test_import_endpoint_reports_partial_success_and_row_errors PASSED [  8%]
tests/test_api_workflow.py::test_unreadable_file_is_422_with_a_visible_failed_import PASSED [ 10%]
tests/test_api_workflow.py::test_detection_run_and_exception_detail PASSED [ 11%]
tests/test_api_workflow.py::test_list_tasks_by_assignee_and_location PASSED [ 12%]
tests/test_api_workflow.py::test_full_lifecycle_to_verified_resolves_the_exception PASSED [ 13%]
tests/test_api_workflow.py::test_invalid_transitions_and_wrong_actor_are_refused PASSED [ 15%]
tests/test_api_workflow.py::test_retry_after_commit_with_idempotency_key_returns_the_original_response PASSED [ 16%]
tests/test_api_workflow.py::test_repeated_completion_without_key_is_safe PASSED [ 17%]
tests/test_api_workflow.py::test_reassignment_rules_and_history PASSED   [ 18%]
tests/test_api_workflow.py::test_verification_requires_an_independent_supervisor_and_can_reopen PASSED [ 20%]
tests/test_api_workflow.py::test_block_and_unblock PASSED                [ 21%]
tests/test_api_workflow.py::test_manual_queue_task_can_be_assigned_by_a_supervisor PASSED [ 22%]
tests/test_api_workflow.py::test_errors_are_structured_with_request_id PASSED [ 24%]
tests/test_detection_integration.py::test_required_scenario_outcomes PASSED [ 25%]
tests/test_detection_integration.py::test_finding_is_traceable_to_source_rows_and_versions PASSED [ 26%]
tests/test_detection_integration.py::test_explanation_is_neutral_about_cause PASSED [ 27%]
tests/test_detection_integration.py::test_duplicate_receipt_record_is_counted_once PASSED [ 29%]
tests/test_detection_integration.py::test_tasks_are_routed_to_eligible_people_with_reasons PASSED [ 30%]
tests/test_detection_integration.py::test_rerunning_import_and_detection_changes_no_business_result PASSED [ 31%]
tests/test_detection_integration.py::test_concurrent_detection_runs_create_one_exception_and_one_task_per_case PASSED [ 32%]
tests/test_detection_integration.py::test_unknown_product_mapping_is_data_quality_not_an_exception PASSED [ 34%]
tests/test_detection_integration.py::test_conflicting_receipt_blocks_evaluation_until_resolved PASSED [ 35%]
tests/test_detection_integration.py::test_accepting_a_corrected_receipt_updates_the_finding_with_history PASSED [ 36%]
tests/test_detection_integration.py::test_late_receipt_that_closes_the_gap_clears_the_case_and_cancels_open_tasks PASSED [ 37%]
tests/test_detection_integration.py::test_manual_queue_when_no_eligible_assignee PASSED [ 39%]
tests/test_detection_integration.py::test_unmapped_employee_location_is_not_used_for_routing PASSED [ 40%]
tests/test_ingestion.py::test_fixture_pack_counts_and_identical_duplicates PASSED [ 41%]
tests/test_ingestion.py::test_reimporting_the_same_files_is_a_no_op PASSED [ 43%]
tests/test_ingestion.py::test_formatting_noise_is_not_a_change PASSED    [ 44%]
tests/test_ingestion.py::test_invalid_rows_are_rejected_with_reasons_and_valid_rows_still_load PASSED [ 45%]
tests/test_ingestion.py::test_missing_required_column_fails_the_whole_file_visibly PASSED [ 46%]
tests/test_ingestion.py::test_same_source_id_with_changed_content_is_flagged_not_overwritten PASSED [ 48%]
tests/test_ingestion.py::test_new_source_id_reusing_a_business_key_is_a_collision PASSED [ 49%]
tests/test_ingestion.py::test_conflicting_duplicate_inside_one_file PASSED [ 50%]
tests/test_ingestion.py::test_concurrent_imports_of_the_same_file_store_each_fact_once PASSED [ 51%]
tests/test_ingestion.py::test_employee_needs_some_scope_and_extra_columns_do_not_break_import PASSED [ 53%]
tests/test_unit_mapping_routing.py::test_erp_and_wms_ids_resolve_to_one_canonical_product_with_factor PASSED [ 54%]
tests/test_unit_mapping_routing.py::test_same_external_id_in_another_system_is_not_reused PASSED [ 55%]
tests/test_unit_mapping_routing.py::test_inconsistent_base_unit_is_ambiguous_not_guessed PASSED [ 56%]
tests/test_unit_mapping_routing.py::test_alias_under_open_conflict_is_ambiguous PASSED [ 58%]
tests/test_unit_mapping_routing.py::test_location_crosswalk_and_region_ambiguity PASSED [ 59%]
tests/test_unit_mapping_routing.py::test_mapping_version_changes_when_alias_content_changes PASSED [ 60%]
tests/test_unit_mapping_routing.py::test_store_014_routes_to_the_on_shift_active_receiver PASSED [ 62%]
tests/test_unit_mapping_routing.py::test_store_015_falls_back_to_the_store_manager PASSED [ 63%]
tests/test_unit_mapping_routing.py::test_no_eligible_person_goes_to_manual_queue_not_to_someone_ineligible PASSED [ 64%]
tests/test_unit_mapping_routing.py::test_regional_employee_without_store_is_never_auto_assigned_a_store_task PASSED [ 65%]
tests/test_unit_mapping_routing.py::test_stale_availability_sample_is_not_trusted PASSED [ 67%]
tests/test_unit_mapping_routing.py::test_outside_shift_window_is_ineligible_even_if_marked_on_shift PASSED [ 68%]
tests/test_unit_mapping_routing.py::test_unmapped_hrms_location_makes_employee_ineligible PASSED [ 69%]
tests/test_unit_mapping_routing.py::test_latest_sample_at_or_before_as_of_wins_and_future_samples_are_ignored PASSED [ 70%]
tests/test_unit_mapping_routing.py::test_routing_is_deterministic_by_employee_id PASSED [ 72%]
tests/test_unit_mapping_routing.py::test_supervision_scope PASSED        [ 73%]
tests/test_unit_mapping_routing.py::test_state_machine_rejects_skipping_steps PASSED [ 74%]
tests/test_unit_rules.py::test_partial_receipts_are_summed_into_one_shortfall PASSED [ 75%]
tests/test_unit_rules.py::test_box_is_converted_to_each_before_comparison PASSED [ 77%]
tests/test_unit_rules.py::test_box_ordered_and_fully_received_in_each_is_not_a_shortfall PASSED [ 78%]
tests/test_unit_rules.py::test_full_receipt_is_no_shortfall PASSED       [ 79%]
tests/test_unit_rules.py::test_deadline_boundary[deadline-after-as-of] PASSED [ 81%]
tests/test_unit_rules.py::test_deadline_boundary[deadline-equals-as-of] PASSED [ 82%]
tests/test_unit_rules.py::test_deadline_boundary[deadline-passed] PASSED [ 83%]
tests/test_unit_rules.py::test_deadline_is_compared_as_an_instant_across_offsets PASSED [ 84%]
tests/test_unit_rules.py::test_cancelled_line_is_not_applicable_even_with_no_receipts PASSED [ 86%]
tests/test_unit_rules.py::test_cancelled_line_with_unmapped_item_is_still_just_cancelled PASSED [ 87%]
tests/test_unit_rules.py::test_unposted_and_reversed_receipts_are_excluded PASSED [ 88%]
tests/test_unit_rules.py::test_receipt_stamped_after_as_of_is_not_counted PASSED [ 89%]
tests/test_unit_rules.py::test_late_receipt_before_as_of_still_counts PASSED [ 91%]
tests/test_unit_rules.py::test_unknown_product_is_a_data_quality_issue_not_an_exception PASSED [ 92%]
tests/test_unit_rules.py::test_unit_not_declared_by_alias_is_not_guessed PASSED [ 93%]
tests/test_unit_rules.py::test_receipt_for_another_location_blocks_the_line PASSED [ 94%]
tests/test_unit_rules.py::test_receipt_for_another_product_blocks_the_line PASSED [ 96%]
tests/test_unit_rules.py::test_conflicted_receipt_blocks_the_line PASSED [ 97%]
tests/test_unit_rules.py::test_over_receipt_is_recorded_but_not_a_shortfall PASSED [ 98%]
tests/test_unit_rules.py::test_configurable_tolerance PASSED             [100%]
============================= 79 passed in 50.88s =============================
```
