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
