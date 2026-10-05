# Decisions

## Province Territory Code Filtering

NHN Work units (AOIs) may span multiple provinces and territories. When filtering by province territory code we are going to filter on workunits, including any workunit that intersects with the provincial or territory boundary.

**Alternative**

Out of scope for now is the option to include all overlapping AOI's and all connected AOI's.

**Prerequisite**

We need to add province_territory_code varchar[] field to the chyf aoi table.

## Accessibility Computation

At one point the accessibility computation was defined as:

```
* IF (the species if valid for that edge) AND (downstream natural impassable barrier for species/lifecycle count = 0) THEN 
    * NATURALLY ACCESSIBLE 
* ELSE
    * NATURALLY INACCESSIBLE
```

The `(the species if valid for that edge)` was designed to allow certain species to be modelled only on specific workunits (or watersheds?).  It was decided to drop this requirement for the initial Phase. If a users only requires results on specific workunits (watersheds) they can filter the results of the analysis as desired.

So accessibility is defined as:

```
* IF (downstream natural impassable barrier for species/lifecycle count = 0) THEN 
    * NATURALLY ACCESSIBLE 
* ELSE
    * NATURALLY INACCESSIBLE
```

## Upstream and Downstream Barrier IDs

For upstream and downstream barrier counts and IDs, only the counts are produced for the combined `spawnrear` lifestage, not a full list of IDs. In the `spawnrear` counts, a barrier that is impassable for both spawning and rearing is only counted once.