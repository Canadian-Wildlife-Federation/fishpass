# Azure Exports

This document outlines the requirements for exporting FishPass engine results from the database to Azure Blob Storage.

**Status:** proposed -- not yet implemented. See the open questions [to confirm with CWF](#open-questions---to-confirm-with-cwf) and [for implementation](#open-questions---for-implementation) for the items still to be decided.

## Overview

At the end of a model run, the results can optionally be exported to an Azure Blob Storage container. The export:

1. is turned on per run from the FishPass GitHub action;
2. writes to a container named after the model plan code;
3. writes every file for a run into its own folder, either versioned or unversioned;
4. is driven by a table in the database (`<output_schema>.azure_exports`) that lists what to export, so the list of exports can change without changing the export code;
5. can be [re-run on its own](#re-running-an-export) without re-running the model, because everything it needs is saved in the database.

## Azure Connection

The Azure connection details are stored as GitHub secrets and passed to the run as environment variables, the same way the `FISHPASS_*` database secrets are handled in [fishpass_engine.yml](../../.github/workflows/fishpass_engine.yml).

## GitHub Actions

The existing fishpass run action will be updated to include azure export option and a new export-only action will be implement to export the results (or re-export the results).

### Update: FishPass Run Action
Three inputs are added to the FishPass run action:

| Input | Type | Applies when | Description |
| :---- | :---- | :---- | :---- |
| Export to Azure | true/false | always | Export the results to Azure. When false, nothing is exported and the other two inputs are ignored. |
| Type | dropdown: `versioned` / `unversioned` | export is true | Whether the run is a versioned release or an unversioned run. |
| Version | free text | type is `versioned` | The version label. Maximum 16 characters; letters, numbers, `_` and `.` only (`a-z`, `A-Z`, `0-9`, `_`, `.`). Required when type is `versioned`. |

GitHub's manual run form cannot show or hide an input based on another input, so all three are always displayed. The run validates the combination before the model starts and stops with an error if the version is missing, too long, or contains other characters.

### New: Export-Only Action

The export-only action is a new GitHub action that runs only the export. It has the plan code, type and version inputs (validated the same way as in [Update: FishPass Run Action](#update-fishpass-run-action)) and does not run the model.

Existing azure files with the same name from previous runs will be overwritten. File names (see [Azure Blob Names](#azure-blob-names)) include the run date/time not the export date/time. The type and version can be different from the original export. This allows a run to be exported as `unversioned`, reviewed, and then exported again as `versioned` without re-running the model. A different type or version writes to a different folder.

The model plan file, species parameter file and run details in `metadata.yaml` come from the run metadata table, so they are the versions used for the model run even if GitHub has changed since.

The action stops with an error if the plan's output schema has no `azure_exports` or `run_metadata` table.

## Azure Storage Location

### Azure Container

One container is used per model plan. It is created if it doesn't already exist, and is named from the plan `code` in the [model plan file](./inputs/model_plan_file.md), converted as follows:

- `A-Z` is lower-cased
- spaces and `_` are replaced with `-`
- `a-z`, `0-9` and `-` are kept
- all other characters are removed
- repeated `-` are collapsed to a single `-`, and any leading or trailing `-` is removed
- the result is prefixed with `fishpass-`

Azure container names only allow lowercase letters, numbers and `-`, and must be 3-63 characters. The prefix keeps short plan codes above the minimum length and makes the FishPass containers easy to find in the storage account. The export stops with an error if the final name is longer than 63 characters.

| Plan code | Container name |
| :---- | :---- |
| `ns` | `fishpass-ns` |
| `asf_phase1` | `fishpass-asf-phase1` |
| `ASF_Phase_1` | `fishpass-asf-phase-1` |
| `ns__2026_` | `fishpass-ns-2026` |

### Azure Blob Names 

Within the container, each run gets its own folder and every file is prefixed with the folder name:

```text
runs/<type>/<model>_<version>_<yyyymmddhhmmss>/<model>_<version>_<yyyymmddhhmmss>_<filename>.<ext>
```

| Part | Description |
| :---- | :---- |
| `<type>` | `versioned` or `unversioned`, from the action input. |
| `<model>` | The plan `code`. |
| `<version>` | The version from the action input. Left out, along with its `_`, when no version is provided. |
| `<yyyymmddhhmmss>` | Date/time of the model run, in UTC, from the [run metadata table](#model-run-metadata-table). This is the model run time, not the export time, so re-running an export writes to the same folder. |
| `<filename>.<ext>` | The output file name and extension for the export (see [Export Table](#export-table)). |

Examples for plan code `ns`, run on 2026-10-07 at 14:30:00 UTC:

```text
runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_streams.gpkg
runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_metadata.yaml
runs/unversioned/ns_20261007143000/ns_20261007143000_streams.gpkg
```

## Database Control Tables

### Export Table

**Table:** `<output_schema>.azure_exports`

To allow what is exported to expand and change without changing the export code, the list of exports lives in a table in the database. The scripts that create each output also write a row to this table describing how to export it. The export script then reads the table and writes one file to Azure per row.

The output schema is dropped and rebuilt on every model run, so the table starts empty each run and only ever lists what that run produced -- don't hand-edit it. If exports that persist between runs are needed in the future, they could be kept in the persistent `<code>_wcrp` schema and copied into this table at the start of each run.

| Field Name | Type | Description |
| :---- | :---- | :---- |
| id | | Primary key. |
| file\_name | varchar | Output file name, without the run prefix or extension (the `<filename>` part of the blob name). |
| sql | text | The SQL query that returns the data to write. For `geopackage` exports the query must return exactly one geometry column, which is used as the geometry in the GeoPackage. The query should only return column data types that are supported in geopackage (no json). |
| format | varchar | Output format: `csv` or `geopackage`. Determines the file extension (`.csv` / `.gpkg`). |

### Formats

Two formats are supported. Exports that have a geometry are written as `geopackage`; exports with no geometry are written as `csv`.

A GeoPackage layer has a single geometry column, so the `sql` for a `geopackage` export must return exactly one geometry column.

Neither format has a list type, so list (array) columns are written as text with the values separated by `|` (e.g. `12|15|31`), in both the CSV and GeoPackage files. A comma is not used because it is the CSV column separator. The `sql` for an export must convert each list column to this form, e.g. `array_to_string(downstr_group_ids, '|')`.

GeoPackage has no uuid or enum type. The `sql` for a `geopackage` export must cast these columns to text:

| Column type | Example column | Example `sql` |
| :---- | :---- | :---- |
| uuid | `barrier_id`, `feature_id` | `barrier_id::text AS barrier_id` |
| enum | the tracking table columns such as `priority` and `constructability` | `priority::text AS priority` |

No cast is needed for a `csv` export, where every value is written as text.

### Model Run Metadata Table 

**Table:** `<output_schema>.run_metadata`

The model run saves the details of the run to this table before the export starts, so the export never depends on the state of GitHub at the time it runs. The table holds a single row and, like the export table, is rebuilt with the output schema on every model run.

| Field Name | Type | Description |
| :---- | :---- | :---- |
| run\_datetime | timestamptz | Date/time the model run started. Written out in UTC. Used for the folder and file names and for `metadata.yaml`. |
| github\_user | varchar | GitHub user that started the model run. |
| github\_branch | varchar | GitHub branch the model was run from. |
| commit\_sha | varchar | Commit the model was run from. |
| plan\_code | varchar | The plan `code`. |
| total\_runtime | interval | Total runtime of the model run. Does not include the time taken by the export. |
| model\_plan\_file | text | The full contents of `config/models/<plan_code>.yaml` used for the run. |
| species\_parameter\_file | text | The full contents of `config/fish_species_parameters.yaml` used for the run. |

The type, version and export date/time are not stored here. They belong to an export, not to the model run, and can differ each time the export is run.



## Data Exports

This section describes the data that will be exported as a part of the first implementation. The required scripts will be updated to add these files to the export table.

### Metadata

Three metadata files are written for every export. They are written directly by the export from the [run metadata table](#model-run-metadata-table) rather than through the export table, so they are always the versions used for the model run.

| File | `<filename>.<ext>` | Format | Source |
| :---- | :---- | :---- | :---- |
| Model plan file | `model_plan.yaml` | YAML | `run_metadata.model_plan_file` -- the contents of `config/models/<plan_code>.yaml` at the time of the model run, unchanged. |
| Species parameter file | `fish_species_parameters.yaml` | YAML | `run_metadata.species_parameter_file` -- the contents of `config/fish_species_parameters.yaml` at the time of the model run, unchanged. |
| Run metadata file | `metadata.yaml` | YAML | The other `run_metadata` fields, plus the details of this export. See [metadata.yaml](#metadatayaml) below. |

Example filenames:

```text
runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_model_plan.yaml
runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_fish_species_parameters.yaml
runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_metadata.yaml
```

#### metadata.yaml

A YAML file with one `name: value` entry per field, in the order below. Text values are quoted so that values such as a version of `1.2` or a runtime of `12:34:48` are read as text, not as numbers.

| Field | Source | Description |
| :---- | :---- | :---- |
| plan\_code | `run_metadata.plan_code` | The plan `code`. |
| export\_type | action input | `versioned` or `unversioned`. |
| version | action input | The version label. `null` when the type is `unversioned`. |
| run\_datetime | `run_metadata.run_datetime` | Date/time the model run started, in UTC (`yyyy-mm-ddThh:mm:ssZ`). The same date/time used in the folder and file names. |
| export\_datetime | set by the export | Date/time the files were written to Azure, in UTC (`yyyy-mm-ddThh:mm:ssZ`). It is normally within minutes of the end of the model run; a large gap tells users the export was re-run some time after the model ran. |
| total\_runtime | `run_metadata.total_runtime` | Total runtime of the model run (`hh:mm:ss`). Does not include the time taken by the export. |
| github\_user | `run_metadata.github_user` | GitHub user that started the model run. |
| github\_branch | `run_metadata.github_branch` | GitHub branch the model was run from. |
| commit\_sha | `run_metadata.commit_sha` | Commit the model was run from. |

Example:

```yaml
plan_code: "ns"
export_type: "versioned"
version: "v1.2"
run_datetime: "2026-10-07T14:30:00Z"
export_datetime: "2026-10-07T16:05:12Z"
total_runtime: "01:34:48"
github_user: "egouge"
github_branch: "main"
commit_sha: "633f011d5c0e4b7a9f2e8d1c3b6a5f4e7d8c9b0a"
```

### Model Data

These are exported by default, and each comes from a row in the export table:

| Export | Source | One file per | Format |
| :---- | :---- | :---- | :---- |
| [Natural barriers](#natural-barriers) | The `<output_schema>.natural_barriers_<species>` views, which are created as a part of the model processing. Each view holds the natural barriers with the columns specific to that species. See [barriers.md](./outputs/barriers.md). | species | geopackage |
| [Streams](#streams) | `<output_schema>.streams`. See [streams.md](./outputs/streams.md). | run | geopackage |
| [Ranked barriers](#ranked-barriers) | `<code>_wcrp.ranked_barriers_<species>_<lifestage>_<code>`. See [ranked_barriers.md](./outputs/ranked_barriers.md). | species/lifestage | csv |
| [Final structures](#final-structures) | `<code>_wcrp.combined_output_table_vw`. See [combined_output_view.md](./outputs/combined_output_view.md). | run | geopackage |
| [Connectivity summary](#connectivity-summary) | `<output_schema>.watershed_summary_stats`. See [watershed_summary.md](./outputs/watershed_summary.md). | run | csv |

#### Natural Barriers

One row is added to the export table for each reporting species (all lifestages are included for the species):

Example filename: `runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_natural_barriers_as.gpkg`

Example `sql` for species `as`, reporting the `spawn` and `rear` lifestages. It reads from the existing `<output_schema>.natural_barriers_<species>` view (see [barriers.md](./outputs/barriers.md)), which has already converted the JSON fields to columns:

```sql
SELECT
    id::text AS id,
    feature_id::text AS barrier_id,
    feature_type,
    passability_status_spawn,
    passability_status_rear,

    -- barrier counts
    upstream_natural_spawn_count,
    upstream_natural_rear_count,
    upstream_natural_spawnrear_count,
    upstream_anthro_spawn_count,
    upstream_anthro_rear_count,
    upstream_anthro_spawnrear_count,
    downstream_natural_spawn_count,
    downstream_natural_rear_count,
    downstream_natural_spawnrear_count,
    downstream_anthro_spawn_count,
    downstream_anthro_rear_count,
    downstream_anthro_spawnrear_count,

    -- barrier id lists, as pipe-separated text
    array_to_string(downstream_natural_spawn_ids, '|') AS downstream_natural_spawn_ids,
    array_to_string(downstream_natural_rear_ids, '|') AS downstream_natural_rear_ids,
    array_to_string(downstream_anthro_spawn_ids, '|') AS downstream_anthro_spawn_ids,
    array_to_string(downstream_anthro_rear_ids, '|') AS downstream_anthro_rear_ids,
    array_to_string(upstream_anthro_spawn_ids, '|') AS upstream_anthro_spawn_ids,
    array_to_string(upstream_anthro_rear_ids, '|') AS upstream_anthro_rear_ids,

    -- accessible lengths (always in the view)
    spawn_upstream_accessible_length,
    rear_upstream_accessible_length,

    -- spawn lengths (in the view only when spawn is reported for this species)
    spawn_upstream_length,
    spawn_functional_upstream_length,
    spawn_weighted_connected_upstream_length,
    spawn_weighted_disconnected_upstream_length,
    spawn_functional_weighted_connected_upstream_length,
    spawn_functional_weighted_disconnected_upstream_length,

    -- rear lengths (in the view only when rear is reported for this species)
    rear_upstream_length,
    rear_functional_upstream_length,
    rear_weighted_connected_upstream_length,
    rear_weighted_disconnected_upstream_length,
    rear_functional_weighted_connected_upstream_length,
    rear_functional_weighted_disconnected_upstream_length,

    -- the one geometry column, used as the geometry in the GeoPackage
    snapped_geometry AS geometry
FROM <output_schema>.natural_barriers_as
```

Notes on the example:

- The columns are listed, not selected with `*`, because the view has two geometry columns (`geometry` and `snapped_geometry`) and a GeoPackage export must return exactly one.
- GeoPackage has no uuid or list type, so the uuid columns are cast to text and the barrier id lists are written as text with the values separated by `|` (e.g. `id1|id2|id3`).
- The length columns in the view depend on the lifestages reported for the species. When `spawnrear` is reported, the view has a third block of six columns with the `spawnrear_` prefix, which is added to the query.
- The view's `feature_id` is written as `barrier_id`, the same name and meaning as `barrier_id` in the final structures export and the tracking table. It is the stable identifier of the barrier (CABD id, gradient barrier id or new structure id). `id` is also written, but it is regenerated on every model run, so it only identifies a barrier within one run.
- The view only holds natural barriers that snapped to the stream network.
- The view does not have the `source`, `upstream_edge_id` or `downstream_edge_id` columns from `all_barriers`.

#### Streams

One row is added to the export table for the run. The single file holds every stream edge with the stream network columns only; the per-species `species_stats` column is left out:

Example filename: `runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_streams.gpkg`

Example `sql`. It reads from `<output_schema>.streams` (see [streams.md](./outputs/streams.md)):

```sql
SELECT
    id::text AS id,
    aoi_id::text AS aoi_id,
    ef_type,
    ef_subtype,
    rank,
    length,
    from_nexus_id::text AS from_nexus_id,
    to_nexus_id::text AS to_nexus_id,
    ecatchment_id::text AS ecatchment_id,
    mainstem_id::text AS mainstem_id,
    graph_id,
    is_isolated,
    strahler_order,
    effective_length,
    segment_gradient,
    downstream_route_measure,
    upstream_route_measure,

    -- the one geometry column, used as the geometry in the GeoPackage
    geometry
FROM <output_schema>.streams
```

Notes on the example:

- The JSON column (`species_stats`) is left out entirely, so the file has no species-specific values (accessibility, habitat, barrier counts or weighted lengths). The same file applies to every species.
- GeoPackage has no uuid type, so the uuid columns are cast to text.
- Every stream edge is written.

#### Ranked Barriers

One row is added to the export table for each reporting species/lifestage pair:

Example filename: `runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_ranked_barriers_as_spawn.csv`

Example `sql` for plan code `ns`, species `as` and lifestage `spawn`. It reads from `<code>_wcrp.ranked_barriers_<species>_<lifestage>_<code>` (see [ranked_barriers.md](./outputs/ranked_barriers.md)):

```sql
SELECT
    barrier_id,
    group_id,
    num_barriers_group,
    total_spawn_hab_gain_group_km,
    w_total_hab_gain_group_km,
    avg_gain_per_barrier_km,
    w_avg_gain_per_barrier_km,

    -- list of group ids, as pipe-separated text
    array_to_string(downstr_group_ids, '|') AS downstr_group_ids,

    rank_w_avg_gain_tiered,
    rank_w_total_upstr_spawn_hab,
    rank_combined,
    as_spawn_passability,
    as_rear_passability
FROM ns_wcrp.ranked_barriers_as_spawn_ns
```

Notes on the example:

- The file name leaves off the plan code that ends the table name, because every file name already starts with it.
- CSV has no list type, so `downstr_group_ids` is written as text with the values separated by `|` (e.g. `12|15|31`). A comma is not used because it is the CSV column separator.
- `barrier_id` here is `all_barriers.id`. It matches `id` in the natural barriers export, not `barrier_id` in the final structures export (which is `all_barriers.feature_id`).
- The table has no geometry. All lengths and gains are in km.

#### Final Structures

One row is added to the export table for the run. The single file holds every reporting species and lifestage:

Example filename: `runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_final_structures.gpkg`

Example `sql` for plan code `ns`, with one reporting species, `as`, reporting the `spawn` lifestage. It reads from `<code>_wcrp.combined_output_table_vw` (see [combined_output_view.md](./outputs/combined_output_view.md)). The view has a large number of columns that depend on the plan, so the example is shortened; the real query lists every column of the view, in view order:

```sql
SELECT
    barrier_id::text AS barrier_id,
    feature_type,

    -- CABD dam and stream crossing attributes
    dam_name_en,
    dam_use,
    -- ... the rest of the CABD dam and stream crossing columns
    date_assessed,

    -- per species: passability and upstream lengths (km)
    as_spawn_passability,
    as_rear_passability,
    as_spawn_upstream_accessible_length_km,
    as_rear_upstream_accessible_length_km,
    as_spawn_upstream_length_km,
    -- ... the rest of the <sp>_<lc>_..._length_km columns

    -- per species/lifestage: ranking
    group_id_as_spawn,
    num_barriers_group_as_spawn,
    -- ... the rest of the ranking columns
    array_to_string(downstr_group_ids_as_spawn, '|') AS downstr_group_ids_as_spawn,
    rank_combined_as_spawn,
    label_in_wcrp_as_spawn,

    -- tracking table columns
    internal_name,
    estimated_cost_dollars,
    estimated_rehabilitation_cost_dollars,
    actual_project_cost_dollars,
    -- ... the rest of the tracking columns
    structure_list_status_as,
    partial_passability_as,
    partial_passability_notes_as,

    -- the one geometry column, used as the geometry in the GeoPackage
    snapped_geometry AS geometry
FROM ns_wcrp.combined_output_table_vw
```

Notes on the example:

- The columns are listed, not selected with `*`, because some need converting. The script that adds this row builds the list from the columns of the view.
- GeoPackage has no uuid, list or enum type, so `barrier_id` is cast to text, each `downstr_group_ids_<sp>_<lc>` list is written as text with the values separated by `|`, and any enum columns from the tracking table are cast to text.
- Three tracking table columns have a `$` in their name, which some GIS tools reject or rename when reading a GeoPackage. `combined_output_table_vw` will be updated to rename them, so the view and the export use the same names:

    | Tracking table column | Name in the view and the export |
    | :---- | :---- |
    | `estimated_cost_$` | `estimated_cost_dollars` |
    | `estimated_rehabilitation_cost_$` | `estimated_rehabilitation_cost_dollars` |
    | `actual_project_cost_$` | `actual_project_cost_dollars` |

- The view includes barriers that could not be snapped to the stream network. They are written with no geometry.
- The view reads the tracking table, which is edited by hand, so this file reflects the tracking table at the time of the export (see [Re-running an Export](#re-running-an-export)).

#### Connectivity Summary

One row is added to the export table for the run. The single file has one row per reporting species:

Example filename: `runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_connectivity_summary.csv`

Example `sql`. It reads from `<output_schema>.watershed_summary_stats` (see [watershed_summary.md](./outputs/watershed_summary.md)):

```sql
SELECT
    species,
    total_km,
    total_spawn_km,
    total_rear_km,
    total_spawnrear_km,
    connected_spawn_km,
    disconnected_spawn_km,
    connected_rear_km,
    disconnected_rear_km,
    connected_spawnrear_km,
    disconnected_spawnrear_km,
    pct_disconnected_spawn,
    pct_disconnected_rear,
    pct_disconnected_spawnrear
FROM <output_schema>.watershed_summary_stats
```

Notes on the example:

- The same columns are written for every plan.
- When the plan has no reporting species the view is not created, so no row is added to the export table and no file is written.


## Re-running an Export

The export is a separate step that only needs the database: the list of files comes from `<output_schema>.azure_exports` and the run details from `<output_schema>.run_metadata`. Both stay in the database until the next model run of that plan.

**When an export fails.** The export is the last step of the model run, after all the model results are committed. If it fails, the run is marked as failed so that it is noticed, but the model results are kept. Once the cause is fixed, the export can be re-run without re-running the model, using the export-only action (see [New: Export-Only Action](#new-export-only-action)).

**!! Results can change after the run  !!**
Some exports read from data that can be edited after the model run. The final structures view reads the [tracking table](./outputs/tracking_table.md), which is edited by hand, so an export run later can include edits made since the model ran. The export date/time in `metadata.yaml` shows when this may apply.

## Open Questions - To Confirm with CWF

- **Streams without species values (to confirm).** The JSON column (`species_stats`) is left out entirely, so the file has no species-specific values (accessibility, habitat, barrier counts or weighted lengths). The same file applies to every species.
- **Ranked barriers `barrier_id`.** `barrier_id` in the ranked barriers export is `all_barriers.id`. It matches `id` in the natural barriers export, not `barrier_id` in the final structures export (which is `all_barriers.feature_id`). See [issue #29](https://github.com/Canadian-Wildlife-Federation/fishpass/issues/29).
- **Barrier id lists in the natural barriers export.** Are the six upstream/downstream barrier id lists (e.g. `upstream_anthro_spawn_ids`, `downstream_natural_rear_ids`) needed in the export? The ids in these lists are the per-run `all_barriers.id`. The four anthropogenic lists cannot be looked up in any exported file: anthropogenic barriers are only exported in the final structures file, which identifies them by `barrier_id` (`all_barriers.feature_id`) and does not include `id`. The two natural lists can be looked up using the `id` column of the natural barriers export. If the lists are not needed they can be left out; the barrier counts are exported either way.

## Open Questions - For Implementation

- **GitHub secret names**, and which kind of credential is used (connection string, SAS token, or service principal).