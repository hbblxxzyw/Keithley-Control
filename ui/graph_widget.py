"""
Pyqtgraph-based plot widgets: preview waveform (Settings tab) and I-V measurement (Graph tab).
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QToolTip
from pyqtgraph import PlotDataItem


def _normalized_pulse_items(items: object) -> list[dict]:
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _append_pulse_level(
    times: list[float],
    values: list[float],
    current_time: float,
    level: float,
    duration_s: float,
) -> float:
    duration_s = max(0.0, float(duration_s))
    if values[-1] != level:
        times.append(current_time)
        values.append(level)
    current_time += duration_s
    times.append(current_time)
    values.append(level)
    if values[-1] != 0.0:
        times.append(current_time)
        values.append(0.0)
    return current_time


def _append_pulse_items(
    times: list[float],
    values: list[float],
    items: list[dict],
    current_time: float,
) -> float:
    for pulse in _normalized_pulse_items(items):
        pulse_type = str(pulse.get("type", "single")).strip().lower()
        if pulse_type == "train":
            current_time = _append_pulse_items(
                times,
                values,
                _normalized_pulse_items(pulse.get("items", [])),
                current_time,
            )
            interval_after = max(0.0, float(pulse.get("interval_after_s", 0.0)))
            current_time += interval_after
            times.append(current_time)
            values.append(0.0)
            continue

        magnitude = float(pulse.get("magnitude", 0.0))
        duration_s = float(pulse.get("duration_s", 0.0))
        current_time = _append_pulse_level(
            times, values, current_time, magnitude, duration_s
        )

        if pulse_type == "paired":
            pair_interval_s = max(0.0, float(pulse.get("interval_s", 0.0)))
            current_time += pair_interval_s
            times.append(current_time)
            values.append(0.0)
            current_time = _append_pulse_level(
                times, values, current_time, magnitude, duration_s
            )

        interval_after = max(0.0, float(pulse.get("interval_after_s", 0.0)))
        current_time += interval_after
        times.append(current_time)
        values.append(0.0)

    return current_time


def build_full_pulse_waveform(pulse_config: dict | None, repeat: int = 1) -> tuple[list[float], list[float]]:
    times: list[float] = [0.0]
    values: list[float] = [0.0]
    if not isinstance(pulse_config, dict):
        return [0.0, 1.0], [0.0, 0.0]

    combinations = pulse_config.get("combinations", [])
    if not isinstance(combinations, list) or not combinations:
        return [0.0, 1.0], [0.0, 0.0]

    current_time = 0.0
    for _ in range(max(1, int(repeat))):
        for combination in combinations:
            if not isinstance(combination, dict):
                continue
            combination_repeat = max(1, int(combination.get("repeat", 1)))
            for _ in range(combination_repeat):
                current_time = _append_pulse_items(
                    times,
                    values,
                    _normalized_pulse_items(combination.get("items", [])),
                    current_time,
                )
                interval_after = max(
                    0.0, float(combination.get("interval_after_s", 0.0))
                )
                current_time += interval_after
                times.append(current_time)
                values.append(0.0)

    if current_time <= 0.0:
        return [0.0, 1.0], [0.0, 0.0]
    return times, values


class PlainAxisItem(pg.AxisItem):
    """Axis item that always renders plain values without SI scaling."""

    def __init__(self, orientation: str, suffix: str = "") -> None:
        super().__init__(orientation=orientation)
        self._suffix = suffix
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):  # type: ignore[override]
        labels: list[str] = []
        decimals = 3 if spacing < 1 else 2
        for value in values:
            text = f"{float(value):.{decimals}f}".rstrip("0").rstrip(".")
            if text == "-0":
                text = "0"
            labels.append(f"{text}{self._suffix}")
        return labels


class PreviewGraphWidget(pg.GraphicsLayoutWidget):
    """
    Preview widget for the Settings tab: two stacked plots (SMU 1 / SMU 2).

    Uses GraphicsLayoutWidget with two independent plot areas; X axes are linked
    for synchronized time zooming. update_preview() pushes generated arrays to
    each subplot.
    """

    def __init__(self, parent=None, **kwargs) -> None:
        super().__init__(parent=parent, **kwargs)
        self.setBackground("w")
        self.setContentsMargins(0, 0, 0, 0)
        self.ci.layout.setContentsMargins(4, 4, 4, 4)
        self.ci.layout.setSpacing(2)

        preview_axes = {
            "bottom": PlainAxisItem("bottom", suffix="s"),
            "left": PlainAxisItem("left"),
        }

        # Top: SMU 1 (red), hide X tick labels to save space
        self.plot_smu1 = self.addPlot(row=0, col=0, axisItems=preview_axes)
        self.plot_smu1.showGrid(x=True, y=True, alpha=0.12)
        self.plot_smu1.showAxis("top", False)
        self.plot_smu1.showAxis("right", False)
        self.plot_smu1.getAxis("left").setTextPen("#5f6368")
        self.plot_smu1.getAxis("bottom").setTextPen("#5f6368")
        self.plot_smu1.getAxis("left").setPen("#d0d7de")
        self.plot_smu1.getAxis("bottom").setPen("#d0d7de")
        self.plot_smu1.getAxis("bottom").setStyle(showValues=False)
        self.plot_smu1.setMenuEnabled(False)
        self.plot_smu1.setMouseEnabled(x=False, y=False)
        self.plot_smu1.setContentsMargins(0, 0, 0, 0)
        self.plot_smu1.getViewBox().setDefaultPadding(0.02)
        self.line_smu1 = self.plot_smu1.plot(pen=pg.mkPen("r", width=2))
        self.line_smu1.setData([], [])

        # Bottom: SMU 2 (blue), linked to plot_smu1
        self.plot_smu2 = self.addPlot(
            row=1,
            col=0,
            axisItems={
                "bottom": PlainAxisItem("bottom", suffix="s"),
                "left": PlainAxisItem("left"),
            },
        )
        self.plot_smu2.setXLink(self.plot_smu1)
        self.plot_smu2.showGrid(x=True, y=True, alpha=0.12)
        self.plot_smu2.showAxis("top", False)
        self.plot_smu2.showAxis("right", False)
        self.plot_smu2.getAxis("left").setTextPen("#5f6368")
        self.plot_smu2.getAxis("bottom").setTextPen("#5f6368")
        self.plot_smu2.getAxis("left").setPen("#d0d7de")
        self.plot_smu2.getAxis("bottom").setPen("#d0d7de")
        self.plot_smu2.setMenuEnabled(False)
        self.plot_smu2.setMouseEnabled(x=False, y=False)
        self.plot_smu2.setContentsMargins(0, 0, 0, 0)
        self.plot_smu2.getViewBox().setDefaultPadding(0.02)
        self.line_smu2 = self.plot_smu2.plot(pen=pg.mkPen("b", width=2))
        self.line_smu2.setData([], [])

    def update_preview(
        self,
        smu1_cfg: dict,
        smu2_cfg: dict,
        duration: float = 1.0,
        num_points: int = 500,
    ) -> None:
        """
        Update preview curves from SMU configs.

        Config format:
        - Fixed: {"mode": "fixed", "level": float}
        - Sweep: {"mode": "sweep", "start": float, "stop": float, "points": int, "dual": bool}
        - Pulse: {"mode": "pulse", "pulse": dict, "repeat": int}
        """
        duration = max(
            float(duration),
            self._config_duration(smu1_cfg),
            self._config_duration(smu2_cfg),
        )
        time = np.linspace(0, duration, num_points)
        x1, y1 = self._build_preview_data(time, smu1_cfg, duration)
        x2, y2 = self._build_preview_data(time, smu2_cfg, duration)
        self.line_smu1.setData(x1, y1)
        self.line_smu2.setData(x2, y2)

    def _build_preview_data(
        self, time: np.ndarray, cfg: dict, duration: float
    ) -> tuple[list[float], list[float]]:
        if cfg.get("mode") == "pulse":
            pulse_times, pulse_values = build_full_pulse_waveform(
                cfg.get("pulse"),
                repeat=int(cfg.get("repeat", 1)),
            )
            if pulse_times and pulse_times[-1] < duration:
                pulse_times = list(pulse_times) + [duration]
                pulse_values = list(pulse_values) + [pulse_values[-1]]
            return pulse_times, pulse_values

        return (
            time.tolist(),
            self._build_amplitude(time, cfg, duration).tolist(),
        )

    def _config_duration(self, cfg: dict) -> float:
        if cfg.get("mode") != "pulse":
            return 0.0
        times, _ = build_full_pulse_waveform(
            cfg.get("pulse"),
            repeat=int(cfg.get("repeat", 1)),
        )
        return max(times) if times else 0.0

    def _build_amplitude(
        self, time: np.ndarray, cfg: dict, duration: float
    ) -> np.ndarray:
        mode = cfg.get("mode", "fixed")
        if mode == "fixed":
            level = float(cfg.get("level", 0.0))
            return np.full_like(time, level)
        if mode == "sweep":
            start = float(cfg.get("start", 0.0))
            stop = float(cfg.get("stop", 0.0))
            points = int(cfg.get("points", 0))
            dual = bool(cfg.get("dual", False))
            if points <= 1:
                return np.full_like(time, start)
            voltages = np.linspace(start, stop, points)
            if dual and len(voltages) > 1:
                # Reverse back without duplicating peak: exclude first point of reverse
                voltages = np.concatenate((voltages, voltages[-2::-1]))
            n_steps = len(voltages)
            if n_steps == 0:
                return np.full_like(time, start)
            step_duration = duration / n_steps
            indices = np.clip(
                (time / step_duration).astype(int), 0, n_steps - 1
            )
            return voltages[indices]
        if mode == "pulse":
            pulse_times, pulse_values = build_full_pulse_waveform(
                cfg.get("pulse"),
                repeat=int(cfg.get("repeat", 1)),
            )
            indices = np.searchsorted(pulse_times, time, side="right") - 1
            indices = np.clip(indices, 0, len(pulse_values) - 1)
            return np.asarray([pulse_values[index] for index in indices], dtype=float)
        return np.zeros_like(time)


class MeasurementGraphWidget(pg.PlotWidget):
    """
    Plot widget for the Graph tab: real I-V measurement data.

    Uses a single plot with separate series for SMU 1 and SMU 2.
    All curves share the same sweep-voltage X axis.
    """

    def __init__(self, parent=None, **kwargs) -> None:
        super().__init__(parent=parent, **kwargs)
        self.setBackground("w")
        self.showGrid(x=True, y=True, alpha=0.5)
        self.setLabel("left", "Current (A)")
        self.setLabel("bottom", "Sweep Voltage (V)")
        self.addLegend()

        self._series: dict[tuple[str, str], PlotDataItem] = {}
        self._data: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
        self._color_index: dict[str, int] = {"SMU 1": 0, "SMU 2": 0}
        self._display_mode = "linear"
        self._hover_distance_px = 10.0
        self._hover_proxy = pg.SignalProxy(
            self.scene().sigMouseMoved,
            rateLimit=60,
            slot=self._handle_mouse_moved,
        )
        self._palette = {
            "SMU 1": ["#1565c0", "#00838f", "#3949ab", "#0277bd"],
            "SMU 2": ["#c62828", "#ef6c00", "#ad1457", "#6d4c41"],
        }

    def append_data_point(
        self, smu_name: str, x_val: float, y_val: float, series_name: str
    ) -> None:
        """Append one point to the given SMU series on the shared plot."""
        key = (smu_name, series_name)
        if key not in self._series:
            palette = self._palette.get(smu_name, ["#424242"])
            color_idx = self._color_index.get(smu_name, 0)
            color = palette[color_idx % len(palette)]
            pen = pg.mkPen(
                color,
                width=2,
            )
            self._color_index[smu_name] = color_idx + 1
            curve = self.plot(
                pen=pen,
                name=f"{smu_name} | {series_name}",
                symbol="o",
                symbolSize=7,
                symbolBrush=pg.mkBrush(color),
                symbolPen=pg.mkPen(color, width=1),
            )
            self._series[key] = curve
            self._data[key] = ([], [])
            curve.setData([], [])
        xs, ys = self._data[key]
        xs.append(x_val)
        ys.append(y_val)
        self._refresh_curve(key)

    def set_display_mode(self, mode: str) -> None:
        """Switch between linear current display and log absolute-current display."""
        normalized = str(mode or "linear").strip().lower()
        self._display_mode = "log" if normalized == "log" else "linear"
        self.setLogMode(x=False, y=self._display_mode == "log")
        if self._display_mode == "log":
            self.setLabel("left", "|Current| (A)")
        else:
            self.setLabel("left", "Current (A)")
        for key in self._series:
            self._refresh_curve(key)

    def _refresh_curve(self, key: tuple[str, str]) -> None:
        xs, ys = self._data[key]
        if self._display_mode == "log":
            log_ys = [abs(float(y)) for y in ys if abs(float(y)) > 0.0]
            log_xs = [x for x, y in zip(xs, ys) if abs(float(y)) > 0.0]
            self._series[key].setData(log_xs, log_ys)
            return
        self._series[key].setData(xs, ys)

    def _handle_mouse_moved(self, event) -> None:
        scene_pos = event[0]
        if not self.plotItem.sceneBoundingRect().contains(scene_pos):
            QToolTip.hideText()
            return

        nearest = self._nearest_plotted_point(scene_pos)
        if nearest is None:
            QToolTip.hideText()
            return

        smu_name, series_name, x_val, y_val = nearest
        widget_pos = self.mapFromScene(scene_pos)
        global_pos = self.mapToGlobal(widget_pos)
        QToolTip.showText(
            global_pos,
            (
                f"{smu_name} | {series_name}\n"
                f"X: {self._format_axis_value(x_val)} V\n"
                f"Y: {self._format_axis_value(y_val)} A"
            ),
            self,
        )

    def _nearest_plotted_point(
        self, scene_pos: QPointF
    ) -> tuple[str, str, float, float] | None:
        best: tuple[float, str, str, float, float] | None = None
        for key, curve in self._series.items():
            x_data, y_data = curve.getData()
            if x_data is None or y_data is None:
                continue
            for x_val, y_val in zip(x_data, y_data):
                try:
                    x_float = float(x_val)
                    y_float = float(y_val)
                except (TypeError, ValueError):
                    continue
                if not np.isfinite(x_float) or not np.isfinite(y_float):
                    continue
                point_pos = self.plotItem.vb.mapViewToScene(
                    QPointF(x_float, y_float)
                )
                distance = (
                    (point_pos.x() - scene_pos.x()) ** 2
                    + (point_pos.y() - scene_pos.y()) ** 2
                ) ** 0.5
                if distance <= self._hover_distance_px and (
                    best is None or distance < best[0]
                ):
                    smu_name, series_name = key
                    best = (distance, smu_name, series_name, x_float, y_float)

        if best is None:
            return None
        _, smu_name, series_name, x_val, y_val = best
        return smu_name, series_name, x_val, y_val

    @staticmethod
    def _format_axis_value(value: float) -> str:
        return f"{float(value):.6g}"

    def clear_plot(self) -> None:
        """Clear all series and legend entries."""
        for curve in list(self._series.values()):
            self.removeItem(curve)
        legend = self.plotItem.legend
        if legend is not None:
            legend.clear()
        self._series.clear()
        self._data.clear()
        self._color_index = {"SMU 1": 0, "SMU 2": 0}
        self.autoscale()

    def autoscale(self) -> None:
        """Autoscale both axes to the currently plotted data."""
        self.plotItem.autoRange()
