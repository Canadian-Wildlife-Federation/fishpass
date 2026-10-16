# FishPass Modelling Engine: failed run of 7 October 2026

Why the asf_phase1 run failed and the options for reducing the model's memory use. Prepared 8 October 2026.

Run: [github.com/Canadian-Wildlife-Federation/fishpass/actions/runs/37663967347](https://github.com/Canadian-Wildlife-Federation/fishpass/actions/runs/37663967347)

## Summary

The run did not fail on a model error. The GitHub-hosted runner stopped responding part-way through the `Run model` step, and GitHub marked the job as failed. The most likely cause is that the model ran the runner out of memory while computing edge statistics (Steps 5-9) for the asf_phase1 plan, which is about 30 times larger than any plan that has completed on this workflow.

This is a likely cause, not a proven one: the runner died before it could upload its logs, so there is no direct record of memory use. The evidence is set out below.

Five options for reducing memory use in that phase are described below. One of them needs a decision from the team, because the simplest way to do it changes what the model writes to stream edges.

## What happened

The workflow was started manually on main (commit 633f011) for plan asf_phase1. It was attempted twice under the same run. All times are UTC on 7 October.

| | Attempt 1 | Attempt 2 (the job linked in the failure) |
|---|---|---|
| Started | 18:04 | 19:56 |
| Ended | 19:51, after 1 h 47 min | 22:18, after 2 h 22 min |
| How it ended | "The runner has received a shutdown signal." | "The hosted runner lost communication with the server." |
| Last log line | 19:29 - Step 5-9: processing 22640 connected component(s) in 44 bundle(s). Nothing was logged in the 22 minutes that followed. | None. The logs were never uploaded. |

Attempt 1 took 1 h 25 min to reach Steps 5-9. If attempt 2 kept the same pace, it spent roughly an hour in that phase before the runner was lost.

> The failed run on 6 October (branch barrier-ranking-ko) was a different failure: the model lost its database connection ("SSL connection has been closed unexpectedly") and exited with an error. It is not covered here.

## Why memory is the likely cause

GitHub's own message for attempt 2 names resource starvation as a cause:

> *"Anything in your workflow that terminates the runner process, starves it for CPU/Memory, or blocks its network access can cause this error."*

- **Both attempts died in the same phase.** Attempt 1 reached Steps 5-9, went silent, and was shut down. Attempt 2 was lost at about the time it would have been in the same phase.
- **The plan is far larger than anything that has succeeded.** asf_phase1 loaded 216 areas of interest and 10.2 million stream edges, rising to about 11.4 million after the network was broken at barriers. The last successful run (plan ns, 5 October) had 340,887 edges and finished in 13 minutes.
- **The network is dominated by a few very large connected components.** Components are packed into bundles of at most 100,000 edges. An even split of 11.4 million edges would need more than 110 bundles; there were 44. A component larger than the budget gets a bundle to itself and is loaded whole.
- **The code holds a whole component in memory at once.** Every edge is fetched into a Python dictionary, and the statistics for every edge are built and kept until the component is finished.
- **One structure grows faster than the network.** Each edge stores its own list of every impassable anthropogenic barrier upstream of it, for each species and life stage. On a large river, each edge of the main stem holds nearly the whole basin's list, and each list is then written into that edge's JSON.

A standard GitHub-hosted Linux runner has 16 GB of memory or less. The run loaded 247,187 CABD structures and added 938,394 gradient barriers.

## Options for reducing memory use

Ranked by expected effect. The ranking comes from reading the code, not from profiling it, so the sizes of the savings are estimates.

| # | Change | Expected saving | Changes model outputs? |
|---|---|---|---|
| 1 | Stop building a separate upstream barrier ID list for every edge | Largest. Removes the only part that grows faster than the network. | Depends on the variant chosen |
| 2 | Write edge statistics to the database as they are produced, and release data once used | Large on very big components | No |
| 3 | Write barrier statistics after each bundle instead of at the end of the run | Moderate | No |
| 4 | Store per-edge values in arrays instead of one dictionary per edge | Roughly 5-10 times less for what remains | No |
| 5 | Fetch edges in a leaner form | Around 1-2 GB on a multi-million-edge component | No |

### 1. Upstream barrier ID lists

While walking the network, the model joins lists of barrier IDs together at every edge, so every edge ends up with its own complete list of the impassable anthropogenic barriers upstream of it, for each species and life stage. On a big river, each edge of the main stem holds nearly the whole basin's list. Those lists are then written into every edge's species_stats JSON. This is the only part of Steps 5-9 that grows faster than the network does; the other four options are savings of a fixed proportion. There are two ways to fix it, and they differ in what the model produces.

- **Keep the outputs identical.** Store the barrier IDs once, in traversal order, and give each edge a start and end position in that list. The full list is only built at the moment a row is written. Memory use becomes proportional to the network size. The JSON written to the streams table stays as large as it is today, so write batches would need to be sized by bytes rather than by a fixed 5,000 rows.
- **Change the outputs.** Keep the upstream barrier counts on stream edges, but write the ID lists only on barriers, where barrier ranking reads them. This reduces both memory and database size, but removes `upstream_anthro_spawn_ids` and `upstream_anthro_rear_ids` from stream edges.

### 2. Write as you go

The model keeps every intermediate result for a component until the whole component has been processed, and builds the JSON for all of its edges before writing any of it. The existing 5,000-row write batching therefore does not help inside a very large component. Producing rows in batches, writing each batch straight away, and dropping intermediate data once it has been used would lower the peak.

### 3. Barrier statistics per bundle

The statistics for every barrier in the run are collected in one list and written at the very end. Each barrier row carries its own upstream ID lists, so this list grows throughout the run. It could be written after each bundle instead.

### 4. Arrays instead of dictionaries

The network-walking functions build one dictionary per edge holding about 18 values per species. Four empty lists per species are also created for every edge, although few edges have barriers. The results are then reorganised into a second set of dictionaries while the first is still in memory. Arrays indexed by edge position, with barrier data stored only for the edges that have barriers, would use far less. This is the most invasive of the five changes.

### 5. Leaner edge fetch

Each bundle's edges are read from the database in one go and then copied into one dictionary per edge, so the data is briefly held twice. Reading through a server-side cursor into compact rows would avoid that.

## Update [2026-10-16]

Items 2 (Write as you go) and 3 (Barrier statistics per bundle) were implemented which has solved the issue for the current network analysis. As networks increase in size other options may be necessary.