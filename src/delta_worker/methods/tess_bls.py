"""tess-bls v1: box least squares transit search on one TESS SPOC light curve.

The preparation and search follow experiments/tess-known-planet-recovery
(T01) scripts/run_experiment.py. The search receives observed arrays and
fixed settings only. The reference comparison runs after the search output
exists, and its values never enter the search.
"""
from collections import Counter
import hashlib
from pathlib import Path

from astropy.io import fits
from astropy.timeseries import BoxLeastSquares
import numpy as np

from . import Inconclusive, MethodContext, MethodOutput
from ..fits_checks import ChecksumError, verify as verify_checksums

BJD_REFERENCE = 2457000.0
TIME_REFERENCE = 'BJD 2457000.0'
BLS_METHOD = 'fast'
GRID = 'Value is a point on the fixed trial grid. BLS reports no uncertainty for it.'


def unavailable(reason):
    return {'kind': 'unavailable', 'reason': reason}


def standard(value):
    return {'kind': 'standard', 'value': float(value)} if np.isfinite(value) and value >= 0 else unavailable(
        'The standard error is not finite.')


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check_constraints(search):
    if not search['minimum_period'] < search['maximum_period']:
        raise Inconclusive('invalid_parameters', 'minimum_period must be less than maximum_period.')
    if not max(search['duration_days']) < search['minimum_period']:
        raise Inconclusive('invalid_parameters', 'Every duration must be less than minimum_period.')


def read_input(path, observation, parameters):
    """Read and check one SPOC light curve. Return arrays and the input report."""
    if path.stat().st_size != observation['bytes'] or sha256(path) != observation['sha256']:
        raise RuntimeError(f'Input file changed after indexing: {path}')
    try:
        checksums = verify_checksums(path)
    except ChecksumError as error:
        raise Inconclusive('fits_checksum', f'FITS checksum failed: {error}')
    with fits.open(path, checksum=False) as hdus:
        hdus.verify('exception')
        primary, header, data = hdus[0].header.copy(), hdus[1].header.copy(), hdus[1].data.copy()
    expected_tic = int(observation['target_id'].split(':')[1])
    if primary['TICID'] != expected_tic:
        raise RuntimeError(f"TICID {primary['TICID']} does not match {observation['target_id']}")
    conventions = {'TIMESYS': 'TDB', 'TIMEREF': 'SOLARSYSTEM', 'TIMEUNIT': 'd'}
    for key, value in conventions.items():
        if header.get(key) != value:
            raise Inconclusive('time_convention', f'{key} is {header.get(key)!r}, expected {value!r}.')
    bjdref = header['BJDREFI'] + header.get('BJDREFF', 0)
    if bjdref != BJD_REFERENCE:
        raise Inconclusive('time_convention', f'BJD reference is {bjdref}, expected {BJD_REFERENCE}.')
    column = parameters['flux_column'].upper()
    t = np.asarray(data['TIME'], dtype=float)
    f = np.asarray(data[column], dtype=float)
    err = np.asarray(data[column + '_ERR'], dtype=float)
    quality = np.asarray(data['QUALITY'], dtype=int)
    reasons = {'quality_nonzero': quality != 0,
               'invalid_time': ~np.isfinite(t), 'invalid_flux': ~np.isfinite(f) | (f <= 0),
               'invalid_error': ~np.isfinite(err) | (err <= 0)}
    keep = ~np.logical_or.reduce(list(reasons.values()))
    report = {'rows': len(t), 'retained': int(keep.sum()), 'excluded': int((~keep).sum()),
              'exclusion_reasons_overlapping': {k: int(v.sum()) for k, v in reasons.items()},
              'quality_values': {str(k): v for k, v in Counter(quality.tolist()).items()},
              'fits_checksums': checksums, 'flux_column': parameters['flux_column'],
              'timesys': header['TIMESYS'], 'timeref': header['TIMEREF'], 'bjd_reference': bjdref,
              'source_sha256': observation['sha256'], 'crowdsap': header.get('CROWDSAP'),
              'flfrcsap': header.get('FLFRCSAP'), 'procver': primary.get('PROCVER'),
              'data_release': primary.get('DATA_REL')}
    if keep.sum() < parameters['minimum_rows']:
        raise Inconclusive('insufficient_rows',
                           f"{int(keep.sum())} retained rows is less than minimum_rows {parameters['minimum_rows']}.",
                           report)
    report.update(median_cadence_seconds=float(np.median(np.diff(t[keep])) * 86400),
                  baseline_days=float(np.ptp(t[keep])), time_min=float(t[keep].min()),
                  time_max=float(t[keep].max()))
    if report['baseline_days'] < parameters['minimum_baseline_days']:
        raise Inconclusive('short_baseline',
                           f"Baseline {report['baseline_days']:.3f} d is less than {parameters['minimum_baseline_days']} d.",
                           report)
    if not np.all(np.diff(t[keep]) > 0):
        raise Inconclusive('time_order', 'Retained times do not increase strictly.', report)
    return t, f, err, keep, bjdref, report


