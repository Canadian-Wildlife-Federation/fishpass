# CHyF Loader
This tool copies the stream network from CHyF, preprocesses it, and caches those results in the fishpass database.  This step will need to be re-run anytime the CHyF networks change (or you need to expand the area of interest), but will not need to be re-run to re-run fishpass models.

After this step is complete the FishPass database will have a copy of the CHyF network, preprocessed and ready for use as input to the fishpass modelling process.

## Running

This is run via a GitHub action, triggered by `workflow_dispatch` (manual).

## Inputs

### Stream Network 

#### CHYF2 Database

* chyf2.eflowpath 
* chyf2.eflowpath_properties

This will load required data from CHyF2 database into the chyf_raw schema using the foreign data wrappers, and apply any additional processing required 
(removal of isolated watersheds, etc).



#### Configuration File
* GitHub
* config/chyf_loader.yaml

Contains any support parameters required for preprocessing including the AOI.

Data will be managed by workunit (aoi). Currently we are only working in Atlantic Canada and for the purposes of space and performance we only want to cache data from those workunits. 

You can reload an individual workunit by only specifying that workunit in the config file.  All existing cached data for a workunit(s) will be removed before copying over new data.

Source and Target Database configurations should live in github secrets.  Not publicly available config or other files.

### Fishpas Database Connection

The database connection uses the `FISHPASS_HOST/PORT/DBNAME/USER/PASSWORD` environment variable / GitHub secrets. Connection details are never stored in config files and never logged.


## Outputs
Writes to the FishPass database chyf_raw schema, populating the schema with the CHyF stream network and aoi. We can merge the flowpath and flowpath_properties table into a single table in this database. 

We will load all primary (rank = 1) CHyF stream edges with an ef_type != 2 (bank skeleton edges).

The following additional attributes will be added to all CHyF stream segments:
* is_isolated - true/false 
* length_km - the length of the stream segment in km

Isolated Stream Segments are defined as any stream segment that does not flow into the coastline or AOI boundary.

## Process
This script will use FDW to load required data (include all flowpath attributes) from the CHyF2 database into the chyf_raw schema.

It runs `chyf_loader/scripts/load.py`, which:

1. Reads the workunit(s) to reload from [`config/chyf_loader.yaml`](../config/chyf_loader.yaml)
   (`workunits`).
2. Resolves those `short_name`s to `chyf2.aoi.id` UUIDs.
3. Deletes all existing cached `chyf_raw` data (for all AOI).
4. Copies the corresponding `aoi`, `shoreline` and (merged) `eflowpath` + `eflowpath_properties` rows from
   CHyF2 via FDW, filtered to `rank = 1` and `ef_type != 2`.
5. Computes `length_km` for the newly loaded rows.
6. Computes `is_isolated` for the newly loaded rows.
    * This is used to identify isolated groups which are flagged and excluded from used in fish pass modelling

Database connection details for both CHyF2 (source) and FishPass (target) come from GitHub
Actions secrets and are injected as environment variables. They are never stored in the config file or logged.


**WARNINGS:** 

**Data is Replaced** Each run will drop all existing CHyF data cached in the FishPass database and reload it from CHyF.

**WorkUnit Interactions**
Users must be aware of interactions between workunit data and must ensure all appropriate workunits are loaded together; otherwise results of the analysis will not be accurate.  For example, mainstems will cross work unit boundaries; if mainstems are recomputed then all workunits must be reloaded - you cannot reload an individual work unit or the mainstems will not be contiguous across the boundaries.

# CHYF 2 Database Schema

```
CREATE TABLE chyf2.eflowpath (
	id uuid NOT NULL,
	nid varchar(32) NULL,
	ef_type int2 NOT NULL,
	ef_subtype int2 NULL,
	"rank" int2 NOT NULL,
	length float8 NOT NULL,
	rivernameid1 uuid NULL,
	aoi_id uuid NULL,
	from_nexus_id uuid NULL,
	to_nexus_id uuid NULL,
	ecatchment_id uuid NULL,
	geometry public.geometry NOT NULL,
	rivernameid2 uuid NULL,
	geom_bak public.geometry NULL,
	CONSTRAINT eflowpath_pkey PRIMARY KEY (id),
	CONSTRAINT eflowpath_aoi_id_fkey FOREIGN KEY (aoi_id) REFERENCES chyf2.aoi(id),
	CONSTRAINT eflowpath_ecatchment_id_fkey FOREIGN KEY (ecatchment_id) REFERENCES chyf2.ecatchment(id),
	CONSTRAINT eflowpath_ef_subtype_fkey FOREIGN KEY (ef_subtype) REFERENCES chyf2.ef_subtype_codes(code),
	CONSTRAINT eflowpath_ef_type_fkey FOREIGN KEY (ef_type) REFERENCES chyf2.ef_type_codes(code),
	CONSTRAINT eflowpath_from_nexus_id_fkey FOREIGN KEY (from_nexus_id) REFERENCES chyf2.nexus(id),
	CONSTRAINT eflowpath_rivernameid1_fkey FOREIGN KEY (rivernameid1) REFERENCES chyf2.names(name_id),
	CONSTRAINT eflowpath_rivernameid2_fkey FOREIGN KEY (rivernameid2) REFERENCES chyf2.names(name_id),
	CONSTRAINT eflowpath_to_nexus_id_fkey FOREIGN KEY (from_nexus_id) REFERENCES chyf2.nexus(id)
);
CREATE INDEX eflowpath_aoi_id_idx ON chyf2.eflowpath USING btree (aoi_id);
CREATE INDEX eflowpath_from_nexus_id_idx ON chyf2.eflowpath USING btree (from_nexus_id);
CREATE INDEX eflowpath_geometry_idx ON chyf2.eflowpath USING gist (geometry);
CREATE INDEX eflowpath_to_nexus_id_idx ON chyf2.eflowpath USING btree (to_nexus_id);


CREATE TABLE chyf2.eflowpath_properties (
	id uuid NOT NULL,
	strahler_order int4 NULL,
	graph_id int4 NULL,
	mainstem_id uuid NULL,
	max_uplength float8 NULL,
	hack_order int4 NULL,
	horton_order int4 NULL,
	mainstem_seq int4 NULL,
	shreve_order int4 NULL,
	CONSTRAINT eflowpath_properties_pkey PRIMARY KEY (id)
);

CREATE TABLE chyf2.aoi (
	id uuid NOT NULL,
	short_name varchar NULL,
	full_name varchar NULL,
	geometry public.geometry(polygon, 4617) NULL,
	display_status int2 DEFAULT 0 NULL,
	CONSTRAINT aoi_pkey PRIMARY KEY (id),
	CONSTRAINT aoi_short_name_key UNIQUE (short_name)
);
```
