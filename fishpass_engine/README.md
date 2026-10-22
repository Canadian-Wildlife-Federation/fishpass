# FishPass Modelling Engine

Runs a model plan end-to-end: loads a stream network, barriers, and habitat data for the plan's
AOI, applies structure/habitat updates, snaps everything onto the network, and computes
per-species/lifecycle accessibility, habitat, and upstream-length statistics and optionally export the results to Azure storage account. Every run then
ranks barriers per species/lifecycle and rebuilds the plan's WCRP combined output view. See
[fishpass_docs.md](docs/fishpass_docs.md) for the full requirements and design
decisions behind this tool -- in particular its **Outstanding Decisions** section, which lists
known gaps/assumptions not yet validated against a real database run.

## Database Setup

These scripts in [init/database](../init/database) configure a new FishPass database. Each is
run by hand, once per database; none is run by a GitHub Action or by a model run. See the comments
at the top of each script for the `psql` command and the variables it needs.

| Script | Sets up |
| --- | --- |
| [fishpass_user.sql](../init/database/fishpass_user.sql) | The `fishpass` role, its read access to the CHyF database, and its ownership of the FishPass database. A template: fill in the role, password, and database names before running. |
| [fishpass_chyf_raw_init.sql](../init/database/fishpass_chyf_raw_init.sql) | The `chyf_raw` schema and tables (`aoi`, `flowpath`, `shoreline`, `names`) that chyf_loader fills, and the `chyf2_fdw` foreign tables it reads from. |
| [fishpass_cabd_raw_init.sql](../init/database/fishpass_cabd_raw_init.sql) | The `cabd_fdw` foreign tables the combined view reads CABD attributes from. Must be run as a superuser. |
| [fishpass_support_tables.sql](../init/database/fishpass_support_tables.sql) | The `support` schema and its `structure_updates`, `new_structures`, and `habitat_updates` tables. |
| [wcrp_support.sql](../init/database/wcrp_support.sql) | The `support.blank2null()` trigger function that every WCRP tracking table uses. |

Run `fishpass_user.sql` first, since the other scripts create objects for the `fishpass` role.

## Prerequisites

* A properly configured database -- see [Database Setup](#database-setup).
* [chyf_loader](../chyf_loader/README.md) must have already loaded `chyf_raw.flowpath`/`chyf_raw.aoi`.
* [gradient_barriers](../gradient_barriers/README.md) must have already populated
  `support.gradient_barriers`, if the plan's `structure_types` includes `gradient_barriers`.
* A model plan file at `config/models/<plan_code>.yaml` -- see
  [model_plan_file.md](docs/inputs/model_plan_file.md) and
  [config/models/example.yaml](../config/models/example.yaml).
* [config/fishpass.yaml](../config/fishpass.yaml) -- natural/anthropogenic structure
  classification. Any `feature_type` not listed there (or in a plan's
  `natural_feature_types_override`) falls back to `anthropogenic`. Also holds the
  `database_roles` (owner/grant roles for WCRP objects) and `wcrp` defaults
  (`label_in_wcrp_rank_threshold`, `min_avg_gain_km` -- both overridable per plan).
* If exporting to Azure, the Azure OIDC connection setup must be configured to allow the GitHub Actions write to Azure.

## Warnings

**Output schema is fully recomputed each run.** The plan's `output_schema` is dropped and
recreated from scratch every run -- nothing in it survives between runs.

**The `<code>_wcrp` schema is persistent.** It is never dropped by a model run. Its tracking
table (`tracking_table_<code>`) holds hand-entered data and is never modified by the scripts after
creation. The `ranked_barriers_*` tables and `combined_output_table_vw` in it ARE replaced on every
model run, so don't hand-edit them.

**Every run syncs the WCRP enums and checks the tracking table.** Before anything else, the run
syncs the `support.tt_*` enum types from the `wcrp.tracking_table_enums` section of
[config/fishpass.yaml](../config/fishpass.yaml): missing types are created and new values are
added. Then the plan's tracking table is created if it doesn't exist, or skipped (left unchanged)
if it does; which one happened is reported in the GitHub Actions job summary. To add an enum
value, add it to `fishpass.yaml`; the next model run applies it.

**AOI-scoped runs and graph_id boundaries.** Compute Statistics partitions the network into
connected components by `graph_id` and computes upstream/downstream statistics using only the
edges already loaded into the output schema. For a plan whose AOI selection is cut by a
`graph_id` that extends into a non-requested AOI, statistics near that boundary will undercount
barriers/lengths from the excluded portion of the network; running with `aoi: workunit: all`
avoids this.

**CABD API's 50,000-feature cap.** Structure loading chunks CABD requests by work-unit subgroup
to stay under this, but a single feature type that's dense across a huge AOI selection could
still need a smaller `chunk_size` than the default -- see `cabd_client.py`.

## Running - GitHub Actions

### FishPass Modelling Engine

Run the **FishPass Modelling Engine** GitHub Action (`workflow_dispatch`, manual trigger). This action has the following parameters:
  * **plan_code** - the plan to execute
  * **export_to_azure** - true/false if you want the model results exported to a (publicly accessible) Azure blob storage container
  * **export_type** - versioned or unversioned (affects the Azure export filenames)
  * **version** - applies to versioned exports only, the version number for the exported results (affects the Azure export filenames)

Database & Azure connection details come from GitHub Actions secrets
(`FISHPASS_HOST`, `FISHPASS_PORT`, `FISHPASS_DBNAME`, `FISHPASS_USER`, `FISHPASS_PASSWORD`, `AZURE_STORAGE_ACCOUNT_URL`) --
never stored in a config file or logged.


### FishPass Azure Export

The **FishPass Azure Export** action exports the last model run of a plan again without re-running the model. Use it after a failed export, or to publish a reviewed unversioned run as a versioned one. Files with the same name are overwritten.

This action takes the plan_code, export_type, and version parameters as described above for the **FishPass Modelling Engine** action.

The output schema comes from the current plan file. The export-only action finds the output schema from the plan file on the branch it runs from. If the plan's output_schema has changed since the model ran, the export won't find the run and will stop with an error.

For further details see [Azure Exports](../fishpass_engine/docs/azure_exports.md).

## Running - Locally

### Warnings

If including export to Azure in local run the local exports write to the real public Azure storage account.

### Prerequisite

If you want to run the model locally and export the results to Azure you need ensure the following conditions are met before you run the scripts:

1. **Azure Permissions** - Your own Azure account needs the 'Storage Blob Data Contributor' role on the storage account. Owner or Contributor alone is not enough. It can take
   5-10 minutes to take effect.
2. **Supporting Tools** - You need the following tools installed locally
   * The Azure CLI
   * GDAL, so that `ogr2ogr` is on PATH. The export needs it to write the GeoPackage files.
3. **Azure Login** - Before running you must sign in to Azure via the CLI using `az login`.


### Running Locally

**1. Configure** 

For ALL runs, configure the run script. Set the various environment variables in the run script (`run.ps1` / `run.sh`)

**2. Run Scripts** 

**Model & Export**

powershell
```
.\fishpass_engine\run.ps1 myplan -ExportToAzure -ExportType unversioned
```
```
.\fishpass_engine\run.ps1 myplan -ExportToAzure -ExportType versioned -Version v1.2
```

bash
```
./fishpass_engine/run.sh myplan --export-to-azure --export-type unversioned
```

```
./fishpass_engine/run.sh myplan --export-to-azure --export-type versioned -version v1.2
```

**Model Only**

```powershell
.\fishpass_engine\run.ps1 -PlanCode myplan
```

Or directly:

```sh
export FISHPASS_HOST=... FISHPASS_PORT=... FISHPASS_DBNAME=... FISHPASS_USER=... FISHPASS_PASSWORD=...
pip install -r fishpass_engine/scripts/requirements.txt
python fishpass_engine/scripts/run_model.py myplan
```

**Export Only**
```
python fishpass_engine/scripts/azure_export.py myplan --export-type versioned --version v1.0
```

### Running Test Cases

Every module's algorithmic logic (passability mapping, network breaking, the two-pass graph
engine, habitat-access mainstem walks, length aggregates) is unit-tested with Python's stdlib
`unittest` against synthetic data and stubbed database cursors -- no live database or real
network geometry required.

