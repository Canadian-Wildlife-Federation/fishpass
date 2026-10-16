# FishPass Engine: Runner Memory and Compute Options

## Findings

### Initial Error - The asf_phase1 failure (run 37663967347)

- Likely cause: the runner ran out of memory while processing bundle 1, the giant connected component, which gets a bundle to itself.
- Memory hotspots in Steps 5–9:
  - `propagate_upstream_multi` gives each edge its own list of every upstream anthropogenic barrier ID. This grows quadratically on large rivers.
  - Per-edge dicts throughout.
  - The whole component's stats JSON was built before any rows were written.
  - Barrier rows were held for the whole run.
- Analysis reported: [fishpass_engine_run_failure_2026-10-07.md](./fishpass_engine_run_failure_2026-10-07.md)

### Fixes - Memory changes (PR #31, merged into main)

- Edge stats are serialised and written as they are produced. Each write batch is capped at 5,000 rows or about 100 MB of JSON, whichever comes first.
- Barrier stat tables are written after each bundle instead of at the end of the run.
- A component's fetched rows are released once it has been processed.
- Fixes 2 & 3 from  [fishpass_engine_run_failure_2026-10-07.md](./fishpass_engine_run_failure_2026-10-07.md)

### Results - First large run on the new code (run 38939410574)

- **Succeeded.** The model took 247m44s, and the job took 4h08m.
- 22,640 connected components in 44 bundles; 1,141,299 new segments; 1,180,876 barriers.
- The repo is public, so standard runners (4 vCPU, 16 GB) are free.

| Phase | Approx. time | Where it runs |
|---|---|---|
| Setup, load network, structures, habitat, Break Network | ~55 min | mostly database |
| Effective length and segment gradient | ~51 min | database |
| Steps 5–9 (most of it in the first ~30 bundles) | ~1h51m | runner (Python) |
| Index rebuild, ranking, views | ~26 min | database |

## Known Risks

1. **Memory headroom is unknown.** The run didn't log memory use, so we don't know how close it came to 16 GB. A larger plan could still fail.
2. **6-hour job limit.** GitHub cancels jobs on hosted runners, standard or larger, after 6 hours. This run left about 1h50m of headroom. Self-hosted runners allow up to 5 days per job.
3. **Paying for waiting.** About 1h50m of the run is SQL on the database server. A bigger runner doesn't speed that up, but every paid option still bills for it.

### Notes - Measuring memory on a runner

GitHub doesn't report memory for hosted runners. Job summaries and artifacts are uploaded at the end of a job, so a lost runner loses them; only streamed log lines (and anything written to the database) survive. Options include:

1. **Log memory in the "Run model" step** (easiest). This also prints the runner's total memory, and you can watch the `[mem]` lines live in the Actions UI:

   ```yaml
   - name: Run model
     run: |
       free -h
       (
         while true; do
           echo "[mem] $(date +%T) used=$(free -m | awk '/Mem:/{print $3}')MB avail=$(free -m | awk '/Mem:/{print $7}')MB swap=$(free -m | awk '/Swap:/{print $3}')MB python_rss=$(ps -C python -o rss= | awk '{s+=$1} END {print int(s/1024)}')MB"
           sleep 30
         done
       ) &
       python fishpass_engine/scripts/run_model.py "${{ inputs.plan_code }}"
   ```

2. **Log memory from Python.** Add `VmRSS` and `VmHWM` (current and peak) from `/proc/self/status` to the existing per-bundle progress messages. This ties memory to a specific bundle.
3. **Write it to the database** every 30 seconds or at each bundle. This is the only option that keeps the data if the runner disappears without warning.
4. **Telemetry actions** such as `catchpoint/workflow-telemetry-action`. They chart CPU, memory and disk in the job summary, but only for jobs that finish.
5. **`/usr/bin/time -v`** prints "Maximum resident set size" at the end. Use it to size a machine when running locally or on a VM; it gives nothing on a run that fails.

## Other Options (if a standard runner isn't enough)

### Software Update Options 

Reduce memory or move the work

| Option | Effort | Notes |
|---|---|---|
| Add swap to the hosted runner | Very low | Add a step before "Run model": `sudo fallocate -l 16G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`. It's slow if the job pages heavily, but it may get a run that is slightly over the limit through. |
| Change the algorithm (option 1 in [the failure report](fishpass_engine_run_failure_2026-10-07.md)) | Medium–high | Store upstream barrier sets in a form that doesn't grow quadratically, or split the giant component into sub-basins and pass results across the boundaries. Running jobs in parallel doesn't help, because the failure is inside a single component. |


### Bigger or different Runner 

Options include:

1. GitHub larger runner (arm64 or x64)
2. Self-hosted Azure VM, started and stopped by the workflow
3. Azure Container Apps Jobs (Dedicated E-series)
4. Existing office server or workstation as a self-hosted runner

#### 1. GitHub larger runners

- **Setup:** Change `runs-on:` in the workflow to the runner's label; nothing else changes.
- **Billing:** Per minute while a job runs, at a rate set by the runner's core count; arm64 runners are cheaper than x64. Memory isn't billed separately; RAM is about 4 GB per core, so you get more memory by buying more cores. (That's my assumption from how GitHub has sized these runners; the pricing page doesn't say.)
- **Advantages:** Least work: one line in the workflow. Nothing to maintain, and logs stay in the Actions UI.
- **Other:**
  - requires GitHub Team or Enterprise Cloud on the org.
  - still capped at 6 hours per job.
  - Unused cores: Steps 5–9 are single-threaded, so most of the extra cores sit idle. You're buying them for the RAM.

#### 2. Self-hosted Azure VM, started and stopped per run

- **Setup:** Create a memory-optimized Linux VM: E4as_v5 (4 vCPU / 32 GiB), E8as_v5 (8 / 64 GiB) or E16s_v5 (16 / 128 GiB). Install the GitHub runner agent (once; it updates itself) and the Python environment, or run the job in a container on the VM Change the workflow on the job to start vm, run the action, then deallocate the vm.    
- **Billing:** Per hour (billed per second) for the VM size while it is running. Monthly for the OS disk, paid even while the VM is deallocated.
- **Advantages:** Likely cheapest paid option. Removes the 6-hour limit. Packages stay installed between runs, so runs start faster. If the database is in Azure, putting the VM in the same region and network makes database access faster.
- **Other:**
  - Requires maintenance - OS patching and keeping the Python environment current. 
  - Risk: a VM left running keeps billing by the hour (risk reduced with the auto-shutdown option).
  
#### 3. Azure Container Apps Jobs

- **Setup:** Package the engine in a Docker image and push it to Azure Container Registry. Create a Container Apps environment with a **Dedicated memory-optimized E-series** profile: E4 (32 GiB), E8 (64 GiB), E16 (128 GiB) or E32 (256 GiB). The Consumption profile (8 GiB) and the Flexible profile (16 GiB, preview) are no bigger than a standard runner. Store database credentials as secrets. Add a workflow step that runs `az containerapp job start` and waits for it to finish.
- **Billing:** Per vCPU-hour and per GiB-hour of the profile instance while the job runs. Per hour for the Dedicated plan management fee. Monthly for Container Registry, and per GB of logs for Log Analytics.
- **Advantages:** No VM to patch or remember to stop; nothing runs between jobs. Memory can be set per plan by choosing the profile size. If the database is in Azure, putting the job in the same region and network makes database access faster.
- **Other:**
    - Requires maintenance - rebuilding and pushing the image when the code or dependencies change.