def prepare(path, parameters, t, f, err, keep, bjdref):
    """Normalize and flatten with Lightkurve. Check that Lightkurve read the same values."""
    import lightkurve as lk
    lc = lk.read(path, flux_column=parameters['flux_column'], quality_bitmask='none')
    # Lightkurve excludes nonfinite TIME even when quality filtering is disabled.
    finite_time = np.isfinite(t)
    if not (len(lc) == finite_time.sum() and lc.time.scale == 'tdb'
            and np.allclose(lc.time.tdb.jd, t[finite_time] + bjdref, rtol=0, atol=1e-8)
            and np.allclose(lc.flux.value, f[finite_time], equal_nan=True, rtol=0, atol=0)
            and np.allclose(lc.flux_err.value, err[finite_time], equal_nan=True, rtol=0, atol=0)):
        raise RuntimeError('Lightkurve values differ from the FITS table values.')
    normalized = lc[keep[finite_time]].normalize()
    flat, trend = normalized.flatten(return_trend=True, **parameters['flatten'])
    if len(flat) != keep.sum():
        raise RuntimeError('flatten removed rows.')
    if not (np.all(np.isfinite(flat.flux.value)) and np.all(flat.flux_err.value > 0)
            and np.all(np.isfinite(trend.flux.value)) and np.all(trend.flux.value > 0)):
        raise Inconclusive('flatten_invalid', 'The flattened series or trend has invalid values.')
    return normalized, flat, trend


def search(t, flux, error, settings):
    """Receive observed arrays and fixed settings only. No catalog parameters."""
    bls = BoxLeastSquares(t, flux, dy=error)
    grid = bls.autoperiod(settings['duration_days'], minimum_period=settings['minimum_period'],
                          maximum_period=settings['maximum_period'],
                          frequency_factor=settings['frequency_factor'])
    if len(grid) > settings['maximum_grid_trials']:
        raise Inconclusive('grid_too_large',
                           f"The period grid has {len(grid)} trials, above maximum_grid_trials "
                           f"{settings['maximum_grid_trials']}.", {'grid_trials': len(grid)})
    result = bls.autopower(settings['duration_days'], minimum_period=settings['minimum_period'],
                           maximum_period=settings['maximum_period'],
                           frequency_factor=settings['frequency_factor'],
                           oversample=settings['oversample'], objective=settings['objective'],
                           method=BLS_METHOD)
    if not (np.array_equal(grid, result.period) and np.all(np.isfinite(result.power))):
        raise RuntimeError('The BLS periodogram is inconsistent with its grid.')
    columns = {k: np.asarray(result[k]) for k in
               ['period', 'power', 'depth', 'depth_err', 'duration', 'transit_time', 'depth_snr', 'log_likelihood']}
    index = int(np.argmax(result.power))
    best = {k: float(v[index]) for k, v in columns.items()}
    stats = bls.compute_stats(best['period'], best['duration'], best['transit_time'])
    peaks = []
    for i in np.argsort(result.power)[::-1]:
        p = float(result.period[i])
        if all(abs(p - v['period']) / v['period'] >= 0.01 for v in peaks):
            peaks.append({k: float(v[i]) for k, v in columns.items()})
        if len(peaks) == 5:
            break
    return {'best': best, 'best_grid_index': index, 'grid_trials': len(grid),
            'grid_period_min_days': float(grid.min()), 'grid_period_max_days': float(grid.max()),
            'peaks': peaks, 'stats': stats, 'objective': settings['objective'],
            'bls_method': BLS_METHOD, 'settings': settings,
            'reference_parameters_supplied_to_search': False}, columns


