# FishPass Modelling Engine

Runs a model plan end-to-end: loads a stream network, barriers, and habitat data for the plan's
AOI, applies structure/habitat updates, snaps everything onto the network, and computes
per-species/lifecycle accessibility, habitat, and upstream-length statistics. See
[fishpass_docs.md](../fishpass/docs/fishpass_docs.md) for the full requirements and design
decisions behind this tool -- in particular its **Outstanding Decisions** section, which lists
known gaps/assumptions not yet validated against a real database run.

## Prerequisites

* [chyf_loader](../chyf_loader/README.md) must have already loaded `chyf_raw.flowpath`/`chyf_raw.aoi`.
* [gradient_barriers](../gradient_barriers/README.md) must have already populated
  `support.gradient_barriers`, if the plan's `structure_types` includes `gradient_barriers`.
* A model plan file at `config/models/<plan_code>.yaml` -- see
  [model_plan_file.md](../fishpass/requirements/inputs/model_plan_file.md) and
  [config/models/example.yaml](../config/models/example.yaml).
* [config/fishpass.yaml](../config/fishpass.yaml) -- natural/anthropogenic structure
  classification. Any `feature_type` not listed there (or in a plan's
  `natural_feature_types_override`) falls back to `anthropogenic`.

## Warnings

**Output schema is fully recomputed each run.** The plan's `output_schema` is dropped and
recreated from scratch every run -- nothing in it survives between runs.

**AOI-scoped runs and graph_id boundaries.** Compute Statistics partitions the network into
connected components by `graph_id` and computes upstream/downstream statistics using only the
edges already loaded into the output schema. For a plan whose AOI selection is cut by a
`graph_id` that extends into a non-requested AOI, statistics near that boundary will undercount
barriers/lengths from the excluded portion of the network. See requirements.md's Outstanding
Decisions for detail; running with `aoi: workunit: all` avoids this.

**CABD API's 50,000-feature cap.** Structure loading chunks CABD requests by work-unit subgroup
to stay under this, but a single feature type that's dense across a huge AOI selection could
still need a smaller `chunk_size` than the default -- see `cabd_client.py`.

## Running - Via GitHub Action

Run the **FishPass Modelling Engine** GitHub Action (`workflow_dispatch`, manual trigger),
supplying the `plan_code` input. Database connection details come from GitHub Actions secrets
(`FISHPASS_HOST`, `FISHPASS_PORT`, `FISHPASS_DBNAME`, `FISHPASS_USER`, `FISHPASS_PASSWORD`) --
never stored in a config file or logged.

## Local Use

### Running Locally

```powershell
.\fishpass_engine\run_local.ps1 -PlanCode myplan
```

Or directly:

```sh
export FISHPASS_HOST=... FISHPASS_PORT=... FISHPASS_DBNAME=... FISHPASS_USER=... FISHPASS_PASSWORD=...
pip install -r fishpass_engine/scripts/requirements.txt
python fishpass_engine/scripts/run_model.py myplan
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
| `model_plan.py` | Load/validate `config/models/<plan_code>.yaml` |
| `load_stream_network.py` | Initialize + Load Stream Network |
| `cabd_client.py`, `load_structures.py` | Load Structures steps 1-4, 6-7 (`load_structures.load_natural_feature_types` loads `config/fishpass.yaml`) |
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
| `db.py` | Shared DB connection/identifier-quoting helpers |
