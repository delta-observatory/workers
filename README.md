# Delta Observatory worker

This repository is one component of [Delta Observatory](https://github.com/delta-observatory/delta-observatory). The entry repository describes the application and restores every component.

The worker claims jobs from PostgreSQL, executes a method, and writes measurements, artifacts, and the result.
It does not call the API. It reads and writes only PostgreSQL rows and files.
The [contracts](https://github.com/delta-observatory/database/blob/main/contracts/README.md) define every document that it reads or writes.

## Methods

| Method | Kind | What it does |
|---|---|---|
| `tess-spoc-index` v1 | Index | Scans `raw/<dataset_id>/*_lc.fits`, and the subdirectory files that selection manifests list. Checks CFITSIO checksums and the time system, reads headers, and writes one observation row and one light-curve artifact for each new file content. Reads labels from `datasets/<dataset_id>/**/manifest.json` files with format `tess-spoc-labels/1`. |
| `tess-bls` v1 | Analysis | Box least squares transit search on one light curve. Writes the input checks, the prepared series, the periodogram, the search summary, and the measurements. Compares the saved result with the closest known planet when the observation has one. |

`tess-bls` follows the preparation and search of [T01](https://github.com/delta-observatory/experiments/blob/main/tess-known-planet-recovery/README.md) (private).
With the default parameters, it gives the same values as T01 for both T01 light curves.
The method is provisional. Depth accuracy, completeness, and false-alarm rates are not validated.

## Selected observations

The index method does not scan subdirectories of `raw/<dataset_id>/`. Research agents keep their downloads there.
A file in a subdirectory enters the inventory only when a selection manifest lists it.
A selection manifest is a file `datasets/<dataset_id>/selections/<name>.json` with format `tess-spoc-selection/1`.
[database/contracts/inputs/tess-spoc-selection/](https://github.com/delta-observatory/database/blob/main/contracts/README.md#selected-observations) defines it.
Each manifest names the owner of the files and the cohort register entry that registers them. Richard decides each registration.

For each listed file, the method applies these rules:

| Condition | Result |
|---|---|
| The manifest is not valid, or names another dataset. | Dataset finding `invalid_selection`. The method skips the manifest. |
| The path contains a symbolic link or resolves outside the dataset directory. | Dataset finding `selected_path_refused`. |
| The file does not exist. | Dataset finding `selected_file_missing`. |
| The size or SHA-256 differs from the manifest. | Dataset finding `selected_file_changed`. The method does not index the file. |
| An observation already has the same content. | Dataset finding `duplicate_content` with the observation ID. No new observation. The existing path does not change. |
| The header TICID differs from the manifest target. | The observation is `invalid` with finding `selected_target_mismatch`. |
| New content. | A new observation with the listed path. `provenance` holds the manifest path, owner, source identifier, and registration. |

Listed files count as present, so a repeated index keeps them. A file that no manifest lists any longer becomes `missing`.
Observations of files in the dataset directory have empty `provenance`.
Selection defects make the dataset `incomplete`. A duplicate content does not.
The index diagnostics give the manifest paths in `selections` and the counts in `selected`.

## Execution

1. At startup, the worker queues an index job for each dataset in `DELTA_DATASETS` unless one waits or runs.
2. The worker claims the oldest available job with `SELECT … FOR UPDATE SKIP LOCKED`, or a job with an expired lease.
3. The worker checks the job document against `job/1` and the method parameter schema.
4. A child process executes the method. The parent extends the lease and measures resident memory.
5. The parent stops the child when it exceeds `limits.maximum_seconds` or `limits.maximum_memory_mib`.
6. The parent checks the result against `result/1` and writes all rows in one transaction.

Outcomes:

- `complete`: the method produced its outputs.
- `inconclusive`: an input or quality condition prevents a usable result, for example too few retained rows. No retry.
- `failed`: a technical error or a limit stopped the job. No automatic retry.

A worker that stops during a job leaves an expired lease. Another worker claims the job again, up to `max_attempts`.
After the last attempt, the job fails with `lease_expired`.
At SIGTERM, the worker stops the child and returns the job to the queue.
Each attempt writes its files under `jobs/<job_id>/<attempt>/`. Files of a failed attempt stay on disk without artifact rows.

The API cancels a running job when it sets the job state to `cancelled`.
At the next lease extension, at most one third of `DELTA_LEASE_SECONDS` later, the worker stops the child.
The worker writes no measurements, artifacts, or result for the job, and logs `job <id> cancelled; output discarded`.
The files of that attempt stay on disk without artifact rows. The worker time of a cancelled job is not recorded.

Restart and retry limits:

- A method does not resume partial work. A new attempt starts the method again from its inputs.
- The worker repeats a job automatically only after an expired lease. A `failed` or `inconclusive` outcome is final.
- A finished job does not change. A database trigger refuses the update.
- A retry of failed or cancelled jobs is a new run (`run.retry_of`). The API creates it. Completed jobs are not repeated.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | required | PostgreSQL connection URL. |
| `DELTA_RAW_ROOT` | `/data/raw` | Read-only `raw/` directory. |
| `DELTA_CATALOG_ROOT` | `/data/catalog` | Read-only directory that contains `datasets/`. |
| `DELTA_ARTIFACT_ROOT` | `/data/artifacts` | Writable artifact storage root. |
| `DELTA_CONTRACTS_DIR` | `vendor/contracts` | Vendored contract schemas. |
| `DELTA_DATASETS` | `tess-spoc-light-curves` | Comma-separated datasets to index at startup. |
| `DELTA_WORKER_ID` | host name | Identity in `job.claimed_by`. |
| `DELTA_WORKER_VERSION` | `development` | Version in each result. The deployment sets the image tag. |
| `DELTA_LEASE_SECONDS` | `60` | Lease length. The worker extends it every third of this time. |
| `DELTA_POLL_SECONDS` | `30` | Longest wait for `NOTIFY delta_jobs` before the next claim attempt. |

## Contracts

`vendor/contracts/` is a copy of `database/contracts/` at the commit in `vendor/contracts/SOURCE`.
`vendor/migrations/` is a copy for the tests only. The image does not include it.
The worker reads `job/1`, `result/1`, `columns/1`, the method parameter schemas, and the selection manifest schema.
Update both copies with `scripts/sync_contracts.sh <database-commit-or-tag>`.

## Tests

```bash
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python -r requirements.txt pytest==8.4.2 pgserver==0.1.4
.venv/bin/python -m pytest -q
```

`test_t01_regression.py` runs `tess-bls` on both T01 files and requires the exact T01 values.
`test_worker_flow.py` uses a temporary PostgreSQL server from `pgserver`. It runs an index job, a repeated index job, a `tess-bls` job, and a job that exceeds its memory limit.
It also cancels a running `tess-bls` job from another connection, and checks that a finished job does not change.
`test_selection.py` checks selection manifests: provenance, findings for each refused file, duplicate content, a repeated index, and the absence of a recursive scan.
The tests read light curves from `../raw/tess-spoc-light-curves/` and skip when the files are absent.
`test_t01_regression.py` also reads the T01 record from `../experiments/`, a private repository. It skips when that record is absent.

## Dependencies

`requirements.in` lists the direct dependencies.
`requirements.txt` pins every package. The scientific packages match the T01 environment.
Regenerate it with:

```bash
uv pip compile requirements.in -c ../experiments/tess-known-planet-recovery/requirements.txt --python-version 3.12 -o requirements.txt
```

## Image

`kubernetes/scripts/build.sh workers` builds `localhost/delta-workers:<tag>` in the cluster.

## License

The code is under the [MIT License](LICENSE).

## Archive acquisition

The existing worker polls the PostgreSQL acquisition queue after scientific jobs.
It uses the [Astroquery MAST client](https://astroquery.readthedocs.io/en/stable/mast/mast_obsquery.html) for bounded metadata queries.
Supported selections contain one TIC target, at most eight sectors, and public SPOC 120-second light curves.
The worker checks file and byte limits before presenting the manifest for approval.
A person approves the exact manifest checksum through the API.

Each download checks its declared size, FITS checksums, target, sector, cadence, and required columns.
The worker installs a checked file atomically in `raw/tess-spoc-light-curves/`.
A session lock prevents duplicate processing by multiple workers.
After interruption, the worker checks completed files and repeats incomplete files.
It stops a transfer before exceeding its byte limit or free-space reserve.
Each file transfer has a 600-second deadline.
Completed transfers queue the existing index method.
Observation provenance includes the acquisition ID, manifest checksum, and source product.

The acquisition checks use controlled transfer inputs and a temporary PostgreSQL server.
They do not evaluate scientific performance.

## Desktop storage policy

`DELTA_STORAGE_CONFIG` selects the canonical `desktop-storage/1` settings file.
The worker reads the settings before each job, download, and artifact write.
The workspace size limit includes managed files under the settings directory.
The worker preserves existing files when a limit blocks an operation.
Without desktop settings, artifact writes retain a 512 MiB free-space reserve.
The archive manifest records its product processing code. Indexed FITS headers preserve the pipeline's `PROCVER` value when present.
