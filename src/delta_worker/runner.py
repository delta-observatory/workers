"""Execute one job in a child process under its time and memory limits.

The parent process keeps the database connection. It extends the lease,
measures the child's resident memory, and stops the child when a limit is
exceeded. The child writes its output to a JSON file in a temporary directory.
"""
from dataclasses import asdict
import importlib.metadata
import json
import multiprocessing
import os
from pathlib import Path
import platform
import resource
import tempfile
import time
import traceback

from .artifacts import ArtifactWriter, clean

PACKAGES = ('astropy', 'lightkurve', 'numpy', 'scipy', 'fitsio', 'jsonschema', 'psycopg')


def software(worker_version):
    versions = {}
    for package in PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    return {'worker_version': worker_version, 'python': platform.python_version(), 'packages': versions}


def _child(job, inputs, existing, paths, output_file):
    for variable in ['OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS']:
        os.environ[variable] = '1'
    os.environ.setdefault('MPLCONFIGDIR', str(Path(output_file).parent / 'matplotlib'))
    from . import methods
    record = {}
    try:
        context = methods.MethodContext(
            job=job, parameters=job['parameters'], inputs=inputs, existing=existing,
            raw_root=Path(paths['raw_root']), catalog_root=Path(paths['catalog_root']),
            artifacts=ArtifactWriter(Path(paths['artifact_root']), paths['artifact_prefix']))
        output = methods.get(job['method']['id'])(context)
        record = {'outcome': 'complete', **asdict(output)}
    except methods.Inconclusive as error:
        record = {'outcome': 'inconclusive', 'error': {'kind': error.kind, 'message': str(error)},
                  'diagnostics': error.diagnostics}
    except Exception as error:
        record = {'outcome': 'failed', 'error': {'kind': type(error).__name__, 'message': str(error) or repr(error),
                                                 'traceback': traceback.format_exc()}}
    record['peak_rss_mib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    Path(output_file).write_text(json.dumps(clean(record), allow_nan=False, default=str))


def _rss_mib(pid):
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) / 1024
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0.0


def run(job, inputs, existing, settings, keep_lease):
    """Run the job and return (record, resources). keep_lease() returns False when the lease is lost."""
    limits = job['limits']
    prefix = f"jobs/{job['job_id']}/{job['attempt']}"
    paths = {'raw_root': str(settings.raw_root), 'catalog_root': str(settings.catalog_root),
             'artifact_root': str(settings.artifact_root), 'artifact_prefix': prefix}
    context = multiprocessing.get_context('spawn')
    with tempfile.TemporaryDirectory(prefix='delta-job-') as scratch:
        output_file = os.path.join(scratch, 'output.json')
        document = {k: v for k, v in job.items() if k != 'attempt'}
        process = context.Process(target=_child, args=(document, inputs, existing, paths, output_file), daemon=True)
        started = time.monotonic()
        process.start()
        peak, stop, last_lease = 0.0, None, started
        while process.is_alive():
            process.join(0.2)
            peak = max(peak, _rss_mib(process.pid))
            elapsed = time.monotonic() - started
            if peak > limits['maximum_memory_mib']:
                stop = {'kind': 'memory_limit', 'message': f"Resident memory {peak:.0f} MiB exceeded "
                                                           f"{limits['maximum_memory_mib']} MiB."}
            elif elapsed > limits['maximum_seconds']:
                stop = {'kind': 'time_limit', 'message': f"Elapsed time exceeded {limits['maximum_seconds']} s."}
            elif time.monotonic() - last_lease > settings.lease_seconds / 3:
                last_lease = time.monotonic()
                if not keep_lease():
                    stop = {'kind': 'lease_lost', 'message': 'Another worker owns the job.'}
            if stop:
                process.kill()
                process.join()
                break
        resources = {'elapsed_seconds': time.monotonic() - started, 'peak_rss_mib': peak}
        if stop:
            return {'outcome': 'failed', 'error': stop}, resources
        if not os.path.exists(output_file):
            return {'outcome': 'failed', 'error': {'kind': 'child_exit',
                                                   'message': f'The method process exited with code {process.exitcode}.'}}, resources
        record = json.loads(Path(output_file).read_text())
        # The child reports its own peak. It covers spikes between samples.
        peak = max(peak, record.pop('peak_rss_mib', 0.0))
        resources['peak_rss_mib'] = peak
        if peak > limits['maximum_memory_mib']:
            return {'outcome': 'failed', 'error': {'kind': 'memory_limit', 'message': f"Resident memory {peak:.0f} MiB "
                                                   f"exceeded {limits['maximum_memory_mib']} MiB."}}, resources
        return record, resources
