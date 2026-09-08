"""Field airborne/facade calculations; ISO 717-1:2020 rating (100–3150 Hz).

Sources and supported field procedures are documented in ACOUSTIC_MODES.md.
Keep source positions separate until their transmission ratios are averaged.
"""
import math
import numpy as np

BANDS = (100,125,160,200,250,315,400,500,630,800,1000,1250,1600,2000,2500,3150)
REFERENCE = np.array((33,36,39,42,45,48,51,52,53,54,55,56,56,56,56,56), float)
C_SPECTRUM = np.array((-29,-26,-23,-21,-19,-17,-15,-13,-12,-11,-10,-9,-9,-9,-9,-9), float)
CTR_SPECTRUM = np.array((-20,-20,-18,-16,-15,-14,-13,-12,-11,-9,-8,-9,-10,-11,-13,-15), float)
METHODS = {'rooms': "DnT", 'facade_ls': 'Dls,2m,nT', 'facade_traffic': 'Dtr,2m,nT',
           'element_45': "R′45°"}


def vector(mapping):
    normalized = {float(k): float(v) for k, v in mapping.items()}
    missing = set(BANDS) - normalized.keys()
    if missing:
        raise ValueError(f'Missing bands: {sorted(missing)}')
    result = np.array([normalized[b] for b in BANDS])
    if not np.isfinite(result).all() or np.any(np.abs(result)>300):
        raise ValueError('Band values must be finite and within -300..300')
    return result


def mapping(values):
    return dict(zip(BANDS, map(float, values)))


def energy_mean(rows):
    if not rows:
        raise ValueError('No measurement positions')
    x = np.array([vector(row) for row in rows])
    peak = x.max(axis=0)
    return peak + 10 * np.log10(np.mean(10 ** ((x - peak) / 10), axis=0))


def transmission_mean(rows):
    return -energy_mean([mapping(-vector(row)) for row in rows])


def rating(levels):
    measured = np.floor(vector(levels) * 10 + .5) / 10
    # Start below every measured band; find the highest allowed integer shift.
    shift = math.floor(float(np.min(measured - REFERENCE)))
    while np.maximum(REFERENCE + shift + 1 - measured, 0).sum() <= 32 + 1e-9:
        shift += 1
    value = 52 + shift
    def adaptation(spectrum):
        terms = (spectrum - measured) / 10
        peak = float(terms.max())
        xa = -10 * (peak + math.log10(float(np.sum(10 ** (terms - peak)))))
        return math.floor(xa + .5) - value
    return dict(value=value, C=adaptation(C_SPECTRUM), Ctr=adaptation(CTR_SPECTRUM),
                reference=mapping(REFERENCE + shift),
                deviation_sum=float(np.maximum(REFERENCE + shift - measured, 0).sum()))


def positive(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be finite and positive')
    return value


def evaluate(groups, reverberation, *, method='rooms', volume=0, area=0, t0=.5):
    """groups: source-position -> {'L1': [spectra], 'L2': [...], 'B2': [...]}.

    Background <6 dB produces a lower-bound insulation result, never a pass.
    No partial band/position silently disappears. T is the receiving room T60.
    """
    if method not in METHODS or not groups:
        raise ValueError('Select a supported method and supply source positions')
    t = vector(reverberation)
    if np.any(t <= 0):
        raise ValueError('Receiving-room reverberation time must be positive in every band')
    t0 = positive(t0, 'Reference reverberation time')
    volume, area = float(volume), float(area)
    if not all(math.isfinite(v) and v >= 0 for v in (volume, area)):
        raise ValueError('Volume and area must be finite and nonnegative')
    with_reduction = method == 'element_45' or (method == 'rooms' and volume > 0 and area > 0)
    if with_reduction:
        absorption = .16 * positive(volume, 'Receiving room volume') / t
        area = positive(area, 'Separating/test element area')
    results, limited, notes = {}, set(), []
    for source, group in groups.items():
        l1, l2, bg = (energy_mean(group.get(role, [])) for role in ('L1', 'L2', 'B2'))
        # ISO 16283 background correction uses levels reduced to 0.1 dB.
        l2, bg = np.floor(l2*10+.5)/10, np.floor(bg*10+.5)/10
        if method == 'rooms' and any(len(group.get(role, [])) < 5 for role in ('L1','L2')):
            notes.append(f'Source {source}: fewer than five spatial samples; field sampling incomplete')
        if method == 'rooms' and np.any(np.abs(np.diff(l1)[1:]) > 8):
            notes.append(f'Source {source}: adjacent source spectrum difference exceeds 8 dB')
        difference = l2 - bg
        weak = difference < 6
        corrected = l2.copy()
        marginal = (difference >= 6) & (difference < 10)
        corrected[marginal] += 10 * np.log10(1 - 10 ** (-difference[marginal] / 10))
        corrected[weak] -= 1.3
        limited.update(b for b, flag in zip(BANDS, weak) if flag)
        d = l1 - corrected
        spectra = {'D': mapping(d)}
        if method != 'element_45':
            spectra[METHODS[method]] = mapping(d + 10 * np.log10(t / t0))
        if with_reduction:
            spectra["R′" if method == 'rooms' else "R′45°"] = mapping(
                d + 10 * np.log10(area / absorption) - (1.5 if method == 'element_45' else 0))
        results[str(source)] = dict(spectra=spectra, L1=mapping(l1), L2=mapping(corrected),
                                   background_difference=mapping(difference))
    names = next(iter(results.values()))['spectra']
    combined = {name: mapping(transmission_mean([r['spectra'][name] for r in results.values()]))
                for name in names}
    if limited:
        notes.append('Background-limited bands: insulation is a lower bound, not a pass/fail result')
    if len(groups) < 2 and method == 'rooms':
        notes.append('Only one source position: field sampling incomplete')
    notes.append('Default 100–3150 Hz calculation; geometry and field sampling require verification')
    return dict(method=method, spectra=combined, ratings={k: rating(v) for k,v in combined.items() if k != 'D'},
                source_positions=results, lower_bound=bool(limited), limited_bands=sorted(limited), notes=notes)
