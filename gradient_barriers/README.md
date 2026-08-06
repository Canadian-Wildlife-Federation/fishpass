# Gradient Barrier Generator

Computes gradient barriers per fish species/lifestage from the cached CHyF stream network
(`chyf_raw.flowpath`), and writes them to `support.gradient_barriers`. See
[requirements.md](requirements.md) (including its "Design Decisions" section) for the full
requirements this implements.

## Prerequisite: chyf_loader

This tool reads from `chyf_raw.flowpath`, which must already be populated by the
[chyf_loader](../chyf_loader/README.md) reload pipeline before running this.

## Regular use

Run the **Gradient Barriers** GitHub Action (`workflow_dispatch`, manual trigger only) whenever
the cached CHyF network or the fish species parameter file has changed. It runs
`gradient_barriers/scripts/compute_barriers.py`, which:

1. Loads species/lifestage gradient thresholds from
   [`config/fish_species_parameters.yaml`](../config/fish_species_parameters.yaml) (override with
   `--species_params`).
2. Archives any existing `support.gradient_barriers` table to
   `support.gradient_barriers_archive_<yyyymmdd>_<seq>`, then creates a fresh one.
3. Walks every mainstem in `chyf_raw.flowpath`, computing the gradient at each vertex against the
   point 100m upstream on the same mainstem (skipping vertices without 100m of upstream mainstem
   available).
4. Flags each vertex whose gradient exceeds a species/lifestage's `accessibility_gradient_*_max`
   as a barrier for that species/lifestage, and inserts the resulting rows.
5. Assigns each barrier point's `workunit` array via a spatial join against `chyf_raw.aoi`.

Database connection details come from GitHub Actions secrets and are injected as environment
variables (`FISHPASS_HOST`, `FISHPASS_PORT`, `FISHPASS_DBNAME`, `FISHPASS_USER`,
`FISHPASS_PASSWORD`) — never stored in a config file or logged.

## Local run

```sh
export FISHPASS_HOST=... FISHPASS_PORT=... FISHPASS_DBNAME=... FISHPASS_USER=... FISHPASS_PASSWORD=...
pip install -r gradient_barriers/scripts/requirements.txt
python gradient_barriers/scripts/compute_barriers.py
```

Pass `--species_params PATH` to use a species parameter file other than the repo default
(`config/fish_species_parameters.yaml`):

```sh
python gradient_barriers/scripts/compute_barriers.py --species_params /path/to/other_parameters.yaml
```

## Tests

`tests/test_compute_barriers.py` unit-tests the gradient/interpolation logic directly (no database
or real network geometry needed) using Python's stdlib `unittest` — it stubs out `psycopg2`,
`shapely`, and `yaml` if they aren't installed, since those are only needed for the DB/geometry code
paths, not the algorithm under test.

```sh
python -m unittest gradient_barriers.tests.test_compute_barriers -v
```

Run from the repo root. Requires no dependencies beyond the Python standard library.

## WARNING: table is fully recomputed every run

Each run recomputes gradient barriers for the *entire* cached network and replaces
`support.gradient_barriers` outright (after archiving). Any manual edits made directly to the live
table (e.g. via `actual_species` or `comments`) will not carry forward automatically — they live
only in the archived copy of the table from before the run.
