# CHyF Loader

Copies the CHyF2 stream network into the FishPass database's `chyf_raw` schema, flags isolated
stream segments, and computes segment length. See [chyf_loader_doc](docs/chyf_loader_doc.md) for the
full requirements this implements.

## Requirements

**psql**

`load.py` shells out to the `psql` command-line client, so `psql` must be installed and on the
`PATH` to run it locally (e.g. via [`run.ps1`](run.ps1) / [`run.sh`](run.sh)).


## Regular use: reload

Run the **CHyF Loader Reload** GitHub Action (`workflow_dispatch`, manual trigger only) whenever
CHyF2 data has changed and needs to be re-cached. 

### Configure AOI
To change which workunit(s) get loaded, edit `config/chyf_loader.yaml` and commit the change before running. All existing data is removed before reloading only the data for the specific AOIs.

### Warnings
**Destructive refresh:** All existing CHyF data is dropped and only the data for the AOI listed in the configure file is reloaded (not an additive action).

**Workunits are connected:** 
Users must be aware of interactions between workunit data and must ensure all appropriate workunits are loaded together; otherwise results of the analysis will not be accurate.  For example, mainstems will cross work unit boundaries; if mainstems are recomputed then all workunits must be reloaded - you cannot reload an individual work unit or the mainstems will not be contiguous across the boundaries. Additionally if you exclude connected workunits, `is_isolated` may not be computed correctly.


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
