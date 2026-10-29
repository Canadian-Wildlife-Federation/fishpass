# Model Exports

This document outlines the requirements for exporting FishPass engine results from the database to Azure Blob Storage. There is also an option to run the scripts locally and export to a local filestore.

## Overview

At the end of a model run, the results can optionally be exported to an Azure Blob Storage container. The export:

1. is turned on per run from the FishPass GitHub action;
2. writes to a container named after the model plan code;
3. writes every file for a run into its own folder, either versioned or unversioned;
4. is driven by a table in the database (`<output_schema>.azure_exports`) that lists what to export, so the list of exports can change without changing the export code;
5. can be [re-run on its own](#re-running-an-export) without re-running the model, because everything it needs is saved in the database.

## Azure Connection

The export signs in to Azure with OIDC (workload identity federation), so no Azure password or key is stored:

- **GitHub actions** run in the `azure-export` GitHub environment, which the Azure app registration trusts. An `azure/login` step signs in just before the export. The run action signs in after the model has finished, because the sign-in lasts about an hour and a model run can take longer.
- **Local runs** use the developer's own `az login`.

The app registration (and each developer who exports locally) needs the **Storage Blob Data Contributor** role on the storage account. The storage account and app registration IDs are stored as secrets on the `azure-export` environment:

| Secret | Value |
| :---- | :---- |
| `AZURE_CLIENT_ID` | Application (client) ID of the app registration |
| `AZURE_TENANT_ID` | Directory (tenant) ID |
| `AZURE_SUBSCRIPTION_ID` | Subscription ID |
| `AZURE_STORAGE_ACCOUNT_URL` | `https://<account>.blob.core.windows.net` |

The `FISHPASS_*` database secrets are passed to the run as before. See the [README](../README.md#azure-export) for the setup steps.

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

**The output schema comes from the current plan file.** The action finds the output schema from `config/models/<plan_code>.yaml` on the branch it is run from, not from `run_metadata`; only the plan file contents written to the export come from `run_metadata`. If the plan's `output_schema` has changed since the model ran, the export looks in the new schema. It stops with an error, either because the tables are missing or because that schema's `run_metadata` is for a different plan, and it cannot find the old run. To re-export that run, run the export-only action from a branch or commit where `output_schema` still has its old value, or re-run the model.

## Azure Storage Location

### Storage Account

A new "fishpass" Storage account will be created in the CWF Azure subscription to hold all this data. In order to enable public access to results, this account will be configured to allow anaoymous access.

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



**Public Access Permission**

The containers will be configured to allow "Blob anonymous access" to allow the exports to be publicily available via a download link. 

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
runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_streams_as.gpkg
runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_metadata.yaml
runs/unversioned/ns_20261007143000/ns_20261007143000_streams_as.gpkg
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
| uuid | `feature_id` | `feature_id::text AS feature_id` |
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
| [Streams](#streams) | The `<output_schema>.streams_<species>` views, which are created as a part of the model processing. Each view holds the stream edges with the columns specific to that species. See [streams.md](./outputs/streams.md). | species | geopackage |
| [Ranked barriers](#ranked-barriers) | `<code>_wcrp.ranked_barriers_<species>_<lifestage>_<code>`. See [ranked_barriers.md](./outputs/ranked_barriers.md). | species/lifestage | csv |
| [Structures](#structures) | `<code>_wcrp.combined_output_table_vw`. See [combined_output_view.md](./outputs/combined_output_view.md). | run | geopackage |
| [Connectivity summary](#connectivity-summary) | `<output_schema>.watershed_summary_stats`. See [watershed_summary.md](./outputs/watershed_summary.md). | run | csv |

#### Natural Barriers

One row is added to the export table for each reporting species (all lifestages are included for the species):

Example filename: `runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_natural_barriers_as.gpkg`

Example `sql` for species `as`, reporting the `spawn` and `rear` lifestages. It reads from the existing `<output_schema>.natural_barriers_<species>` view (see [barriers.md](./outputs/barriers.md)), which has already converted the JSON fields to columns:

```sql
SELECT
    feature_id::text AS feature_id,
    feature_type,
    stream_name_en,
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

    -- anthropogenic barrier id lists, as pipe-separated text
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
- `feature_id` is the barrier identifier (CABD id, gradient barrier id or new structure id), the same as `feature_id` in the ranked barriers and structures exports and the tracking table. It is stable across model runs for CABD barriers and new structures; gradient barrier ids change whenever the gradient barriers table is recomputed. The per-run `all_barriers.id` is not in the view and is not exported.
- Only the four anthropogenic barrier id lists are exported. The view's natural barrier id lists (`downstream_natural_spawn_ids`, `downstream_natural_rear_ids`) are left out; the natural barrier counts are still exported.
- The anthropogenic id lists hold `feature_id` values, which can be looked up in the structures export.
- The view only holds natural barriers that snapped to the stream network.
- `stream_name_en` is the name of the stream edge immediately upstream of the barrier (see [barriers.md](./outputs/barriers.md)).

#### Streams

One row is added to the export table for each reporting species (all lifestages are included for the species):

Example filename: `runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_streams_as.gpkg`

Example `sql` for species `as`, reporting the `spawn` and `rear` lifestages. It reads from the existing `<output_schema>.streams_<species>` view (see [streams.md](./outputs/streams.md)), which has already converted the JSON fields to columns:

```sql
SELECT
    id::text AS id,
    length,
    strahler_order,
    effective_length,
    segment_gradient,
    stream_name_en,

    -- accessibility and habitat
    spawn_accessibility,
    rear_accessibility,
    spawn_habitat,
    rear_habitat,
    spawnrear_habitat,

    -- barrier counts
    upstream_anthro_spawn_count,
    upstream_anthro_rear_count,
    upstream_anthro_spawnrear_count,
    downstream_anthro_spawn_count,
    downstream_anthro_rear_count,
    downstream_anthro_spawnrear_count,
    upstream_natural_spawn_count,
    upstream_natural_rear_count,
    upstream_natural_spawnrear_count,
    downstream_natural_spawn_count,
    downstream_natural_rear_count,
    downstream_natural_spawnrear_count,

    -- anthropogenic barrier id lists, as pipe-separated text
    array_to_string(upstream_anthro_spawn_ids, '|') AS upstream_anthro_spawn_ids,
    array_to_string(upstream_anthro_rear_ids, '|') AS upstream_anthro_rear_ids,
    array_to_string(downstream_anthro_spawn_ids, '|') AS downstream_anthro_spawn_ids,
    array_to_string(downstream_anthro_rear_ids, '|') AS downstream_anthro_rear_ids,

    -- weighted lengths (in the view only when the lifestage is reported for this species)
    spawn_weighted_length,
    spawn_weighted_connected_length,
    spawn_weighted_disconnected_length,
    rear_weighted_length,
    rear_weighted_connected_length,
    rear_weighted_disconnected_length,

    -- the one geometry column, used as the geometry in the GeoPackage
    geometry
FROM <output_schema>.streams_as
```

Notes on the example:

- GeoPackage has no uuid or list type, so `id` is cast to text and the barrier id lists are written as text with the values separated by `|`. The lists hold barrier `feature_id` values, which can be looked up in the structures export.
- The weighted length columns in the view depend on the lifestages reported for the species: the `spawn_` block is only there when `spawn` is reported, and the `rear_` block only when `rear` is reported. There is no `spawnrear_` weighted length block.
- The view only holds the stream edges that have statistics for the species, so the files for different species can have different edges.
- The view only has some of the `streams` columns (`id`, `length`, `strahler_order`, `effective_length`, `segment_gradient`, `stream_name_en` and `geometry`). The other network columns (e.g. `aoi_id`, `ef_type`, `rank`, `mainstem_id`, `graph_id`) are not in the view and so are not exported.

#### Ranked Barriers

One row is added to the export table for each reporting species/lifestage pair:

Example filename: `runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_ranked_barriers_as_spawn.csv`

Example `sql` for plan code `ns`, species `as` and lifestage `spawn`. It reads from `<code>_wcrp.ranked_barriers_<species>_<lifestage>_<code>` (see [ranked_barriers.md](./outputs/ranked_barriers.md)):

```sql
SELECT
    feature_id,
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
- `feature_id` is `all_barriers.feature_id`, so the file joins to the structures export and the tracking table on `feature_id`.
- The table has no geometry. All lengths and gains are in km.

#### Structures

One row is added to the export table for the run. The single file holds every reporting species and lifestage:

Example filename: `runs/versioned/ns_v1.2_20261007143000/ns_v1.2_20261007143000_structures.gpkg`

Example `sql` for plan code `ns`, with one reporting species, `as`, reporting the `spawn` lifestage. It reads from `<code>_wcrp.combined_output_table_vw` (see [combined_output_view.md](./outputs/combined_output_view.md)). The view has a large number of columns that depend on the plan, so the example is shortened; the real query lists every column of the view, in view order:

```sql
SELECT
    feature_id::text AS feature_id,
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
- GeoPackage has no uuid, list or enum type, so `feature_id` is cast to text, each `downstr_group_ids_<sp>_<lc>` list is written as text with the values separated by `|`, and any enum columns from the tracking table are cast to text.
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

The output schema itself is found from the current plan file. If the plan's `output_schema` has changed since the model ran, see the note in [New: Export-Only Action](#new-export-only-action).

**When an export fails.** The export is the last step of the model run, after all the model results are committed. If it fails, the run is marked as failed so that it is noticed, but the model results are kept. Once the cause is fixed, the export can be re-run without re-running the model, using the export-only action (see [New: Export-Only Action](#new-export-only-action)).

**!! Results can change after the run  !!**
Some exports read from data that can be edited after the model run. The structures view reads the [tracking table](./outputs/tracking_table.md), which is edited by hand, so an export run later can include edits made since the model ran. The export date/time in `metadata.yaml` shows when this may apply.

## Local File Exports

It is possible to run the export script locally, and write the results to the local machine instead of Azure. 

This is useful for testing the exports or reviewing the results without uploading them. It writes the same files as the Azure export, from the same [export table](#export-table) and [run metadata table](#model-run-metadata-table), with the same formats and file names.

- **GDAL is required** (`ogr2ogr`) is required on the path to write the GeoPackages.
- Local export is for local runs only. It is refused when running in GitHub Actions, so the GitHub actions always export to Azure.
- Only one of the Azure and local exports can be used per run.
- No Azure sign-in or `AZURE_STORAGE_ACCOUNT_URL` is needed. 
- The export type and version inputs work the same way as for the Azure export.

Details on running local exports are found in [README.md](../README.md). 


## Open Questions

- **Container public access under OIDC.** The export creates a missing container with anonymous blob access while signed in with the Storage Blob Data Contributor role. This has not been tested yet and may need more than that role. If it fails, either create each plan's container in the portal (public access level: Blob) or give the app registration a role that can change container settings.
