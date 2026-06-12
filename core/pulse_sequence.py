"""Pulse sequence flattening and validation helpers."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from typing import Any


MIN_TRIGGER_DELAY_S = 500e-9
MIN_DELAYLIST_AVERAGE_S = 50e-6


@dataclass(frozen=True)
class PulseEvent:
    """One source pulse followed by an idle interval."""

    level: float
    width_s: float
    interval_after_s: float

    @property
    def period_s(self) -> float:
        return self.width_s + self.interval_after_s


@dataclass(frozen=True)
class PulseTimelinePoint:
    """One timed list-sweep point for pulse-mode acquisition."""

    time_s: float
    source_level: float
    dwell_to_next_s: float


@dataclass(frozen=True)
class PulseListSweepSegment:
    """Quantized source-list metadata for one active or idle pulse segment."""

    event_index: int
    kind: str
    level: float
    duration_s: float
    point_count: int
    remainder_s: float


@dataclass(frozen=True)
class PulseListSweepPlan:
    """NPLC-derived source list for list-sweep pulse acquisition."""

    source_levels: list[float]
    point_interval_s: float
    measurement_window_s: float
    src_to_meas_delay_s: float
    total_points: int
    segments: list[PulseListSweepSegment]
    warnings: list[str]


def flatten_pulse_config(
    config: dict[str, Any] | None,
    *,
    repeat: int = 1,
) -> list[PulseEvent]:
    """Expand UI pulse combinations into a linear PulseEvent list."""
    if not isinstance(config, dict):
        return []

    combinations = config.get("combinations", [])
    if not isinstance(combinations, list):
        return []

    one_pass: list[PulseEvent] = []
    for combination in combinations:
        if not isinstance(combination, dict):
            continue
        combination_events = _flatten_items(combination.get("items", []))
        if combination_events:
            combination_interval = _float_value(
                combination.get("interval_after_s", 0.0),
                "combination interval_after_s",
            )
            combination_events[-1] = _add_interval(
                combination_events[-1],
                combination_interval,
            )
            combination_repeat = max(1, int(combination.get("repeat", 1)))
            for _ in range(combination_repeat):
                one_pass.extend(combination_events)

    repeats = max(1, int(repeat))
    return one_pass * repeats


def validate_pulse_events(events: list[PulseEvent]) -> None:
    """Validate event timing before programming the trigger model."""
    if not events:
        raise ValueError("Pulse sequence is empty.")

    widths = [event.width_s for event in events]
    periods = [event.period_s for event in events]
    for index, event in enumerate(events, start=1):
        if event.width_s <= 0.0:
            raise ValueError(f"Pulse {index} width must be greater than 0 s.")
        if event.interval_after_s < 0.0:
            raise ValueError(f"Pulse {index} interval_after_s must be >= 0 s.")
        if event.width_s < MIN_TRIGGER_DELAY_S:
            raise ValueError(
                f"Pulse {index} width is below the 500 ns trigger timer minimum."
            )
        if event.period_s < MIN_TRIGGER_DELAY_S:
            raise ValueError(
                f"Pulse {index} period is below the 500 ns trigger timer minimum."
            )

    if _average(widths) < MIN_DELAYLIST_AVERAGE_S:
        raise ValueError("Pulse width delaylist average must be at least 50 us.")
    if _average(periods) < MIN_DELAYLIST_AVERAGE_S:
        raise ValueError("Pulse period delaylist average must be at least 50 us.")


def build_pulse_timeline(
    events: list[PulseEvent],
    sample_interval_s: float,
) -> list[PulseTimelinePoint]:
    """
    Expand pulse events into a full timed list sweep.

    The returned points include every pulse start/end boundary and additional
    samples spaced by sample_interval_s inside both pulse and idle segments.
    """
    validate_pulse_events(events)
    try:
        sample_interval = float(sample_interval_s)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid pulse sample interval: {sample_interval_s!r}") from exc
    if sample_interval <= 0.0:
        raise ValueError("Pulse sample interval must be greater than 0 s.")

    segments: list[tuple[float, float, float]] = []
    current_time = 0.0
    for event in events:
        width = max(0.0, float(event.width_s))
        interval_after = max(0.0, float(event.interval_after_s))
        if width > 0.0:
            segments.append((current_time, current_time + width, float(event.level)))
            current_time += width
        if interval_after > 0.0:
            segments.append((current_time, current_time + interval_after, 0.0))
            current_time += interval_after

    if not segments or current_time <= 0.0:
        raise ValueError("Pulse sequence is empty.")

    times: set[float] = {0.0, _round_time(current_time)}
    for start_s, end_s, _level in segments:
        times.add(_round_time(start_s))
        times.add(_round_time(end_s))
        sample_time = start_s + sample_interval
        while sample_time < end_s - 1e-12:
            times.add(_round_time(sample_time))
            sample_time += sample_interval

    sorted_times = sorted(times)
    points: list[PulseTimelinePoint] = []
    for index, sample_time in enumerate(sorted_times):
        source_level = _level_at_time(sample_time, segments)
        if index + 1 < len(sorted_times):
            dwell_to_next = sorted_times[index + 1] - sample_time
        else:
            dwell_to_next = 0.0
        points.append(
            PulseTimelinePoint(
                time_s=_round_time(sample_time),
                source_level=float(source_level),
                dwell_to_next_s=_round_time(dwell_to_next),
            )
        )

    validate_pulse_timeline(points)
    return points


def build_pulse_list_sweep_plan(
    events: list[PulseEvent],
    *,
    nplc: float,
    line_frequency_hz: float,
    src_to_meas_delay_s: float,
) -> PulseListSweepPlan:
    """Flatten pulse events into an NPLC-paced source-level list sweep."""
    try:
        nplc_value = float(nplc)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid NPLC: {nplc!r}") from exc
    try:
        line_frequency = float(line_frequency_hz)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid line frequency: {line_frequency_hz!r}") from exc
    try:
        src_to_meas_delay = float(src_to_meas_delay_s)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid source-to-measure delay: {src_to_meas_delay_s!r}"
        ) from exc

    if nplc_value <= 0.0:
        raise ValueError("NPLC must be greater than 0.")
    if line_frequency <= 0.0:
        raise ValueError("Line frequency must be greater than 0 Hz.")

    measurement_window = nplc_value / line_frequency
    point_interval = measurement_window + src_to_meas_delay
    if point_interval <= 0.0:
        raise ValueError(
            "Point interval must be greater than 0 s for the current NPLC, "
            "line frequency, and source-to-measure delay."
        )
    if not events:
        raise ValueError("Pulse sequence is empty.")

    source_levels: list[float] = []
    segments: list[PulseListSweepSegment] = []
    warnings: list[str] = []

    for index, event in enumerate(events, start=1):
        width = float(event.width_s)
        interval_after = float(event.interval_after_s)
        if width <= 0.0:
            raise ValueError(f"Pulse {index} width must be greater than 0 s.")
        if interval_after < 0.0:
            raise ValueError(f"Pulse {index} interval_after_s must be >= 0 s.")

        active_count = _floor_interval_count(width, point_interval)
        if active_count < 1:
            raise ValueError(
                f"Pulse {index} width is too short for the current NPLC + "
                "source-to-measure delay."
            )

        active_remainder = _quantization_remainder(width, active_count, point_interval)
        source_levels.extend([float(event.level)] * active_count)
        segments.append(
            PulseListSweepSegment(
                event_index=index,
                kind="active",
                level=float(event.level),
                duration_s=width,
                point_count=active_count,
                remainder_s=active_remainder,
            )
        )
        _append_remainder_warning(
            warnings,
            index=index,
            kind="active",
            remainder_s=active_remainder,
        )

        idle_count = _floor_interval_count(interval_after, point_interval)
        idle_remainder = _quantization_remainder(
            interval_after,
            idle_count,
            point_interval,
        )
        source_levels.extend([0.0] * idle_count)
        segments.append(
            PulseListSweepSegment(
                event_index=index,
                kind="idle",
                level=0.0,
                duration_s=interval_after,
                point_count=idle_count,
                remainder_s=idle_remainder,
            )
        )
        _append_remainder_warning(
            warnings,
            index=index,
            kind="idle",
            remainder_s=idle_remainder,
        )

    return PulseListSweepPlan(
        source_levels=source_levels,
        point_interval_s=point_interval,
        measurement_window_s=measurement_window,
        src_to_meas_delay_s=src_to_meas_delay,
        total_points=len(source_levels),
        segments=segments,
        warnings=warnings,
    )


def pulse_source_levels_to_csv_rows(source_levels: list[float]) -> list[list[float]]:
    """Return source levels as single-column CSV rows."""
    return [[float(level)] for level in source_levels]


def pulse_source_levels_to_csv_string(source_levels: list[float]) -> str:
    """Return source levels as a single-column CSV string."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(pulse_source_levels_to_csv_rows(source_levels))
    return output.getvalue()


