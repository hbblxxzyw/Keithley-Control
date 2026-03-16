"""
Pyqtgraph-based plot widgets: preview waveform (Settings tab) and I-V measurement (Graph tab).
"""

import numpy as np
import pyqtgraph as pg
from pyqtgraph import PlotDataItem


class PreviewGraphWidget(pg.GraphicsLayoutWidget):
    """
    Preview widget for the Settings tab: two stacked plots (SMU 1 / SMU 2).

    Uses GraphicsLayoutWidget with two independent plot areas; X axes are linked
    for synchronized time zooming. update_preview() pushes generated arrays to
    each subplot.
    """

    def __init__(self, parent=None, **kwargs) -> None:
        super().__init__(parent=parent, **kwargs)
        self.setBackground("#2b2b2b")

        # Top: SMU 1 (red), hide X axis labels to save space
        self.plot_smu1 = self.addPlot(row=0, col=0)
        self.plot_smu1.showGrid(x=True, y=True, alpha=0.3)
        self.plot_smu1.setLabel("left", "SMU 1 Amp")
        self.plot_smu1.getAxis("bottom").setStyle(showValues=False)
        self.line_smu1 = self.plot_smu1.plot(pen=pg.mkPen("r", width=2))
        self.line_smu1.setData([], [])

        # Bottom: SMU 2 (blue), shared X axis label, linked to plot_smu1
        self.plot_smu2 = self.addPlot(row=1, col=0)
        self.plot_smu2.setXLink(self.plot_smu1)
        self.plot_smu2.showGrid(x=True, y=True, alpha=0.3)
        self.plot_smu2.setLabel("left", "SMU 2 Amp")
        self.plot_smu2.setLabel("bottom", "Time (s)")
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
        """
        time = np.linspace(0, duration, num_points)
        y1 = self._build_amplitude(time, smu1_cfg, duration)
        y2 = self._build_amplitude(time, smu2_cfg, duration)
        self.line_smu1.setData(time.tolist(), y1.tolist())
        self.line_smu2.setData(time.tolist(), y2.tolist())

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
            pen = pg.mkPen(
                palette[color_idx % len(palette)],
                width=2,
            )
            self._color_index[smu_name] = color_idx + 1
            curve = self.plot(pen=pen, name=f"{smu_name} | {series_name}")
            self._series[key] = curve
            self._data[key] = ([], [])
            curve.setData([], [])
        xs, ys = self._data[key]
        xs.append(x_val)
        ys.append(y_val)
        self._series[key].setData(xs, ys)

    def clear_plot(self) -> None:
        """Clear all series data."""
        for key in self._series:
            self._data[key] = ([], [])
            self._series[key].setData([], [])
        self._color_index = {"SMU 1": 0, "SMU 2": 0}
