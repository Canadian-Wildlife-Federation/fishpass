# WCRP Tracking Table

**Table:** `<code>_wcrp.tracking_table_<code>`

Hand-entered, per-barrier tracking data for a WCRP (e.g. structure list status, assessments, rehabilitation, next steps), edited directly, typically in QGIS. Created automatically at the start of a plan's first model run: before anything else, every run checks for the plan's tracking table and creates it if it doesn't exist. On later runs creation is skipped and the table is left unchanged. Either outcome is reported in the log and the GitHub Actions job summary.

**Persistence:** the table lives in the persistent `<code>_wcrp` schema, NOT the plan's `output_schema`, because the output schema is dropped and rebuilt on every model run. Once created it is never dropped, replaced, or altered by any script. Model runs skip creation when it exists, and running `create_wcrp_tracking_table.py` directly against an existing table stops with an error without making changes.

**No foreign key:** `barrier_id` matches `<output_schema>.all_barriers.feature_id` in type, but a foreign key into the ephemeral output schema couldn't survive the rebuild. Instead, every model run checks each `barrier_id` against the freshly built `all_barriers` and logs any that don't match (see [ranked_barriers.md](./ranked_barriers.md)).

**Support objects:** the `support.tt_*` enum types used below are defined in the `wcrp.tracking_table_enums` section of [config/fishpass.yaml](../../../config/fishpass.yaml), and every model run syncs them into the database before the tracking table is created or checked. The `support.blank2null()` trigger function comes from [init/database/wcrp_support.sql](../../../init/database/wcrp_support.sql), which is run by hand once per database.

**Dropdowns and blank values:** the enum columns show up as dropdowns in QGIS. Every enum also includes a blank (`''`) option, last in the list, because QGIS writes `''` when a user clears a dropdown and PostgreSQL rejects any value that isn't in the enum. The `blank2null_trg` trigger (`BEFORE INSERT OR UPDATE`, calling `support.blank2null()`) then turns any `''` in an enum column into NULL, so blanks are never actually stored.

**Ownership:** owned by `database_roles.owner`, with ALL granted to `database_roles.grant_all` and SELECT to `database_roles.grant_select` (all from `config/fishpass.yaml`). Finer-grained, per-WCRP biologist access is applied separately.

**Per-species columns:** one set for each `target_species` in the plan, at the time the table is created. Adding a species to the plan later does NOT add columns; add them by hand.

Table Structure (in column order; `<sp>` = species code):

| Field | Type | Comment |
| :---- | :---- | :---- |
| internal_name | varchar | |
| barrier_id | uuid | primary key; `all_barriers.feature_id` of the barrier (cabd_id, gradient barrier id, or new structure id) |
| watercourse_name | varchar | |
| road_name | varchar | shown as `tracking_road_name` in the combined view |
| structure_type | support.tt_structure_type | Dam, Stream crossing - OBS, Stream crossing - CBS, Stream crossing - Ford, Other, None. Shown as `tracking_structure_type` in the combined view. |
| structure_owner | varchar | |
| private_owner_details | varchar | |
| structure_list_status_\<sp\> | support.tt_structure_list_status_type | Excluded structure, Data-deficient barrier, Non-actionable barrier, Priority barrier, Rehabilitated barrier. `Rehabilitated barrier` puts the barrier back into that species' ranking even if it is now passable. |
| passability_assessment_type | support.tt_passability_asmt_type | Informal assessment, Rapid assessment, Full assessment |
| assessment_step_completed | support.tt_assessment_step_type | Informal assessment, Passability assessment, Habitat confirmation, Detailed habitat investigation, Engineering design, Rehabilitated, Post-rehabilitation monitoring, Other |
| reason_for_exclusion | support.tt_excl_reason_type | Passable, No structure, No key upstream habitat, No structure and key upstream habitat |
| method_of_exclusion | support.tt_excl_method_type | Imagery review, Informal assessment, Field assessment, Local knowledge |
| partial_passability_\<sp\> | support.tt_partial_passability_type | Yes, No, Unknown |
| partial_passability_notes_\<sp\> | support.tt_partial_passability_notes_type | Proportion of individuals, Proportion of time |
| upstream_habitat_quality | support.tt_upstr_hab_quality_type | High, Medium, Low, N/A or unassessed |
| constructability | support.tt_constructability_type | Difficult, Moderate, Easy |
| estimated_cost_$ | numeric | |
| priority | support.tt_priority_type | High, Medium, Low |
| type_of_rehabilitation | support.tt_rehab_type | Removal/decommissioned, Replacement - OBS, Replacement - CBS, Retrofit |
| rehabilitated_by | varchar | |
| rehabilitated_date | text | |
| estimated_rehabilitation_cost_$ | numeric | |
| actual_project_cost_$ | numeric | |
| next_steps | support.tt_next_steps_type | Barrier assessment, Barrier reassessment, In-depth passage assessment, Habitat confirmation, In-depth habitat investigation, Identify barrier owner, Engage with barrier owner, Engage with partners, Engage in public consultation, Bring barrier to regulator, Commission engineering designs, Fundraise, Rehabilitation, Post-rehabilitation monitoring, Correct deficiencies, Leave until end of lifecycle, Non-actionable, N/A - project complete |
| timeline_for_next_steps | text | |
| lead_for_next_steps | varchar | |
| others_involved_in_next_steps | varchar | |
| reason | varchar | |
| notes | varchar | |
| supporting_links | varchar | |

The per-species columns are grouped: all `structure_list_status_<sp>` columns follow `private_owner_details`, and all `partial_passability_<sp>` / `partial_passability_notes_<sp>` pairs follow `method_of_exclusion`.

Allowable enum values come from the BC Tracking Table Guidance on Notion. To add a value, add it to the type's list under `wcrp.tracking_table_enums` in `config/fishpass.yaml`. The next model run adds it to the end of the dropdown (just before the blank) without touching existing data. The blank is added automatically, so don't list it in the YAML.

Renaming or removing a value in the YAML is not applied to the database and has to be done by hand. To rename, run `ALTER TYPE support.<type> RENAME VALUE 'old' TO 'new';` (existing rows follow automatically) and make the same change in the YAML. PostgreSQL can't drop an enum value, so removing one means updating any rows that use it and then rebuilding the type.
