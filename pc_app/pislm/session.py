"""Measurement session — source positions, receiver channels, results.

One field measurement lives in a single object. It saves to and loads from
JSON, so a crash mid-measurement does not lose the work already done.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

import numpy as np

from .standards import (
    HEAVY_THIRD_OCTAVE_BANDS,
    LIGHT_THIRD_OCTAVE_BANDS,
    POST_VERIFICATION_LIMIT_DB,
    DualEvaluation,
    InverseACurve,
    RoomCorrection,
    Spectrum,
    a_weighted_max_level,
    a_weighted_single_number,
    average_spectra,
    correct_background,
    energy_average,
    rate,
    rate_with_curve,
)

SCHEMA_VERSION = 3


class ImpactSource(str, Enum):
    """Standard impact sources.

    Legacy bang uses directly measured 1/1-octave Fmax. Ball and tapping
    use 1/3 octave. Do not sum separately timed third-octave maxima.
    """

    TAPPING = "tapping"          # tapping machine (light, KS F 2810-1)
    BANG = "bang"                # bang machine (heavy, former KS F 2810-2)
    RUBBER_BALL = "rubber_ball"  # rubber ball / impact ball (heavy)

    @property
    def label(self) -> str:
        return {
            "tapping": "Tapping machine (light)",
            "bang": "Bang machine — 구법 역A (1/1 octave)",
            "rubber_ball": "Rubber ball (heavy)",
        }[self.value]

    @property
    def is_heavy(self) -> bool:
        return self in (ImpactSource.BANG, ImpactSource.RUBBER_BALL)

    @property
    def bands(self) -> tuple[float, ...]:
        """Required bands for the selected source and assessment profile."""
        if self is ImpactSource.BANG:
            return (63, 125, 250, 500)
        return HEAVY_THIRD_OCTAVE_BANDS if self.is_heavy else LIGHT_THIRD_OCTAVE_BANDS

    @property
    def fraction(self) -> int:
        return 1 if self is ImpactSource.BANG else 3

    @property
    def quantity(self) -> str:
        """What is taken per band.

        Heavy uses the Fast time-weighted maximum; light uses Leq (RMS based).
        Fast weighting is the heavy-impact quantity; for light impact it only
        drives the on-screen meter and plays no part in the Leq figure.
        """
        return "Fmax" if self.is_heavy else "Leq"

    @property
    def time_weighting(self) -> str:
        return "Fast"

    @property
    def frequency_weighting(self) -> str:
        """Always measure with Z (unweighted).

        [SPEC] ISO 717-2 applies A-weighting **as values added to the 1/3-octave
        band spectrum**, not as a filter on the signal. So the Pi sends
        unweighted band levels and the weighting is added only when the single
        number is computed. That way the unweighted spectrum for the report and
        the A-weighted single number come from the same measurement.
        """
        return "Z"

    @property
    def single_number_symbol(self) -> str:
        """The quantity the post-construction check is made against.

        [SPEC] 국토교통부 고시 제2022-868호 (2022-12-28) replaced both of the
        old inverse-A quantities:

        * light (tapping machine) -> **L'nT,w**, 가중 표준화 바닥충격음레벨.
          ISO 717-2 reference-curve shifting on the standardized 1/3-octave
          spectrum. *Not* an A-weighted sum of the bands.
        * heavy (impact ball) -> **L'iA,Fmax**, A-가중 최대 바닥충격음레벨.
        """
        if self is ImpactSource.BANG:
            return "L'i,Fmax,AW"
        return "L'iA,Fmax" if self.is_heavy else "L'nT,w"

    @property
    def single_number_method(self) -> str:
        if self is ImpactSource.BANG:
            return "KS F 2863-2 legacy inverse-A (8 dB)"
        return (
            "A-weighted maximum" if self.is_heavy
            else "ISO 717-2 reference-curve shifting"
        )

    @property
    def requires_standardisation(self) -> bool:
        """Is the band spectrum referred to T0 = 0.5 s before rating?

        [SPEC] Light only. L'nT,w is by definition the *standardised* level, so
        the tapping-machine path cannot be rated without a reverberation time.
        The heavy quantity L'iA,Fmax is the A-weighted Fast maximum **as
        measured** — no T normalisation, and therefore no volume or T needed at
        all. Normalising it anyway would shift the figure by 10 lg(T/T0)
        against the same 49 dB limit, which is a silent few-decibel error.
        """
        return not self.is_heavy

    @property
    def requires_reverberation(self) -> bool:
        """Whether a reverberation time has to be measured for this source."""
        return self.requires_standardisation


@dataclass(slots=True)
class Measurement:
    """One result from a (source position, receiver channel) pair.

    A receiver position is a microphone channel. With a six-channel rig one
    excitation records several receiver positions **at once**, so a single
    capture produces several Measurement objects.
    """

    source_position: int
    #: Receiver channel (global number) — this is the receiver position.
    channel: int
    #: Band levels (light = Leq, heavy = Fmax)
    levels: dict[float, float] = field(default_factory=dict)
    #: Broadband single number — L_iA,Fmax for heavy, L_A,eq for light
    broadband: float | None = None
    quantity: str = ""
    weighting: str = "Z"
    timestamp: str = ""
    valid: bool = True
    note: str = ""
    fraction: int = 3  # old files were 1/3 octave; never reinterpret as octaves

    def spectrum(self, fraction: int = 3) -> Spectrum:
        return Spectrum.from_mapping(
            self.levels,
            fraction=fraction,
            quantity=self.quantity,
            weighting=self.weighting,
        )

    @property
    def key(self) -> tuple[int, int]:
        return (self.source_position, self.channel)


@dataclass(slots=True)
class Room:
    """Receiving room data."""

    name: str = ""
    volume: float = 0.0
    #: Reverberation time per band, seconds. Without it no normalisation is possible
    reverberation: dict[float, float] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return self.volume > 0 and bool(self.reverberation)

    def correction(self, bands) -> RoomCorrection:
        times = [self.reverberation.get(float(b)) for b in bands]
        if any(t is None or t <= 0 for t in times):
            missing = [b for b, t in zip(bands, times) if not t]
            raise ValueError(f"no reverberation time for bands: {missing}")
        return RoomCorrection(volume=self.volume, reverberation_time=np.array(times))

    def mean_reverberation(self) -> float:
        values = [v for v in self.reverberation.values() if v and v > 0]
        return float(np.mean(values)) if values else 0.0


@dataclass(slots=True)
class Session:
    """One complete field measurement."""

    title: str = ""
    site: str = ""
    operator: str = ""
    source: ImpactSource = ImpactSource.RUBBER_BALL
    room: Room = field(default_factory=Room)
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    #: Number of source positions — the operator moves the source between them
    source_positions: int = 5
    #: Receiver channels. Every one of these is recorded in a single excitation
    channels: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])

    measurements: list[Measurement] = field(default_factory=list)
    #: Background noise band levels
    background: dict[float, float] = field(default_factory=dict)
    #: Calibration record (channel -> sensitivity)
    calibration: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: Auditable SET/cycle/quality records for applied reverberation measurements.
    reverberation_records: list[dict] = field(default_factory=list)

    # ── Bands ──
    @property
    def bands(self) -> tuple[float, ...]:
        return self.source.bands

    @property
    def fraction(self) -> int:
        return self.source.fraction

    # ── Measurement bookkeeping ──
    def add(self, measurement: Measurement) -> None:
        """Overwrite any existing (source, channel) pair — supports re-measuring."""
        if not measurement.timestamp:
            measurement.timestamp = datetime.now().isoformat(timespec="seconds")
        self.measurements = [m for m in self.measurements if m.key != measurement.key]
        self.measurements.append(measurement)
        self.measurements.sort(key=lambda m: m.key)

    def add_all(self, measurements) -> int:
        """Store every channel from one excitation at once."""
        count = 0
        for measurement in measurements:
            self.add(measurement)
            count += 1
        return count

    def get(self, source_position: int, channel: int) -> Measurement | None:
        for m in self.measurements:
            if m.key == (source_position, channel):
                return m
        return None

    @property
    def valid_measurements(self) -> list[Measurement]:
        return [m for m in self.measurements if m.valid]

    @property
    def required_count(self) -> int:
        return self.source_positions * len(self.channels)

    @property
    def progress(self) -> tuple[int, int]:
        return len(self.valid_measurements), self.required_count

    @property
    def complete(self) -> bool:
        done, need = self.progress
        return done >= need

    def completed_source_positions(self) -> set[int]:
        """Source positions where every channel has been filled."""
        done = set()
        have = {m.key for m in self.valid_measurements}
        for s in range(1, self.source_positions + 1):
            if all((s, c) in have for c in self.channels):
                done.add(s)
        return done

    def missing(self) -> list[tuple[int, int]]:
        have = {m.key for m in self.valid_measurements}
        return [
            (s, c)
            for s in range(1, self.source_positions + 1)
            for c in self.channels
            if (s, c) not in have
        ]

    # ── Aggregation ──
    def averaged_spectrum(self) -> Spectrum:
        """[SPEC] ISO 16283-2 formulas (7)(8)(9) — energy average over positions.

        The standard averages receiver positions within each source position
        first, then averages those. With a complete matrix that equals a flat
        average, but with missing pairs it does not, so the order is kept.
        """
        valid = self.valid_measurements
        if not valid:
            raise ValueError("no valid measurements")

        if self.source is ImpactSource.BANG:
            if any(m.fraction != 1 or set(m.levels) != set(self.bands)
                   or not np.isfinite(list(m.levels.values())).all() for m in valid):
                raise ValueError("Legacy bang requires four measured octave Fmax bands; recapture old third-octave data")
        per_source: list[Spectrum] = []
        for s in sorted({m.source_position for m in valid}):
            group = [m.spectrum(self.fraction) for m in valid if m.source_position == s]
            per_source.append(average_spectra(group))
        if self.source is ImpactSource.BANG:
            return Spectrum.from_mapping(
                {b: float(np.mean([s.as_dict()[b] for s in per_source])) for b in self.bands},
                fraction=1, quantity="Fmax", weighting="Z")
        return average_spectra(per_source)

    def channel_spectrum(self, channel: int) -> Spectrum:
        """One receiver channel averaged over source positions — for the graph."""
        group = [
            m.spectrum(self.fraction) for m in self.valid_measurements if m.channel == channel
        ]
        if not group:
            raise ValueError(f"no measurements on channel {channel}")
        return average_spectra(group)

    def background_spectrum(self) -> Spectrum | None:
        """Background restricted to the bands this source is rated over.

        One background sweep can cover 50-3150 Hz and serve both impact
        sources, which is worth doing because it is the same room noise either
        way. `correct_background()` insists the two band sets match exactly,
        so trim here rather than forcing a second measurement.
        """
        if not self.background:
            return None
        background = self.background
        if self.source is ImpactSource.BANG and set(background) != set(self.bands):
            # Background is Leq: third-octave energy summation is valid here.
            from .standards.korea import octave_from_third
            background = octave_from_third(Spectrum.from_mapping(background, fraction=3)).as_dict()
        wanted = {float(b): background[b] for b in background
                  if float(b) in {float(x) for x in self.bands}}
        if len(wanted) < len(self.bands):
            return None  # incomplete for this source; treated as "not measured"
        return Spectrum.from_mapping(
            wanted,
            fraction=self.fraction,
            label="background",
            quantity=self.source.quantity,
            weighting=self.source.frequency_weighting,
        )

    def single_number_broadband(self) -> float | None:
        """Energy average of the per-position A-weighted levels (formula 9).

        Each measurement's value came from **adding A-weighting values to its Z
        band spectrum** (ISO 717-2).

        This is the heavy-impact rating, L'iA,Fmax. For a tapping machine it is
        *not* the rating — light impact is rated by curve shifting (L'nT,w) —
        but the number is still worth having as a broadband cross-check.
        """
        values = [m.broadband for m in self.valid_measurements if m.broadband is not None]
        if not values:
            return None
        return a_weighted_max_level(values)

    def a_weighted_from_bands(self) -> float | None:
        """A-weighted single number from the averaged spectrum (cross-check).

        `single_number_broadband()` weights per position and then averages; this
        averages first and then weights. Energy averaging and energy summing
        commute, so the two must agree — a mismatch means something is wrong.
        """
        try:
            spectrum = self.averaged_spectrum()
        except ValueError:
            return None
        if spectrum.weighting == "A":
            spectrum = spectrum.unweighted()
        return a_weighted_single_number(spectrum)

    # ── Rating ──
    def evaluate(self, curve: InverseACurve | None = None) -> DualEvaluation:
        """Compute both rating systems.

        Order is background -> standardisation (reverberation) -> single number,
        and the standardisation step is **light only** — see
        `ImpactSource.requires_standardisation`. Missing ingredients skip a step
        and are noted in `warnings`.
        """
        result = DualEvaluation(
            limit_db=POST_VERIFICATION_LIMIT_DB,
            post_verification_symbol=self.source.single_number_symbol,
        )

        try:
            spectrum = self.averaged_spectrum()
        except ValueError as exc:
            result.warnings.append(str(exc))
            return result

        if not self.complete:
            done, need = self.progress
            result.warnings.append(f"measurement incomplete ({done}/{need} pairs)")

        background = self.background_spectrum()
        if background is None:
            result.warnings.append("background not measured — no correction applied")
        else:
            corrected = correct_background(spectrum, background)
            spectrum = corrected.spectrum
            if corrected.at_limit:
                result.warnings.append(corrected.report_note())

        # [SPEC] Standardisation (ISO 16283-2 formula 1, L'nT = Li − 10 lg(T/T0))
        # belongs to the **light** quantity only. The Korean post-construction
        # heavy figure L'iA,Fmax is the A-weighted Fast maximum as measured; it
        # carries no reverberation normalisation, so applying one here would move
        # the result by 10 lg(T/T0) against the 49 dB limit — in a live room
        # (T ≈ 0.8 s) that is +2 dB of pure error, in the direction that fails a
        # floor that passed. Heavy therefore needs neither volume nor T, and the
        # absence of them is not a caveat on the answer.
        if self.source.requires_standardisation:
            if self.room.configured:
                try:
                    spectrum = self.room.correction(self.bands).standardized(spectrum)
                except ValueError as exc:
                    result.warnings.append(f"standardisation failed: {exc}")
            else:
                result.warnings.append(
                    "volume/reverberation missing — values are not standardised"
                )

        if self.source is ImpactSource.BANG:
            result.legacy = True
            try:
                legacy_curve = InverseACurve.legacy_heavy()
                result.inverse_a = rate_with_curve(spectrum, legacy_curve)
                result.inverse_a.quantity = "L'i,Fmax,AW (legacy bang)"
                result.warnings.append("Legacy method: receiver energy average, source arithmetic average; no 49 dB post-verification verdict")
                result.warnings.append("Curve checked against published 2013 test report; confirm the project's applicable KS edition")
            except (KeyError, ValueError) as exc:
                result.warnings.append(f"legacy inverse-A rating failed: {exc}")
            return result

        # 1) Post-verification single number. The two impact sources are rated by
        #    different procedures — 국토교통부 고시 제2022-868호 replaced the old
        #    inverse-A pair, and replaced them with *different* things:
        #      heavy -> L'iA,Fmax, an A-weighted energy sum of the bands
        #      light -> L'nT,w, ISO 717-2 reference-curve shifting
        #    Summing A-weighted bands for a tapping machine would be a different
        #    quantity entirely and is not comparable with the 49 dB limit.
        working = spectrum.unweighted() if spectrum.weighting == "A" else spectrum
        if self.source.is_heavy:
            try:
                result.post_verification = a_weighted_single_number(working)
            except (ValueError, KeyError) as exc:
                result.warnings.append(f"{self.source.single_number_symbol} failed: {exc}")
        else:
            try:
                # Same procedure as the ISO 717-2 figure below, so keep the one
                # result object and read the rating off it.
                rating = rate(
                    working,
                    fraction=self.fraction,
                    quantity=self.source.single_number_symbol,
                )
                result.iso_717_2 = rating
                result.post_verification = float(rating.value)
            except (ValueError, KeyError) as exc:
                result.warnings.append(f"{self.source.single_number_symbol} failed: {exc}")

        # 2) KS F 2863: inverse-A curve
        if curve is not None:
            try:
                result.inverse_a = rate_with_curve(spectrum, curve)
                if not curve.verified:
                    result.warnings.append(curve.warning())
            except KeyError as exc:
                result.warnings.append(f"inverse-A rating failed: {exc}")

        # 3) ISO 717-2. For light impact this *is* the post-verification rating
        #    and was computed above; for heavy it is only a cross-reference.
        if result.iso_717_2 is None:
            try:
                result.iso_717_2 = rate(
                    working, fraction=self.fraction, quantity="ISO 717-2"
                )
            except (ValueError, KeyError):
                pass  # skip when the band set does not match what ISO requires

        return result

    # ── Save / restore ──
    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "title": self.title,
            "site": self.site,
            "operator": self.operator,
            "source": self.source.value,
            "created": self.created,
            "source_positions": self.source_positions,
            "channels": list(self.channels),
            "room": {
                "name": self.room.name,
                "volume": self.room.volume,
                "reverberation": {str(k): v for k, v in self.room.reverberation.items()},
            },
            "background": {str(k): v for k, v in self.background.items()},
            "calibration": dict(self.calibration),
            "notes": list(self.notes),
            "reverberation_records": list(self.reverberation_records),
            "measurements": [
                {**asdict(m), "levels": {str(k): v for k, v in m.levels.items()}}
                for m in self.measurements
            ],
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> Session:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        schema = data.get("schema", 1)
        if schema > SCHEMA_VERSION:
            raise ValueError(
                f"session schema {schema} is newer than this program (v{SCHEMA_VERSION})"
            )
        room_data = data.get("room", {})
        session = cls(
            title=data.get("title", ""),
            site=data.get("site", ""),
            operator=data.get("operator", ""),
            source=ImpactSource(data.get("source", "rubber_ball")),
            created=data.get("created", ""),
            source_positions=int(data.get("source_positions", 4)),
            channels=[int(c) for c in data.get("channels", [0, 1, 2, 3, 4])],
            room=Room(
                name=room_data.get("name", ""),
                volume=float(room_data.get("volume", 0.0)),
                reverberation={
                    float(k): float(v) for k, v in room_data.get("reverberation", {}).items()
                },
            ),
            background={float(k): float(v) for k, v in data.get("background", {}).items()},
            calibration=dict(data.get("calibration", {})),
            notes=list(data.get("notes", [])),
            reverberation_records=list(data.get("reverberation_records", [])),
        )
        for raw in data.get("measurements", []):
            session.measurements.append(
                Measurement(
                    source_position=int(raw["source_position"]),
                    # v1 schema used microphone_position — read it as the channel
                    channel=int(raw.get("channel", raw.get("microphone_position", 0))),
                    levels={float(k): float(v) for k, v in raw.get("levels", {}).items()},
                    broadband=raw.get("broadband", raw.get("a_weighted_max")),
                    quantity=raw.get("quantity", ""),
                    weighting=raw.get("weighting", "Z"),
                    timestamp=raw.get("timestamp", ""),
                    valid=bool(raw.get("valid", True)),
                    note=raw.get("note", ""),
                    fraction=int(raw.get("fraction", 3)),
                )
            )
        session.measurements.sort(key=lambda m: m.key)
        return session
