# CHyF Loader

Copies the CHyF2 stream network into the FishPass database's `chyf_raw` schema, flags isolated
stream segments, and computes segment length. See [chyf_loader_doc](docs/chyf_loader_doc.md) for the
full requirements this implements.

## Requirements

**psql**

`load.py` shells out to the `psql` command-line client, so `psql` must be installed and on the
`PATH` to run it locally (e.g. via [`run.ps1`](run.ps1) / [`run.sh`](run.sh)).


## One-time setup

 `init/database/chyf_raw_init.sql`

Before the reload pipeline can be used, the `chyf_raw` schema, its tables, and the FDW
connection to CHyF2 must exist on the target FishPass database. Run this manually (it is not
part of any GitHub Action):

```
psql "host=<fishpass-host> port=<port> dbname=<dbname> user=<user>" \
  -v chyf2_host='<chyf2-host>' -v chyf2_port='5432' -v chyf2_dbname='chyf2' \
  -v chyf2_user='<chyf2-user>' -v chyf2_password='<chyf2-password>' \
  -f init/database/chyf_raw_init.sql
```

All statements in that script are idempotent (`IF NOT EXISTS`), so it's safe to re-run, e.g.
after a schema change.

## Regular use: reload

Run the **CHyF Loader Reload** GitHub Action (`workflow_dispatch`, manual trigger only) whenever
CHyF2 data has changed and needs to be re-cached. It runs `chyf_loader/scripts/load.py`, which:

1. Reads the workunit(s) to reload from [`config/chyf_loader.yaml`](../config/chyf_loader.yaml)
   (`workunits`).
2. Resolves those `short_name`s to `chyf2.aoi.id` UUIDs.
3. Deletes all existing cached `chyf_raw` data.
4. Copies the corresponding `aoi`, `shoreline` and (merged) `eflowpath` + `eflowpath_properties` rows from
   CHyF2 via FDW, filtered to `rank = 1` and `ef_type != 2`.
5. Computes `length_km` for the newly loaded rows.
6. Computes `is_isolated` for the newly loaded rows.

Database connection details for both CHyF2 (source) and FishPass (target) come from GitHub
Actions secrets and are injected as environment variables. They are never stored in the config file or logged.

To change which workunit(s) get reloaded, edit `config/chyf_loader.yaml` and commit the change
(see [`chyf_loader.yaml.example`](../config/chyf_loader.yaml.example) for the documented format).

## WARNING: workunits are connected

Users must be aware of interactions between workunit data and must ensure all appropriate workunits are loaded together; otherwise results of the analysis will not be accurate.  For example, mainstems will cross work unit boundaries; if mainstems are recomputed then all workunits must be reloaded - you cannot reload an individual work unit or the mainstems will not be contiguous across the boundaries. Additionally if you exclude connected workunits, `is_isolated` may not be computed correctly.

