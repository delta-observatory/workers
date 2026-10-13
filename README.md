# Delta Observatory worker

The worker claims jobs from PostgreSQL, executes a method, and writes measurements, artifacts, and the result.
It does not call the API. It reads and writes only PostgreSQL rows and files.
The [contracts](../database/contracts/README.md) define every document that it reads or writes.

## Methods

| Method | Kind | What it does |
|---|---|---|
| `tess-spoc-index` v1 | Index | Scans `raw/<dataset_id>/*_lc.fits`. Checks CFITSIO checksums and the time system, reads headers, and writes one observation row and one light-curve artifact for each new file content. Reads labels from `datasets/<dataset_id>/**/manifest.json` files with format `tess-spoc-labels/1`. |
| `tess-bls` v1 | Analysis | Box least squares transit search on one light curve. Writes the input checks, the prepared series, the periodogram, the search summary, and the measurements. Compares the saved result with the closest known planet when the observation has one. |

`tess-bls` follows the preparation and search of [T01](../experiments/tess-known-planet-recovery/README.md).
With the default parameters, it gives the same values as T01 for both T01 light curves.
The method is provisional. Depth accuracy, completeness, and false-alarm rates are not validated.

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
Update both copies with `scripts/sync_contracts.sh <database-commit-or-tag>`.

## Tests

```bash
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python -r requirements.txt pytest==8.4.2 pgserver==0.1.4
.venv/bin/python -m pytest -q
```

`test_t01_regression.py` runs `tess-bls` on both T01 files and requires the exact T01 values.
`test_worker_flow.py` uses a temporary PostgreSQL server from `pgserver`. It runs an index job, a repeated index job, a `tess-bls` job, and a job that exceeds its memory limit.
The tests read light curves from `../raw/tess-spoc-light-curves/` and skip when the files are absent.

## Dependencies

`requirements.in` lists the direct dependencies.
`requirements.txt` pins every package. The scientific packages match the T01 environment.
Regenerate it with:

```bash
uv pip compile requirements.in -c ../experiments/tess-known-planet-recovery/requirements.txt --python-version 3.12 -o requirements.txt
```

## Image

`kubernetes/scripts/build.sh workers` builds `localhost/delta-workers:<tag>` in the cluster.
