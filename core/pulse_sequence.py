"""Pulse sequence flattening and validation helpers."""

from __future__ import annotations

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
