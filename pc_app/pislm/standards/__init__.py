"""Floor impact sound standards maths — ISO 16283-2 / ISO 717-2 / ISO 3382-2.

Layers:

    bands.py          band definitions, A/C weighting, level arithmetic, rounding
    normalize.py      background correction, L'nT / L'n / L'i,Fmax,V,T
    rating.py         ISO 717-2 reference curve shifting
    reverberation.py  Schroeder backward integration, T20/T30

Provenance is tagged in the source comments as [SPEC] / [DERIVED] / [PRACTICE].
**Anything marked [DERIVED] could not be taken from the published text and was
derived from physics instead; check it against the standard before issuing a
report.** Currently that means:

  * `normalize.standardized_max_level()` — ISO 16283-2 formula (4)
  * `korea.inverse_a_curve()` — KS F 2863 curve values (not bundled; supply your own)

Typical flow:

    from pislm.standards import (
        Spectrum, correct_background, RoomCorrection, rate,
    )

    signal = Spectrum.from_mapping({100: 58.2, 125: 57.1, ...})
    background = Spectrum.from_mapping({100: 31.0, 125: 30.4, ...})

    corrected = correct_background(signal, background)
    room = RoomCorrection(volume=42.0, reverberation_time=0.42)
    result = rate(room.standardized(corrected.spectrum), quantity="L'nT,w")
    print(result.summary())
"""

from .bands import (
    G_BASE10,
    OCTAVE_NOMINAL,
    RATING_OCTAVE,
    RATING_THIRD_OCTAVE,
    RUBBER_BALL_OCTAVE,
    RUBBER_BALL_THIRD_OCTAVE,
    TAPPING_LOW_FREQUENCY,
    TAPPING_THIRD_OCTAVE,
    THIRD_OCTAVE_NOMINAL,
    Spectrum,
    a_weighting,
    average_spectra,
    band_edges,
    band_index,
    c_weighting,
    energy_average,
    energy_difference,
    energy_sum,
    exact_center,
    exact_centers,
    nominal_center,
    rms_level,
    round_half_up_1dp,
)
from .normalize import (
    A0_DWELLING,
    BACKGROUND_CLEAN_DIFFERENCE,
    BACKGROUND_LIMIT_CORRECTION,
    BACKGROUND_MIN_DIFFERENCE,
    T0_DWELLING,
    V0_DWELLING,
    BackgroundResult,
    RoomCorrection,
    absorption_area,
    correct_background,
    normalized_level,
    standardized_level,
    standardized_max_level,
)
from .rating import (
    MAX_DEVIATION_OCTAVE,
    MAX_DEVIATION_THIRD_OCTAVE,
    REFERENCE_OCTAVE,
    REFERENCE_THIRD_OCTAVE,
    KoreanCurveNotConfigured,
    RatingResult,
    a_weighted_max_level,
    rate,
    rate_inverse_a,
)
from .korea import (
    FILTER_ORDER,
    HEAVY_OCTAVE_BANDS,
    HEAVY_THIRD_OCTAVE_BANDS,
    LIGHT_THIRD_OCTAVE_BANDS,
    POST_VERIFICATION_LIMIT_DB,
    DualEvaluation,
    InverseACurve,
    a_weighted_single_number,
    inverse_a_curve,
    octave_from_third,
    rate_with_curve,
)
from .reverberation import (
    CURVATURE_LIMIT,
    DecayResult,
    average_reverberation,
    decay_time,
    octave_filter,
    reverberation_spectrum,
    schroeder_curve,
)

__all__ = [
    # bands
    "Spectrum", "a_weighting", "c_weighting", "energy_sum", "energy_average",
    "energy_difference", "average_spectra", "nominal_center", "round_half_up_1dp",
    "exact_center", "exact_centers", "band_edges", "band_index", "rms_level", "G_BASE10",
    "THIRD_OCTAVE_NOMINAL", "OCTAVE_NOMINAL", "TAPPING_THIRD_OCTAVE",
    "TAPPING_LOW_FREQUENCY", "RUBBER_BALL_THIRD_OCTAVE", "RUBBER_BALL_OCTAVE",
    "RATING_THIRD_OCTAVE", "RATING_OCTAVE",
    # normalize
    "correct_background", "BackgroundResult", "absorption_area",
    "standardized_level", "normalized_level", "standardized_max_level",
    "RoomCorrection", "T0_DWELLING", "A0_DWELLING", "V0_DWELLING",
    "BACKGROUND_MIN_DIFFERENCE", "BACKGROUND_LIMIT_CORRECTION",
    "BACKGROUND_CLEAN_DIFFERENCE",
    # rating
    "rate", "RatingResult", "REFERENCE_THIRD_OCTAVE", "REFERENCE_OCTAVE",
    "MAX_DEVIATION_THIRD_OCTAVE", "MAX_DEVIATION_OCTAVE",
    "a_weighted_max_level", "rate_inverse_a", "KoreanCurveNotConfigured",
    # reverberation
    "schroeder_curve", "decay_time", "DecayResult", "octave_filter",
    "reverberation_spectrum", "average_reverberation", "CURVATURE_LIMIT",
    # korea
    "InverseACurve", "inverse_a_curve", "rate_with_curve", "DualEvaluation",
    "POST_VERIFICATION_LIMIT_DB", "HEAVY_OCTAVE_BANDS", "HEAVY_THIRD_OCTAVE_BANDS",
    "LIGHT_THIRD_OCTAVE_BANDS", "octave_from_third", "FILTER_ORDER",
    "a_weighted_single_number",
]