def search_measurements(observation_id, saved):
    best, stats = saved['best'], saved['stats']
    events = np.asarray(stats['per_transit_count'])
    flat = ['trend_removal_can_reduce_depth']
    rows = [
        ('period', best['period'], 'd', unavailable(GRID), []),
        ('transit_time', best['transit_time'], 'd', unavailable(GRID), []),
        ('duration', best['duration'], 'd', unavailable(GRID), []),
        ('depth', best['depth'], '1', standard(best['depth_err']), flat),
        ('depth_snr', best['depth_snr'], '1', unavailable('Formal ratio of depth to its standard error.'), []),
        ('log_likelihood', best['log_likelihood'], '1', unavailable('Objective value, not a measured quantity.'), []),
        ('depth_odd', stats['depth_odd'][0], '1', standard(stats['depth_odd'][1]), flat),
        ('depth_even', stats['depth_even'][0], '1', standard(stats['depth_even'][1]), flat),
        ('harmonic_delta_log_likelihood', stats['harmonic_delta_log_likelihood'], '1',
         unavailable('Model comparison statistic, not a measured quantity.'), []),
        ('observed_transit_count', int(np.count_nonzero(events > 0)), '1',
         unavailable('Count of predicted transits that contain at least one retained row.'), []),
    ]
    out = []
    for name, value, unit, uncertainty, flags in rows:
        m = {'observation_id': observation_id, 'role': 'search', 'name': name,
             'value': None if not np.isfinite(value) else float(value) if name != 'observed_transit_count' else value,
             'unit': unit, 'uncertainty': uncertainty, 'quality_flags': flags}
        if name == 'transit_time':
            m.update(time_scale='tdb', time_reference=TIME_REFERENCE)
        out.append(m)
    return out


def compare(observation, saved, midpoint):
    """Compare the saved search output with the closest known planet of the target."""
    planets = [p for p in observation.get('labels', {}).get('planets', []) if p.get('period_days')]
    if not planets:
        return [], None
    best = saved['best']
    planet = min(planets, key=lambda p: abs(best['period'] / p['period_days'] - 1))
    period = planet['period_days']
    note = 'Reference uncertainty is in the comparison diagnostics.'
    rows = [('relative_period_error', abs(best['period'] / period - 1)),
            ('relative_half_period_error', abs(best['period'] / (period / 2) - 1)),
            ('relative_double_period_error', abs(best['period'] / (period * 2) - 1))]
    diagnostics = {'planet': planet, 'midpoint_btjd_tdb': midpoint}
    epoch = planet.get('transit_midpoint_btjd_tdb')
    if epoch is not None:
        fitted = best['transit_time'] + round((midpoint - best['transit_time']) / best['period']) * best['period']
        cycles = round((fitted - epoch) / period)
        predicted = epoch + cycles * period
        rows.append(('phase_error', abs(fitted - predicted)))
        diagnostics.update(fitted_event_near_midpoint_btjd_tdb=fitted, reference_event_btjd_tdb=predicted,
                           reference_cycle_index=cycles)
        period_err, epoch_err = planet.get('period_err_days'), planet.get('transit_midpoint_err_days')
        if period_err is not None and epoch_err is not None:
            diagnostics['reference_timing_error_days'] = (
                epoch_err + abs(round((midpoint - epoch) / period)) * period_err)
    out = [{'observation_id': observation['id'], 'role': 'reference_comparison', 'name': name,
            'value': float(value), 'unit': 'd' if name == 'phase_error' else '1',
            'uncertainty': unavailable(note), 'quality_flags': ['guided_comparison']}
           for name, value in rows]
    return out, diagnostics


