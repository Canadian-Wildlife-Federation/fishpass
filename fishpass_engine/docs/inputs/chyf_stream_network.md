# CHyF Stream Network

The input for the stream network comes from the `chyf_raw.flowpath` tables in the database.

These tables should be loaded using the chyf_loader tools prior to analysis.  See chyf_loader for more details on how to populate these tables.

The input geometry data data must be LineString4D data where Z value represents the raw elevation and M value represents the smoothed elevation. 

This tool does not modify any data in the `chyf_raw` schema. A copy of the required data is made in the model's output schema and modifications are made to the copy.

