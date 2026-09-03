# Gradient Barrier Generator

Computes gradient barriers per fish species/lifestage from the cached CHyF stream network
and writes them to `support.gradient_barriers`. Each run also creates (or appends a
row to) `support.gradient_barriers_metadata`, recording the AOI scope (`{all}` for a full run, or
the reprocessed AOI short_names for a scoped run), the fish species parameters in effect, and the
run's timestamp. The metadata table is fully replaced for a full run (all AOI's), and appended to for a AOI scoped run.

See [gradient_barriers_doc.md](docs/gradient_barriers_doc.md) for the full requirements and design decisions behind this tool.

## Prerequisite: chyf_loader

This tool reads from `chyf_raw.flowpath`, which must already be populated by the
[chyf_loader](../chyf_loader/README.md) reload pipeline before running this.


## Warnings

**Table is fully recomputed each run.** Manual edits to `actual_species`/`comments` on the live
  table don't carry forward — they only survive in the archived copy. For AOI-scoped runs, edits for the AOI in scope are lost, other edits remain.

**AOI-scoped runs and boundary mainstems.** A scoped run reads across into neighboring AOIs
  where a mainstem crosses a boundary, to keep gradients correct near the edge, but only writes
  barriers for the requested AOI(s).

**AOI boundary changes.** If `chyf_raw.aoi` polygons are redrawn, an AOI-scoped run won't
  correctly re-clear old boundary-area barriers. Do a full (unscoped) run, or manually fix up the
  `workunit` column on affected rows first.

**Partial-failure risk.** Runs commit in stages, not as one transaction — a run that fails
  partway leaves the table partially updated. Re-run (full or AOI-scoped, as appropriate) to
  recover.

## Running - Via GitHub Action (regular use)

Run the **Gradient Barriers** GitHub Action (`workflow_dispatch`, manual trigger only).

It must be run whenever the cached CHyF network or the fish species parameter file has changed. 


### Reprocessing one or more AOIs

To recompute barriers for just one or a few AOI(s) instead of the entire network, edit
[`config/gradient_barriers.yaml`](../config/gradient_barriers.yaml) and commit the change:

```yaml
aoi:
  workunit:
    - 08MF001
    - 08MG001
```

Leave `workunit` empty (or delete the file) to recompute the entire network. When one or more `workunit`(s) are set, `compute_barriers.py`
backs up the existing rows for those AOI(s), recomputes barriers for just those AOI(s), and
leaves every other AOI's barriers untouched.


## Local Use

### Running Locally

There are two script (run.sh - linux; run.ps1 - windows) to run locally. These scripts must be modified to configure the database connection parameters.

To ensure all python requirements are installed, run this first:
```sh
pip install -r gradient_barriers/scripts/requirements.txt
```

Pass `--species_params PATH` to use a species parameter file other than the repo default
(`config/fish_species_parameters.yaml`), and/or `--config PATH` to use an AOI config file other
than the repo default (`config/gradient_barriers.yaml`):

```sh
run.sh --species_params /path/to/other_parameters.yaml --config /path/to/other_gradient_barriers.yaml
```


### Running Test Cases

`tests/test_compute_barriers.py` unit-tests the gradient/interpolation logic directly (no database
or real network geometry needed) using Python's stdlib `unittest` — it stubs out `psycopg`,
`shapely`, and `yaml` if they aren't installed, since those are only needed for the DB/geometry code
paths, not the algorithm under test.

```sh
python -m unittest gradient_barriers.tests.test_compute_barriers -v
```

Run from the repo root. Requires no dependencies beyond the Python standard library.