def execute(context: MethodContext) -> MethodOutput:
    parameters = context.parameters
    check_constraints(parameters['search'])
    if len(context.inputs) != 1:
        raise RuntimeError('tess-bls needs exactly one observation.')
    observation = context.inputs[0]
    oid = observation['id']
    path = Path(context.raw_root, observation['dataset_id'], observation['relative_path'])
    output = MethodOutput()
    t, f, err, keep, bjdref, report = read_input(path, observation, parameters)
    normalized, flat, trend = prepare(path, parameters, t, f, err, keep, bjdref)
    output.artifacts.append(context.artifacts.json('input-checks.json', report,
                                                   observation_id=oid, kind='input_checks'))
    tk = t[keep]
    output.artifacts.append(context.artifacts.columns('prepared.json.gz', [
        {'name': 'source_row', 'unit': '1', 'values': np.flatnonzero(keep)},
        {'name': 'time', 'unit': 'd', 'time_scale': 'tdb', 'time_reference': TIME_REFERENCE, 'values': tk},
        {'name': 'normalized_flux', 'unit': '1', 'values': normalized.flux.value,
         'description': f"{parameters['flux_column']} divided by its median."},
        {'name': 'normalized_error', 'unit': '1', 'values': normalized.flux_err.value},
        {'name': 'trend', 'unit': '1', 'values': trend.flux.value, 'description': 'Savitzky-Golay trend.'},
        {'name': 'flattened_flux', 'unit': '1', 'values': flat.flux.value,
         'description': 'Normalized flux divided by the trend. The search input.'},
        {'name': 'flattened_error', 'unit': '1', 'values': flat.flux_err.value},
    ], observation_id=oid, kind='prepared_series', description='Retained rows after preparation.'))
    saved, columns = search(tk, flat.flux.value, flat.flux_err.value, parameters['search'])
    output.artifacts.append(context.artifacts.columns(
        'periodogram.json.gz',
        [{'name': k, 'unit': 'd' if k in ('period', 'duration', 'transit_time') else '1',
          **({'time_scale': 'tdb', 'time_reference': TIME_REFERENCE} if k == 'transit_time' else {}),
          'values': v} for k, v in columns.items()],
        observation_id=oid, kind='periodogram', description='BLS periodogram on the trial period grid.'))
    output.artifacts.append(context.artifacts.json('search.json', saved, observation_id=oid, kind='search_summary'))
    output.measurements = search_measurements(oid, saved)
    # The search output is on disk before the reference values are read.
    comparison, comparison_diagnostics = compare(observation, saved, float((tk.min() + tk.max()) / 2))
    output.measurements += comparison
    output.diagnostics = {
        'rows': report['rows'], 'retained_rows': report['retained'],
        'exclusion_reasons_overlapping': report['exclusion_reasons_overlapping'],
        'median_cadence_seconds': report['median_cadence_seconds'], 'baseline_days': report['baseline_days'],
        'grid_trials': saved['grid_trials'], 'grid_period_min_days': saved['grid_period_min_days'],
        'grid_period_max_days': saved['grid_period_max_days'], 'objective': saved['objective'],
        'bls_method': BLS_METHOD, 'crowdsap': report['crowdsap'], 'flfrcsap': report['flfrcsap'],
        'procver': report['procver'], 'data_release': report['data_release'],
        'peaks': [{'period': p['period'], 'power': p['power'], 'depth_snr': p['depth_snr']} for p in saved['peaks']],
        'reference_comparison': comparison_diagnostics,
    }
    return output