def pulse_list_sweep_levels_to_csv(source_levels: list[float]) -> str:
    """Return time-less pulse list-sweep source levels with a ``level`` header."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["level"])
    writer.writerows(pulse_source_levels_to_csv_rows(source_levels))
    return output.getvalue()


def validate_pulse_timeline(points: list[PulseTimelinePoint]) -> None:
    """Validate timed-list delays for Keithley 2600B trigger.timer.delaylist."""
    if not points:
        raise ValueError("Pulse sequence is empty.")
    if len(points) == 1:
        raise ValueError("Pulse timeline must contain at least two points.")

    dwell_values = [float(point.dwell_to_next_s) for point in points[:-1]]
    for index, dwell_s in enumerate(dwell_values, start=1):
        if dwell_s < MIN_TRIGGER_DELAY_S:
            raise ValueError(
                f"Pulse timeline dwell {index} is below the 500 ns trigger timer minimum."
            )

    if _average(dwell_values) < MIN_DELAYLIST_AVERAGE_S:
        raise ValueError("Pulse timeline delaylist average must be at least 50 us.")


def _flatten_items(items: Any) -> list[PulseEvent]:
    if not isinstance(items, list):
        return []

    events: list[PulseEvent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pulse_type = str(item.get("type", "single")).strip().lower()
        if pulse_type == "train":
            nested = _flatten_items(item.get("items", []))
            if nested:
                train_interval = _float_value(
                    item.get("interval_after_s", 0.0),
                    "train interval_after_s",
                )
                nested[-1] = _add_interval(nested[-1], train_interval)
                events.extend(nested)
            continue

        magnitude = _float_value(item.get("magnitude", 0.0), "pulse magnitude")
        width_s = _float_value(item.get("duration_s", 0.0), "pulse duration_s")
        if pulse_type == "paired":
            interval_s = _float_value(item.get("interval_s", 0.0), "paired interval_s")
            interval_after_s = _float_value(
                item.get("interval_after_s", 0.0),
                "paired interval_after_s",
            )
            events.append(PulseEvent(magnitude, width_s, interval_s))
            events.append(PulseEvent(magnitude, width_s, interval_after_s))
        else:
            interval_after_s = _float_value(
                item.get("interval_after_s", 0.0),
                "pulse interval_after_s",
            )
            events.append(PulseEvent(magnitude, width_s, interval_after_s))

    return events


def _add_interval(event: PulseEvent, extra_interval_s: float) -> PulseEvent:
    return PulseEvent(
        event.level,
        event.width_s,
        event.interval_after_s + extra_interval_s,
    )


def _float_value(value: Any, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {label}: {value!r}") from exc


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _quantization_remainder(
    duration_s: float,
    point_count: int,
    point_interval_s: float,
) -> float:
    return _round_time(max(0.0, duration_s - point_count * point_interval_s))


def _floor_interval_count(duration_s: float, point_interval_s: float) -> int:
    return math.floor((duration_s + 1e-12) / point_interval_s)


def _append_remainder_warning(
    warnings: list[str],
    *,
    index: int,
    kind: str,
    remainder_s: float,
) -> None:
    if remainder_s > 1e-12:
        warnings.append(
            f"Pulse {index} {kind} segment has {remainder_s:g} s remainder "
            "not represented after NPLC point-interval quantization."
        )


def _round_time(value: float) -> float:
    return round(float(value), 12)


def _level_at_time(
    sample_time: float,
    segments: list[tuple[float, float, float]],
) -> float:
    for start_s, end_s, level in segments:
        if start_s - 1e-12 <= sample_time < end_s - 1e-12:
            return float(level)
    return 0.0
