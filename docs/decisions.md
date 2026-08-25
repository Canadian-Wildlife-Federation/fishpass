# Decisions

## Province Territory Code Filtering

NHN Work units (AOIs) may span multiple provinces and territories. When filtering by province territory code we are going to filter on workunits, including any workunit that intersects with the provincial or territory boundary.

**Alternative**
Out of scope for now is the option to include all overlapping AOI's and all connected AOI's.

### Prerequiste

We need to add province_territory_code varchar[] field to the chyf aoi table.