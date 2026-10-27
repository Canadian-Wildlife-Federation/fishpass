# FishPass automated QA suite

This read-only test runner loads the FishPass **model plan YAML** and **fish species parameter YAML** selected at runtime. It derives the plan schema, species-specific views, WCRP schema/table names, and habitat thresholds directly from those files.

It reports `PASS`, `FAIL`, `ERROR`, or `SKIP`, then writes detailed JSON and CSV reports.

## Included checks

- Accessibility and habitat consistency
- Habitat gradient and Strahler thresholds from the selected species configuration
- Stream weighted-length conservation and valid ranges
- Effective-length rules
- Functional versus total upstream-length rules
- Barrier passability calculations
- Barrier count versus ID-array cardinality
- Rear WCRP ranking consistency
- Optional satellite-review propagation checks

Manual sampling remains necessary for topology, correctness of barrier IDs, first qualifying downstream barrier selection, functional reset behaviour, and visual gradient-barrier review.

## Windows setup

Extract the ZIP and activate the same Python 3.10 environment used for FishPass:

```powershell
python -m pip install -r requirements.txt
```

The script reads:

- `FISHPASS_HOST`
- `FISHPASS_PORT`
- `FISHPASS_DBNAME`
- `FISHPASS_USER`
- `FISHPASS_PASSWORD`

## Basic ASF run

The example YAML files are included in this package:

```powershell
python .\fishpass_qa.py `
  --model-plan .\asf_phase1.yaml `
  --species-parameters .\fish_species_parameters.yaml
```

For the attached ASF configuration, the runner selects species code `as`, reads the Atlantic Salmon habitat thresholds, and derives:

- output schema: `asf_phase1`
- streams view: `streams_as`
- anthropogenic barrier view: `anthropogenic_barriers_as`
- WCRP schema: `asf_phase1_wcrp`
- rear ranking table: `ranked_barriers_as_rear_asf_phase1`

## Use different runtime configuration files

```powershell
python .\fishpass_qa.py `
  --model-plan C:\path\to\another_plan.yaml `
  --species-parameters C:\path\to\fish_species_parameters.yaml
```

If a plan has multiple target species, select one:

```powershell
python .\fishpass_qa.py `
  --model-plan .\multi_species_plan.yaml `
  --species-parameters .\fish_species_parameters.yaml `
  --species-code as
```

The run stops with a clear configuration error if required plan keys, the selected species, or required habitat thresholds are missing.

## List configured checks without connecting

```powershell
python .\fishpass_qa.py `
  --model-plan .\asf_phase1.yaml `
  --species-parameters .\fish_species_parameters.yaml `
  --list
```

## Satellite-review checks

Load the latest processed review export into `fishpass_dev` as `public.satellite_review_processed`, with at least:

- `cabd_id uuid`
- `crossing_type_code integer`
- `review_last_modified timestamp`

Then run:

```powershell
python .\fishpass_qa.py `
  --model-plan .\asf_phase1.yaml `
  --species-parameters .\fish_species_parameters.yaml `
  --include-satellite
```

Use `--satellite-schema` and `--satellite-table` to override the staging location.

## Useful options

```powershell
# Skip ranking checks
python .\fishpass_qa.py --model-plan .\asf_phase1.yaml --species-parameters .\fish_species_parameters.yaml --skip-rankings

# Return exit code 1 on failed/error checks for CI
python .\fishpass_qa.py --model-plan .\asf_phase1.yaml --species-parameters .\fish_species_parameters.yaml --fail-on-failure

# Increase each SQL check's timeout
python .\fishpass_qa.py --model-plan .\asf_phase1.yaml --species-parameters .\fish_species_parameters.yaml --statement-timeout 1200
```

Object names can be overridden with `--output-schema`, `--streams-view`, `--barriers-view`, `--wcrp-schema`, and `--ranked-rear-table` when necessary.

## Results

- `PASS`: no exceptions returned.
- `FAIL`: one or more exceptions returned.
- `ERROR`: query/configuration/permission/timeout problem.
- `SKIP`: optional checks were not enabled.

Reports are written to `qa_reports`. The JSON report records the exact plan path, species-parameter path, selected species, derived schema, reporting values, and habitat thresholds used by the run.
