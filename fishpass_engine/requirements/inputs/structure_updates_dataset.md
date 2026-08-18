# Structure Updates Dataset

The structure updates dataset provides a mechanism for users to overwrite information about barriers provided from the CABD dataset.

**Source:** FishPass Database

**Table:** support.structure_updates

| Column | Type | Description |
| :---- | :---- | :---- |
| id | uuid | Primary key for the table. |
| barrier_id | uuid | Barrier identifier for the update. If the feature is from CABD, `barrier_id` = `cabd_id`; otherwise `barrier_id` = `new_structure_id`. There can be multiple entries for the same barrier. |
| feature_type | varchar | Feature type (either from CABD or the new structure feature type). For reference purposes only; not used for analysis. |
| update_type | enum: authoritative, local_override | If there are multiple entries for a given barrier_id, the code should prioritize the local_override entry for reporting. |
| update_scope | array[varchar] | Values: 'all', or any specific plan code (e.g.: cheticamp_wcrp). Determines how the structure update is applied — to all plan outputs, or just an individual plan output. The plan will take all updates with `update_scope` = 'all' OR `update_scope` contains the plan code. Conflict resolution is detailed under update_type. |
| passability_status | jsonb | Passability status per species as a JSON string: `{"es": 0.25, "wl": 1}`. If this is null, or the specific species doesn't exist in the JSON string, the structure is assumed to be a full barrier (impassable) for that species. |
| update_source | varchar | Source of the update — e.g., a particular organization, workshop, assessment, or data source. |
| update_date | date | Date the update was collected. For assessment data, this is the date of assessment. For other sources, this is the date the information was received or collected. Updates are prioritized first by update_type, then by update_date. |
| notes | varchar | Reason for the update to the structure (i.e., incorrect CABD structure, or new structure added by a WCRP partner). |

#### CWF Notes

There are three possible outcomes for structure updates:

1. Integrated into CABD (handled via CABD update workflows)
2. Authoritative updates that we want applied across all model outputs (but not integrated into CABD)
3. WCRP-specific updates that we only want applied for specific WCRP outputs

To support this, we propose one structure updates table to handle outcomes 2 and 3 above, with certain attributes determining how updates are applied.