```sh
python -m unittest discover -s fishpass_engine/tests -p "test_*.py" -v
```

## Module Layout

| Module | Phase |
| :---- | :---- |
| `run_model.py` | Entry point to fishpass modelling script. |
| `model_plan.py` | Load/validate `config/models/<plan_code>.yaml` |
| `load_stream_network.py` | Initialize + Load Stream Network |
| `cabd_client.py`, `load_structures.py` | Load Structures steps 1-4, 6-7 (the natural feature types come from `utils.natural_feature_types`) |
| `network_snap.py`, `snap_structures.py` | Load Structures step 5 (snapping) |
| `load_habitat.py` | Process Habitat |
| `network_break.py` | Compute Statistics step 2 (network breaking) |
| `compute_statistics.py` | Compute Statistics orchestrator: steps 1-4, plus driving steps 5-9 per component and populating the remaining output tables |
| `species_params.py` | Fish species parameter file loader |
| `graph_stats.py` | Core topological graph engine + steps 5-7 |
| `habitat_access.py` | Compute Statistics step 8 |
| `length_stats.py` | Compute Statistics step 9 |
| `graph_component.py` | Per-graph_id DB I/O wiring the engine together |
| `barrier_tables.py` | `natural_barriers`/`anthropogenic_barriers` views/cached feature-type tables |
| `postprocess_views.py` | Create Barrier Views (also the single source of the barrier length-field names, `barrier_length_fields`) |
| `rank_barriers.py` | Rank Barriers (`<code>_wcrp.ranked_barriers_<species>_<lifecycle>_<code>`) |
| `create_combined_view.py` | Create Combined View (`<code>_wcrp.combined_output_table_vw`) |
| `azure_export.py` | Azure export: the `azure_exports` / `run_metadata` tables, and the export itself (also the export-only CLI) |
| `create_wcrp_tracking_table.py` | Pre-run WCRP setup: `sync_wcrp_tracking_enums` (syncs the `support.tt_*` enums from `config/fishpass.yaml`) and `ensure_tracking_table` (creates the tracking table if missing) |
| `db.py` | Shared DB connection/identifier-quoting helpers, `as_role` role switching, catalog checks |
| `utils.py` | Shared non-DB helpers: repo paths, shared constants (`IDENTIFIER_RE`, `SAFE_NAME_RE`, `SNAPPED_GEOMETRY_SRID`), `setup_logging`, `config/fishpass.yaml` loaders (`get_db_roles`, `wcrp_setting`, `natural_feature_types`), run details, GitHub Actions job summary |




