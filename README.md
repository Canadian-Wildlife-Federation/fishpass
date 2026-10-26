# fishpass
Fish passage and connectivity modelling for Canadian watersheds

FishPass models how barriers -- dams, culverts, waterfalls, and other natural or anthropogenic
structures -- limit fish species from reaching habitat throughout a stream network. Given a
stream network, a catalogue of barriers, and per-species/lifestage passability parameters, it
computes which stream segments are naturally accessible to which species, and the resulting
habitat and upstream-length statistics per species/lifecycle. Runs are scoped to one or more
AOIs (work units) and executed via GitHub Actions against a shared PostgreSQL database.

The pipeline has three components, run in order:

1. **[chyf_loader](chyf_loader/README.md)** -- Copies the CHyF2 stream network into the FishPass
   database's `chyf_raw` schema, flags isolated stream segments, and computes segment length.
2. **[gradient_barriers](gradient_barriers/README.md)** -- Computes gradient barriers per fish
   species/lifestage from the cached CHyF stream network and writes them to
   `support.gradient_barriers`. Requires chyf_loader to have already run.
3. **[fishpass_engine](fishpass_engine/README.md)** -- Runs a model plan end-to-end: loads the
   stream network, barriers, and habitat data for the plan's AOI, applies structure/habitat
   updates, snaps everything onto the network, and computes per-species/lifecycle accessibility,
   habitat, and upstream-length statistics. It then ranks barriers per species/lifecycle and
   rebuilds the plan's WCRP combined output view. Requires chyf_loader (and gradient_barriers,
   if the plan uses gradient barriers) to have already run.

## FishPass Architecture 
```mermaid
---
config:
  layout: elk
  look: neo
---
flowchart TD
    %% 1. Define Style Classes
    classDef classBlack fill:#ffffff,stroke:#000,stroke-width:1px,color:#000;
    classDef classYellowBox fill:#FFF2CC,stroke:#E4A11B,stroke-width:1px,color:#000;

    %% 2. Nodes & Subgraphs
    DEM[DEM]
    
    CHyF_Proc(<b>CHyF Elevation Processor</b><br/><small>Adds raw elevation and computes smoothed elevation for CHyF network</small>)

    CHyF_DB[(<b>CHyF DB</b><br/><small>with mainstems, raw elevation, network groups, to/from nexus ids</small>)]

    Loader(<b>FishPass - CHyF Loader</b><br/><small>Loads the CHyF network into FishPash DB and does any initial preprocessing.</small>)

    subgraph FishPass_DB [FishPass DB]
        CHyF_Data[Processed CHyF Data]
        Gradient_Barriers[Gradient Barriers]
        Output_Schema[<b>&lt;plan&gt;</b><br/>Output Schema]
    end

    Generator(<b>FishPass - Model Generator</b><br/><small>Using the prepocessed CHyF data and other input datasets, runs the fish pass modelling. Resulting models are versioned, exported, and placed on cloud storage.</small>)
    Barrier_Generator(<b>FishPass - Gradient Barrier Generator</b><br/><small>Uses the CHyF network to compute gradient barriers.</small>)
    
    CABD[CABD API]
    Cloud[Cloud Storage<br/><i><small>model exports</small></i>]

    %% 3. Connections
    DEM --> CHyF_Proc
    CHyF_DB <--> CHyF_Proc
    CHyF_DB --> Loader
    
    Loader --> CHyF_Data
    CHyF_Data --> Generator
    CHyF_Data --> Barrier_Generator
    Barrier_Generator --> Gradient_Barriers
    Output_Schema <--> Generator
    Gradient_Barriers --> Generator
    
    CABD --> Generator
    Generator --> Cloud

    %% 4. Apply Styling
    class DEM,CHyF_DB,CABD,Cloud,CHyF_Data,Output_Schema,Gradient_Barriers classBlack
    class CHyF_Proc,Loader,Generator,Barrier_Generator classYellowBox
    style FishPass_DB fill:#ffffff,stroke:#000,stroke-width:1px;


```

## Development

Code formatting and linting are handled by [ruff](https://docs.astral.sh/ruff/), pinned in
`pyproject.toml`. Install it with:

```bash
pip install --group dev
```

(requires pip 25.1+; on an older pip, install the pinned version directly with
`pip install ruff==0.16.8` instead).

Then, from the repo root:

```bash
ruff format .   # format all files
ruff check .    # lint all files
```

A [pre-commit](https://pre-commit.com/) hook is configured to run both automatically on
`git commit`, only against staged files. After installing the dev dependencies, enable it once
per clone with:

```bash
pre-commit install
```
