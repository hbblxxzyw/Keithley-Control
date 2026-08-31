"""
Main controller: connects MainWindowUI and AbstractSMU, binds signals and slots.
"""

import csv
import json
import math
import time
import traceback
from typing import TYPE_CHECKING, Any, Dict

import numpy as np
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QTableWidgetItem,
)

from core.instrument_base import AbstractSMU
from core.pulse_sequence import (
    PulseTimelinePoint,
    build_pulse_list_sweep_plan,
    build_pulse_timeline,
    flatten_pulse_config,
    pulse_list_sweep_levels_to_csv,
)
from models.project import (
    MAX_QUICK_CONFIG_NAME_LENGTH,
    RunRecord,
    WorkspaceDocument,
    clone_json,
)
from models.run_data_cache import RunCacheKey, RunDataCache

if TYPE_CHECKING:
    from ui.main_window_ui import MainWindowUI


class SweepWorker(QThread):
    """
    Background worker thread to run sweeps without blocking the UI.

    It receives an instrument instance plus a parameter dictionary and emits
    data_ready for each acquired point and finished_sweep when done.
    """

    data_ready = Signal(dict)
    error_occurred = Signal(str)
    finished_sweep = Signal()
    def __init__(
        self,
        instrument: AbstractSMU,
        params: Dict[str, Any],
        parent: "MainWindowUI | None" = None,
    ) -> None:
        super().__init__(parent)
        self.instrument = instrument
        self.params = params

    def run(self) -> None:
        p = self.params

        primary_name: str = p["primary_name"]
        stepper_name: str = p["stepper_name"]
        primary_channel: str = p["primary_channel"]
        stepper_channel: str = p["stepper_channel"]
        pri_start: float = p["pri_start"]
        pri_stop: float = p["pri_stop"]
        primary_limit: float = p["primary_limit"]
        stepper_level: float = p["stepper_level"]
        stepper_limit: float = p["stepper_limit"]
        stepper_start: float = p["stepper_start"]
        stepper_stop: float = p["stepper_stop"]
        stepper_mode: str = p["stepper_mode"]
        points: int = p["points"]
        stepper_points_assigned: int = p["stepper_points_assigned"]
        src_meas_delay: float = p["src_meas_delay"]
        step_sweep_delay: float = p["step_sweep_delay"]
        nplc: float = p["nplc"]
        primary_dual: bool = p["primary_dual"]
        stepper_dual: bool = p["stepper_dual"]
        ramp_up: bool = p["ramp_up"]
        ru_step: float = p["ru_step"]
        ru_delay: float = p["ru_delay"]
        ramp_down: bool = p["ramp_down"]
        rd_step: float = p["rd_step"]
        rd_delay: float = p["rd_delay"]
        measure_cfg: Dict[str, Dict[str, Any]] = p["measure_cfg"]
        enabled_channels: list[str] = list(p.get("enabled_channels", ["smua", "smub"]))
        single_smu: bool = bool(p.get("single_smu", False))

        delay_s = max(0.0, src_meas_delay)
        step_sweep_delay_s = max(0.0, step_sweep_delay)

        secondary_channel = "smub" if primary_channel == "smua" else "smua"

        def abort_if_requested() -> None:
            if not self.isInterruptionRequested():
                return
            try:
                self.instrument.abort_sweep()
            except Exception:
                pass
            raise InterruptedError("Sweep aborted by user.")

        def sleep_with_abort(duration_s: float) -> None:
            remaining_s = max(0.0, float(duration_s))
            while remaining_s > 0.0:
                abort_if_requested()
                sleep_slice_s = min(0.05, remaining_s)
                time.sleep(sleep_slice_s)
                remaining_s -= sleep_slice_s

        def build_channel_values(
            requested_items: list[str],
            source_values: list[float],
            current_values: list[float],
            measured_voltage_values: list[float] | None,
        ) -> list[dict[str, float]]:
            values_per_point: list[dict[str, float]] = []
            for index, (source_voltage, current) in enumerate(zip(source_values, current_values)):
                measured_voltage = None
                if measured_voltage_values is not None and index < len(measured_voltage_values):
                    measured_voltage = float(measured_voltage_values[index])
                point_voltage = (
                    measured_voltage if measured_voltage is not None else float(source_voltage)
                )
                point_values: dict[str, float] = {"Voltage": point_voltage}
                if "Current" in requested_items:
                    point_values["Current"] = float(current)
                if "Resistance" in requested_items:
                    if abs(current) > 1e-15:
                        point_values["Resistance"] = point_voltage / float(current)
                    else:
                        point_values["Resistance"] = float("inf")
                values_per_point.append(point_values)
            return values_per_point

        def build_payload(
            started_at: float,
            series_name: str,
            primary_setpoint: float,
            secondary_setpoint: float,
            primary_values: dict[str, float],
            secondary_values: dict[str, float],
            sample_timestamp: float | None = None,
            sweep_phase: str = "sweep",
        ) -> dict[str, object]:
            smu1_values = primary_values if primary_channel == "smua" else secondary_values
            smu2_values = primary_values if primary_channel == "smub" else secondary_values
            time_s = (
                float(sample_timestamp)
                if sample_timestamp is not None
                else time.monotonic() - started_at
            )
            payload: dict[str, object] = {
                "time_s": time_s,
                "series_name": series_name,
                "primary_name": primary_name,
                "stepper_name": stepper_name,
                "sweep_phase": sweep_phase,
                "is_ramp": sweep_phase in {"ramp_up", "ramp_down"},
                "smu1": {
                    "source_v": primary_setpoint if primary_channel == "smua" else secondary_setpoint,
                    "values": smu1_values,
                },
                "smu2": {
                    "source_v": primary_setpoint if primary_channel == "smub" else secondary_setpoint,
                    "values": smu2_values,
                },
            }
            if sample_timestamp is not None:
                payload["sample_t_s"] = sample_timestamp
            return payload

        def emit_single_chunk_payloads(
            started_at: float,
            series_name: str,
            source_values: list[float],
            current_values: list[float],
            measured_voltage_values: list[float] | None,
            timestamp_values: list[float],
            phase_values: list[str],
        ) -> int:
            primary_points = build_channel_values(
                list(measure_cfg[primary_channel]["items"]),
                source_values,
                current_values,
                measured_voltage_values,
            )
            emitted_points = 0
            for index, (source_value, primary_values) in enumerate(
                zip(source_values, primary_points)
            ):
                payload = build_payload(
                    started_at,
                    series_name,
                    float(source_value),
                    0.0,
                    primary_values,
                    {},
                    float(timestamp_values[index]) if index < len(timestamp_values) else None,
                    phase_values[index] if index < len(phase_values) else "sweep",
                )
                self.data_ready.emit(payload)
                emitted_points += 1
            return emitted_points

        def emit_chunk_payloads(
            started_at: float,
            series_name: str,
            primary_source_values: list[float],
            primary_current_values: list[float],
            primary_voltage_values: list[float] | None,
            secondary_source_values: list[float],
            secondary_current_values: list[float],
            secondary_voltage_values: list[float] | None,
            timestamp_values: list[float],
            phase_values: list[str],
        ) -> int:
            primary_points = build_channel_values(
                list(measure_cfg[primary_channel]["items"]),
                primary_source_values,
                primary_current_values,
                primary_voltage_values,
            )
            secondary_points = build_channel_values(
                list(measure_cfg[secondary_channel]["items"]),
                secondary_source_values,
                secondary_current_values,
                secondary_voltage_values,
            )
            emitted_points = 0
            for index, (primary_source_value, secondary_source_value, primary_values, secondary_values) in enumerate(zip(
                primary_source_values,
                secondary_source_values,
                primary_points,
                secondary_points,
            )):
                payload = build_payload(
                    started_at,
                    series_name,
                    float(primary_source_value),
                    float(secondary_source_value),
                    primary_values,
                    secondary_values,
                    float(timestamp_values[index]) if index < len(timestamp_values) else None,
                    phase_values[index] if index < len(phase_values) else "sweep",
                )
                self.data_ready.emit(payload)
                emitted_points += 1
            return emitted_points

        def ramp_point_count(enabled: bool, endpoint_v: float, step_v: float) -> int:
            if not enabled or abs(endpoint_v) <= 0.0:
                return 0
            return max(2, int(math.ceil(abs(endpoint_v) / max(step_v, 1e-9))) + 1)

        def build_phase_tracker(
            sweep_start: float,
            sweep_stop: float,
            sweep_points: int,
            use_ramp_up: bool,
            use_ramp_down: bool,
        ):
            segments: list[tuple[str, int]] = []
            ramp_up_points = ramp_point_count(use_ramp_up, sweep_start, ru_step)
            if ramp_up_points > 0:
                segments.append(("ramp_up", ramp_up_points))
            segments.append(("sweep", max(0, int(sweep_points))))
            ramp_down_points = ramp_point_count(use_ramp_down, sweep_stop, rd_step)
            if ramp_down_points > 0:
                segments.append(("ramp_down", ramp_down_points))

            segment_index = 0
            emitted_in_segment = 0

            def next_phases(point_count: int) -> list[str]:
                nonlocal segment_index, emitted_in_segment
                phases: list[str] = []
                for _ in range(max(0, int(point_count))):
                    while segment_index < len(segments) and emitted_in_segment >= segments[segment_index][1]:
                        segment_index += 1
                        emitted_in_segment = 0
                    phase = segments[segment_index][0] if segment_index < len(segments) else "sweep"
                    phases.append(phase)
                    emitted_in_segment += 1
                return phases

            return next_phases

        def stream_iv_pass(
            started_at: float,
            series_name: str,
            stepper_setpoint: float,
            sweep_start: float,
            sweep_stop: float,
            sweep_points: int,
            use_ramp_up: bool,
            use_ramp_down: bool,
        ) -> int:
            emitted_points = 0
            next_phases = build_phase_tracker(
                sweep_start,
                sweep_stop,
                sweep_points,
                use_ramp_up,
                use_ramp_down,
            )
            abort_if_requested()
            if single_smu:
                for (
                    source_chunk,
                    current_chunk,
                    voltage_chunk,
                    timestamp_chunk,
                ) in self.instrument.run_single_smu_sweep(
                    primary_channel,
                    sweep_start,
                    sweep_stop,
                    sweep_points,
                    delay_s,
                    nplc,
                    primary_limit,
                    list(measure_cfg[primary_channel]["items"]),
                    use_ramp_up,
                    ru_step,
                    ru_delay,
                    use_ramp_down,
                    rd_step,
                    rd_delay,
                    abort_if_requested,
                ):
                    abort_if_requested()
                    source_values = list(source_chunk)
                    emitted_points += emit_single_chunk_payloads(
                        started_at,
                        series_name,
                        source_values,
                        list(current_chunk),
                        None if voltage_chunk is None else list(voltage_chunk),
                        list(timestamp_chunk),
                        next_phases(len(source_values)),
                    )
                return emitted_points

            for (
                primary_source_chunk,
                primary_current_chunk,
                primary_voltage_chunk,
                secondary_source_chunk,
                secondary_current_chunk,
                secondary_voltage_chunk,
                timestamp_chunk,
            ) in self.instrument.run_iv_sweep(
                primary_channel,
                sweep_start,
                sweep_stop,
                sweep_points,
                delay_s,
                nplc,
                primary_limit,
                list(measure_cfg[primary_channel]["items"]),
                use_ramp_up,
                ru_step,
                ru_delay,
                use_ramp_down,
                rd_step,
                rd_delay,
                "fixed",
                stepper_setpoint,
                None,
                None,
                stepper_limit,
                abort_if_requested,
            ):
                abort_if_requested()
                primary_source_values = list(primary_source_chunk)
                emitted_points += emit_chunk_payloads(
                    started_at,
                    series_name,
                    primary_source_values,
                    list(primary_current_chunk),
                    None if primary_voltage_chunk is None else list(primary_voltage_chunk),
                    list(secondary_source_chunk),
                    list(secondary_current_chunk),
                    None if secondary_voltage_chunk is None else list(secondary_voltage_chunk),
                    list(timestamp_chunk),
                    next_phases(len(primary_source_values)),
                )
            return emitted_points

        def run_primary_sweep(series_name: str, stepper_setpoint: float) -> None:
            started_at = time.monotonic()
            stream_iv_pass(
                started_at,
                series_name,
                stepper_setpoint,
                pri_start,
                pri_stop,
                points,
                ramp_up,
                False if primary_dual else ramp_down,
            )
            if primary_dual and points > 1:
                step_v = (pri_stop - pri_start) / float(points - 1)
                reverse_start = pri_stop - step_v
                stream_iv_pass(
                    started_at,
                    series_name,
                    stepper_setpoint,
                    reverse_start,
                    pri_start,
                    points - 1,
                    False,
                    ramp_down,
                )

        try:
            abort_if_requested()
            for channel in enabled_channels:
                self.instrument.configure_measurement(
                    channel,
                    list(measure_cfg[channel]["items"]),
                    str(measure_cfg[channel]["range"]),
                    str(measure_cfg[channel]["autozero"]),
                    nplc,
                )
            if single_smu:
                self.instrument.set_output(secondary_channel, False)
                series_name = f"{primary_name} (Single)"
                run_primary_sweep(series_name, 0.0)
                return

            if stepper_mode == "fixed":
                # ----- Branch A: fixed stepper bias -----
                self.instrument.set_voltage_source(
                    stepper_channel, stepper_level, stepper_limit
                )
                self.instrument.set_output(stepper_channel, True)
                if step_sweep_delay_s > 0:
                    sleep_with_abort(step_sweep_delay_s)

                series_name = f"{primary_name} (Bias={stepper_level:.2f}V)"
                run_primary_sweep(series_name, stepper_level)
            else:
                # ----- Branch B: swept stepper family of curves -----
                n_stepper = max(1, int(stepper_points_assigned))
                if n_stepper == 1:
                    stepper_vals = np.array([float(stepper_start)])
                else:
                    stepper_vals = np.linspace(
                        float(stepper_start),
                        float(stepper_stop),
                        n_stepper,
                    )
                    if stepper_dual:
                        stepper_vals = np.concatenate(
                            (stepper_vals, stepper_vals[-2::-1])
                        )
                for step_val in stepper_vals:
                    abort_if_requested()
                    self.instrument.set_voltage_source(
                        stepper_channel, float(step_val), stepper_limit
                    )
                    self.instrument.set_output(stepper_channel, True)
                    if step_sweep_delay_s > 0:
                        sleep_with_abort(step_sweep_delay_s)

                    series_name = f"{primary_name} (Step={step_val:.2f}V)"
                    run_primary_sweep(series_name, float(step_val))
        except InterruptedError:
            print("Sweep aborted by user.")
        except Exception as exc:
            print(f"Sweep failed: {exc}")
            try:
                error_queue = list(self.instrument.dump_errors())
            except Exception as error_exc:
                print(f"Failed to read instrument error queue: {error_exc}")
            else:
                if error_queue:
                    print("Instrument error queue:")
                    for index, error_entry in enumerate(error_queue, start=1):
                        print(f"  {index}. {error_entry}")
                else:
                    print("Instrument error queue is empty.")
            traceback.print_exc()
            self.error_occurred.emit(str(exc))
        finally:
            # Ensure outputs are turned off and signal completion
            try:
                self.instrument.set_output("smua", False)
            except Exception:
                pass
            try:
                self.instrument.set_output("smub", False)
            except Exception:
                pass
            self.finished_sweep.emit()


class PulseWorker(QThread):
    """Background worker for a pulse timeline sequence."""

    data_ready = Signal(dict)
    error_occurred = Signal(str)
    finished_sweep = Signal()

    def __init__(
        self,
        instrument: AbstractSMU,
        params: Dict[str, Any],
        parent: "MainWindowUI | None" = None,
    ) -> None:
        super().__init__(parent)
        self.instrument = instrument
        self.params = params

    def run(self) -> None:
        p = self.params
        channel: str = p["channel"]
        smu_name: str = p["smu_name"]
        source_mode: str = p["source_mode"]
        source_limit: float = p["source_limit"]
        use_list_sweep_sampling = bool(p.get("pulse_list_sweep_sampling", False))
        timeline: list[PulseTimelinePoint] = list(p.get("timeline", []))
        source_levels: list[float] = [
            float(level) for level in p.get("source_levels", [])
        ]
        src_to_meas_delay_s = float(p.get("src_to_meas_delay_s", 0.0))
        measure_cfg: dict[str, Any] = p["measure_cfg"]
        bias_cfg: dict[str, Any] | None = p.get("bias_config")
        bias_measure_cfg: dict[str, Any] = p.get("bias_measure_cfg", {})
        bias_name = str(bias_cfg.get("smu_name", "")) if isinstance(bias_cfg, dict) else ""
        bias_channel = str(bias_cfg.get("channel", "")) if isinstance(bias_cfg, dict) else ""
        bias_source_mode = (
            str(bias_cfg.get("source_mode", "voltage")).strip().lower()
            if isinstance(bias_cfg, dict)
            else "voltage"
        )
        nplc: float = p["nplc"]
        started_at = time.monotonic()

        def abort_if_requested() -> None:
            if not self.isInterruptionRequested():
                return
            try:
                self.instrument.abort_sweep()
            except Exception:
                pass
            raise InterruptedError("Pulse run aborted by user.")

        def build_values(
            mode: str,
            source_level: float,
            current_value: float | None,
            voltage_value: float | None,
        ) -> dict[str, float]:
            normalized_mode = str(mode).strip().lower()
            values: dict[str, float] = {}
            if normalized_mode == "current":
                values["Current"] = float(
                    current_value if current_value is not None else source_level
                )
                if voltage_value is not None:
                    values["Voltage"] = float(voltage_value)
            else:
                values["Voltage"] = float(
                    voltage_value if voltage_value is not None else source_level
                )
                if current_value is not None:
                    values["Current"] = float(current_value)
            if "Voltage" in values and "Current" in values:
                current = values["Current"]
                values["Resistance"] = (
                    values["Voltage"] / current
                    if abs(current) > 1e-15
                    else float("inf")
                )
            return values

        def build_payload(
            sample_timestamp: float,
            source_level: float,
            values: dict[str, float],
            bias_source_level: float | None = None,
            bias_values: dict[str, float] | None = None,
        ) -> dict[str, object]:
            empty = {"source_v": 0.0, "values": {}}
            if str(source_mode).strip().lower() == "current":
                active = {"source_v": 0.0, "source_i": float(source_level), "values": values}
            else:
                active = {"source_v": float(source_level), "values": values}
            if bias_source_level is None:
                bias = empty
            elif bias_source_mode == "current":
                bias = {
                    "source_v": 0.0,
                    "source_i": float(bias_source_level),
                    "values": bias_values or {},
                }
            else:
                bias = {"source_v": float(bias_source_level), "values": bias_values or {}}
            return {
                "time_s": float(sample_timestamp),
                "sample_t_s": float(sample_timestamp),
                "series_name": f"{smu_name} Pulse",
                "primary_name": smu_name,
                "stepper_name": bias_name,
                "smu1": active if channel == "smua" else (bias if bias_channel == "smua" else empty),
                "smu2": active if channel == "smub" else (bias if bias_channel == "smub" else empty),
            }

        try:
            abort_if_requested()
            self.instrument.configure_measurement(
                channel,
                list(measure_cfg.get("items", [])),
                str(measure_cfg.get("range", "Auto")),
                str(measure_cfg.get("autozero", "Auto")),
                nplc,
            )
            if isinstance(bias_cfg, dict) and bias_channel:
                self.instrument.configure_measurement(
                    bias_channel,
                    list(bias_measure_cfg.get("items", [])),
                    str(bias_measure_cfg.get("range", "Auto")),
                    str(bias_measure_cfg.get("autozero", "Auto")),
                    nplc,
                )
            else:
                inactive_channel = "smub" if channel == "smua" else "smua"
                self.instrument.set_output(inactive_channel, False)

            if use_list_sweep_sampling:
                runner = getattr(self.instrument, "run_pulse_list_sweep", None)
                if not callable(runner):
                    raise RuntimeError(
                        "The active instrument driver does not support pulse list "
                        "sweep sampling."
                    )
                pulse_chunks = runner(
                    smu_channel=channel,
                    source_mode=source_mode,
                    source_levels=source_levels,
                    src_to_meas_delay_s=src_to_meas_delay_s,
                    nplc=nplc,
                    source_limit=source_limit,
                    measurement_items=list(measure_cfg.get("items", [])),
                    bias_config=bias_cfg,
                    stop_checker=abort_if_requested,
                )
            else:
                pulse_chunks = self.instrument.run_pulse_timeline(
                    channel,
                    source_mode,
                    timeline,
                    source_limit,
                    list(measure_cfg.get("items", [])),
                    bias_cfg,
                    abort_if_requested,
                )

            for (
                source_chunk,
                current_chunk,
                voltage_chunk,
                bias_source_chunk,
                bias_current_chunk,
                bias_voltage_chunk,
                timestamp_chunk,
            ) in pulse_chunks:
                abort_if_requested()
                for index, source_level in enumerate(source_chunk):
                    current_value = (
                        None
                        if current_chunk is None or index >= len(current_chunk)
                        else float(current_chunk[index])
                    )
                    voltage_value = (
                        None
                        if voltage_chunk is None or index >= len(voltage_chunk)
                        else float(voltage_chunk[index])
                    )
                    sample_timestamp = (
                        float(timestamp_chunk[index])
                        if index < len(timestamp_chunk)
                        else time.monotonic() - started_at
                    )
                    values = build_values(
                        source_mode,
                        float(source_level),
                        current_value,
                        voltage_value,
                    )
                    bias_source_level = (
                        None
                        if bias_source_chunk is None or index >= len(bias_source_chunk)
                        else float(bias_source_chunk[index])
                    )
                    bias_current_value = (
                        None
                        if bias_current_chunk is None or index >= len(bias_current_chunk)
                        else float(bias_current_chunk[index])
                    )
                    bias_voltage_value = (
                        None
                        if bias_voltage_chunk is None or index >= len(bias_voltage_chunk)
                        else float(bias_voltage_chunk[index])
                    )
                    bias_values = None
                    if bias_source_level is not None:
                        bias_values = build_values(
                            bias_source_mode,
                            bias_source_level,
                            bias_current_value,
                            bias_voltage_value,
                        )
                    self.data_ready.emit(
                        build_payload(
                            sample_timestamp,
                            float(source_level),
                            values,
                            bias_source_level,
                            bias_values,
                        )
                    )
        except InterruptedError:
            print("Pulse run aborted by user.")
        except Exception as exc:
            print(f"Pulse run failed: {exc}")
            try:
                error_queue = list(self.instrument.dump_errors())
            except Exception as error_exc:
                print(f"Failed to read instrument error queue: {error_exc}")
            else:
                if error_queue:
                    print("Instrument error queue:")
                    for index, error_entry in enumerate(error_queue, start=1):
                        print(f"  {index}. {error_entry}")
                else:
                    print("Instrument error queue is empty.")
            traceback.print_exc()
            self.error_occurred.emit(str(exc))
        finally:
            try:
                self.instrument.set_output("smua", False)
            except Exception:
                pass
            try:
                self.instrument.set_output("smub", False)
            except Exception:
                pass
            self.finished_sweep.emit()


class MainController:
    """
    Connects UI actions to the instrument: connect, run sweep, update preview
    and device summary from channel settings. Relies on UI's channel_config_changed
    signal and setattr-generated widget names (e.g. function_combo_smu1, limit_spin_smu1).
    """

    def __init__(self, ui: "MainWindowUI", instrument: AbstractSMU) -> None:
        self.ui = ui
        self.instrument = instrument
        self.workspace = WorkspaceDocument.new(ui.collect_settings())
        self._connected = False
        self._connecting = False
        self._autoscale_after_first_point = False
        self._syncing_step_controls = False
        self._current_run_id: int | None = None
        self._run_failed = False
        self._run_aborted = False
        self._suppress_project_default_update = False
        self._active_run_view_key: RunCacheKey | None = None
        self._rendered_graph_key: RunCacheKey | None = None
        self._rendered_table_key: RunCacheKey | None = None
        self._run_data_cache = RunDataCache()
        self.bind_signals()
        self.refresh_project_ui()
        self.update_preview_and_summary()
        self._auto_connect_timer = QTimer(self.ui)
        self._auto_connect_timer.setInterval(1000)
        self._auto_connect_timer.timeout.connect(self.try_auto_connect)
        self._auto_connect_timer.start()
        QTimer.singleShot(0, self.try_auto_connect)

    def bind_signals(self) -> None:
        """Bind UI signals: channel_config_changed and smu_selector change -> update summary and preview."""
        self.ui.new_project_btn.clicked.connect(self.handle_new_project)
        self.ui.open_project_btn.clicked.connect(self.handle_open_project)
        self.ui.save_project_btn.clicked.connect(self.handle_save_project)
        self.ui.save_project_as_btn.clicked.connect(self.handle_save_project_as)
        self.ui.new_project_record_btn.clicked.connect(self.handle_new_project_record)
        self.ui.save_quick_config_btn.clicked.connect(self.handle_save_quick_config)
        self.ui.project_selected.connect(self.handle_project_selected)
        self.ui.project_rename_requested.connect(self.handle_rename_project)
        self.ui.project_delete_requested.connect(self.handle_delete_project)
        self.ui.run_selected.connect(self.handle_run_selected)
        self.ui.run_rename_requested.connect(self.handle_rename_run)
        self.ui.run_delete_requested.connect(self.handle_delete_run)
        self.ui.quick_config_clicked.connect(self.handle_quick_config_clicked)
        self.ui.quick_config_rename_requested.connect(self.handle_rename_quick_config)
        self.ui.quick_config_delete_requested.connect(self.handle_delete_quick_config)
        self.ui.quick_config_quick_access_toggled.connect(
            self.handle_quick_config_quick_access_toggled
        )
        self.ui.scan_btn.clicked.connect(self.handle_scan)
        self.ui.connect_btn.clicked.connect(self.handle_connect)
        self.ui.import_config_btn.clicked.connect(self.handle_import_config)
        self.ui.export_config_btn.clicked.connect(self.handle_export_config)
        self.ui.reset_config_btn.clicked.connect(self.handle_reset_config)
        self.ui.run_btn.clicked.connect(self.handle_run)
        self.ui.abort_btn.clicked.connect(self.handle_abort)
        self.ui.clear_plot_btn.clicked.connect(self.handle_clear_plot)
        self.ui.autoscale_btn.clicked.connect(self.handle_autoscale)
        self.ui.graph_linear_btn.clicked.connect(
            lambda: self.handle_graph_scale_change("linear")
        )
        self.ui.graph_log_btn.clicked.connect(
            lambda: self.handle_graph_scale_change("log")
        )
        self.ui.x_axis_combo.currentTextChanged.connect(self.handle_graph_axis_change)
        self.ui.y_axis_combo.currentTextChanged.connect(self.handle_graph_axis_change)
        self.ui.graph_show_ramping_check.stateChanged.connect(
            self.handle_graph_ramping_visibility_change
        )
        self.ui.csv_show_ramping_check.stateChanged.connect(
            self.handle_table_ramping_visibility_change
        )
        self.ui.export_csv_btn.clicked.connect(self.handle_export_csv)
        self.ui.channel_config_changed.connect(self.update_preview_and_summary)
        self.ui.channel_config_changed.connect(self.handle_project_default_settings_changed)
        self.ui.channel_config_changed.connect(self.handle_ramping_filter_availability_change)
        self.ui.smu_selector.currentIndexChanged.connect(self.update_preview_and_summary)
        self.ui.tab_widget.currentChanged.connect(self.handle_run_view_tab_changed)
        self.ui.step_display_smu1.valueChanged.connect(
            lambda value: self.handle_step_value_changed(1, value)
        )
        self.ui.step_display_smu2.valueChanged.connect(
            lambda value: self.handle_step_value_changed(2, value)
        )

    def refresh_project_ui(self) -> None:
        """Refresh workspace title, project tree, and quick config buttons."""
        path_text = str(self.workspace.path) if self.workspace.path is not None else None
        active_project = self.workspace.active_project()
        self.ui.set_workspace_title(self.workspace.name, path_text, self.workspace.dirty)
        self.ui.set_project_tree(
            self.workspace.projects,
            self.workspace.active_project_id,
            active_project.active_run_id,
        )
        self.ui.set_quick_configs(self.workspace.quick_configs)

    def _project_file_filter(self) -> str:
        return (
            "Keithley Workspaces (*.keithley-workspace.sqlite);;"
            "Legacy JSON Workspaces (*.keithley-workspace.json *.json);;"
            "All Files (*)"
        )

    def _default_project_path(self) -> str:
        return "untitled.keithley-workspace.sqlite"

    def _autosave_project(self) -> None:
        if self.workspace.path is None:
            self.refresh_project_ui()
            return
        try:
            self.workspace.save()
        except Exception as exc:
            QMessageBox.warning(
                self.ui,
                "Workspace Autosave Failed",
                f"Could not save workspace automatically.\n{exc}",
            )
        self.refresh_project_ui()

    def _set_workspace(self, workspace: WorkspaceDocument) -> None:
        self._run_data_cache.clear()
        self.workspace = workspace
        active_project = self.workspace.active_project()
        self._current_run_id = active_project.active_run_id
        self.refresh_project_ui()
        active_run = active_project.active_run()
        if active_run is not None:
            self._load_run_into_ui(active_run)
        else:
            self._apply_settings_without_default_update(active_project.default_settings)
            self.ui.graph_plot_placeholder.clear_plot()
            self.ui.data_table.setRowCount(0)

    def handle_new_project(self) -> None:
        if not self._confirm_discard_unsaved_project():
            return
        self._set_workspace(WorkspaceDocument.new(self.ui.collect_settings()))

    def handle_open_project(self) -> None:
        if not self._confirm_discard_unsaved_project():
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self.ui,
            "Open Workspace",
            "",
            self._project_file_filter(),
        )
        if not file_path:
            return
        try:
            workspace = WorkspaceDocument.load(file_path)
        except Exception as exc:
            QMessageBox.critical(
                self.ui,
                "Open Workspace Failed",
                f"Could not open workspace.\n{exc}",
            )
            return
        self._set_workspace(workspace)

    def handle_save_project(self) -> None:
        if self.workspace.path is None:
            self.handle_save_project_as()
            return
        try:
            self.workspace.save()
        except Exception as exc:
            QMessageBox.critical(
                self.ui,
                "Save Workspace Failed",
                f"Could not save workspace.\n{exc}",
            )
            return
        self.refresh_project_ui()

    def handle_save_project_as(self) -> None:
        start_path = str(self.workspace.path) if self.workspace.path else self._default_project_path()
        file_path, _ = QFileDialog.getSaveFileName(
            self.ui,
            "Save Workspace As",
            start_path,
            self._project_file_filter(),
        )
        if not file_path:
            return
        if not file_path.lower().endswith((".sqlite", ".db", ".sqlite3", ".json")):
            file_path = f"{file_path}.keithley-workspace.sqlite"
        try:
            self.workspace.save(file_path)
        except Exception as exc:
            QMessageBox.critical(
                self.ui,
                "Save Workspace Failed",
                f"Could not save workspace.\n{exc}",
            )
            return
        self._run_data_cache.clear()
        self.refresh_project_ui()

    def _confirm_discard_unsaved_project(self) -> bool:
        if not self.workspace.dirty:
            return True
        answer = QMessageBox.question(
            self.ui,
            "Unsaved Workspace",
            "The current workspace has unsaved changes. Continue without saving?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def handle_new_project_record(self) -> None:
        name = f"Project {self.workspace.next_project_id}"
        project = self.workspace.add_project(name, self.ui.collect_settings())
        self.workspace.active_project_id = project.id
        self._current_run_id = None
        self.ui.graph_plot_placeholder.clear_plot()
        self.ui.data_table.setRowCount(0)
        self._autosave_project()

    def handle_save_quick_config(self) -> None:
        name = f"Config {self.workspace.next_config_id}"
        self.workspace.add_quick_config(name, self.ui.collect_settings())
        self._autosave_project()

    def handle_quick_config_clicked(self, config_id: int) -> None:
        config = self.workspace.get_quick_config(config_id)
        if config is None:
            return
        self.ui.apply_settings(config.settings)
        self._store_current_settings_as_project_default()

    def handle_rename_quick_config(self, config_id: int) -> None:
        config = self.workspace.get_quick_config(config_id)
        if config is None:
            return
        dialog = QInputDialog(self.ui)
        dialog.setInputMode(QInputDialog.TextInput)
        dialog.setWindowTitle("Rename Config")
        dialog.setLabelText(f"Name (max {MAX_QUICK_CONFIG_NAME_LENGTH} characters):")
        dialog.setTextValue(config.name)
        line_edit = dialog.findChild(QLineEdit)
        if line_edit is not None:
            line_edit.setMaxLength(MAX_QUICK_CONFIG_NAME_LENGTH)
        if dialog.exec() != QDialog.Accepted:
            return
        text = dialog.textValue()
        self.workspace.rename_quick_config(config_id, text)
        self._autosave_project()

    def handle_quick_config_quick_access_toggled(
        self,
        config_id: int,
        enabled: bool,
    ) -> None:
        if self.workspace.set_quick_config_quick_access(config_id, enabled):
            self._autosave_project()
            return
        self.refresh_project_ui()

    def handle_delete_quick_config(self, config_id: int) -> None:
        config = self.workspace.get_quick_config(config_id)
        if config is None:
            return
        answer = QMessageBox.question(
            self.ui,
            "Delete Config",
            f"Delete quick config '{config.name}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.workspace.delete_quick_config(config_id)
        self._autosave_project()

    def handle_project_selected(self, project_id: int) -> None:
        project = self.workspace.get_project(project_id)
        if project is None:
            return
        self.workspace.active_project_id = project.id
        self._current_run_id = project.active_run_id
        active_run = project.active_run()
        if active_run is not None:
            self._load_run_into_ui(active_run)
        else:
            self._apply_settings_without_default_update(project.default_settings)
            self.ui.graph_plot_placeholder.clear_plot()
            self.ui.data_table.setRowCount(0)
        self.workspace.mark_dirty()
        self._autosave_project()

    def handle_rename_project(self, project_id: int) -> None:
        project = self.workspace.get_project(project_id)
        if project is None:
            return
        text, ok = QInputDialog.getText(
            self.ui,
            "Rename Project",
            "Name (max 30 characters):",
            text=project.name,
        )
        if not ok:
            return
        self.workspace.rename_project(project_id, text)
        self._autosave_project()

    def handle_delete_project(self, project_id: int) -> None:
        project = self.workspace.get_project(project_id)
        if project is None:
            return
        if len(self.workspace.projects) <= 1:
            QMessageBox.information(
                self.ui,
                "Delete Project",
                "A workspace must contain at least one project.",
            )
            return
        answer = QMessageBox.question(
            self.ui,
            "Delete Project",
            f"Delete project '{project.name}' and all of its runs?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._run_data_cache.invalidate_project(self.workspace.path, project.id)
        self.workspace.delete_project(project_id)
        active_project = self.workspace.active_project()
        self._current_run_id = active_project.active_run_id
        active_run = active_project.active_run()
        if active_run is not None:
            self._load_run_into_ui(active_run)
        else:
            self._apply_settings_without_default_update(active_project.default_settings)
            self.ui.graph_plot_placeholder.clear_plot()
            self.ui.data_table.setRowCount(0)
        self._autosave_project()

    def handle_run_selected(self, project_id: int, run_id: int) -> None:
        project = self.workspace.get_project(project_id)
        if project is None:
            return
        run = project.get_run(run_id)
        if run is None:
            return
        self.workspace.active_project_id = project.id
        project.active_run_id = run.id
        self._current_run_id = run.id
        self.workspace.mark_dirty()
        self._load_run_into_ui(run)
        self._autosave_project()

    def handle_rename_run(self, project_id: int, run_id: int) -> None:
        project = self.workspace.get_project(project_id)
        if project is None:
            return
        run = project.get_run(run_id)
        if run is None:
            return
        text, ok = QInputDialog.getText(
            self.ui,
            "Rename Run",
            "Name (max 30 characters):",
            text=run.name,
        )
        if not ok:
            return
        project.rename_run(run_id, text)
        self.workspace.mark_dirty()
        self._autosave_project()

    def handle_delete_run(self, project_id: int, run_id: int) -> None:
        project = self.workspace.get_project(project_id)
        if project is None:
            return
        run = project.get_run(run_id)
        if run is None:
            return
        answer = QMessageBox.question(
            self.ui,
            "Delete Run",
            f"Delete RUN {run.id}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._run_data_cache.invalidate(self._run_cache_key(run, project.id))
        project.delete_run(run_id)
        self.workspace.active_project_id = project.id
        self._current_run_id = project.active_run_id
        active_run = project.active_run()
        if active_run is not None:
            self._load_run_into_ui(active_run)
        else:
            self._apply_settings_without_default_update(project.default_settings)
            self.ui.graph_plot_placeholder.clear_plot()
            self.ui.data_table.setRowCount(0)
        self.workspace.mark_dirty()
        self._autosave_project()

    def _load_run_into_ui(self, run: RunRecord) -> None:
        active_view = self._active_run_view()
        self.ui.show_loading("Loading run settings...", 100)
        QApplication.processEvents()
        try:
            self._apply_settings_without_default_update(run.settings)
            self.ui.graph_plot_placeholder.clear_plot()
            self.ui.data_table.setRowCount(0)
            graph_cfg = run.graph if isinstance(run.graph, dict) else {}
            x_axis = str(graph_cfg.get("x_axis", "SMU1 Voltage"))
            y_axis = str(graph_cfg.get("y_axis", "SMU1 Current"))
            display_mode = str(graph_cfg.get("display_mode", "linear"))
            show_ramping = bool(graph_cfg.get("show_ramping", True))

            self.ui.x_axis_combo.blockSignals(True)
            self.ui.y_axis_combo.blockSignals(True)
            if x_axis in [self.ui.x_axis_combo.itemText(i) for i in range(self.ui.x_axis_combo.count())]:
                self.ui.x_axis_combo.setCurrentText(x_axis)
            if y_axis in [self.ui.y_axis_combo.itemText(i) for i in range(self.ui.y_axis_combo.count())]:
                self.ui.y_axis_combo.setCurrentText(y_axis)
            self.ui.x_axis_combo.blockSignals(False)
            self.ui.y_axis_combo.blockSignals(False)
            self.ui.graph_plot_placeholder.set_axes(x_axis, y_axis)
            self.handle_graph_scale_change(display_mode)
            self.ui.graph_show_ramping_check.blockSignals(True)
            self.ui.graph_show_ramping_check.setChecked(show_ramping)
            self.ui.graph_show_ramping_check.blockSignals(False)
            self.ui.graph_plot_placeholder.set_show_ramping(show_ramping)

            self._active_run_view_key = self._run_cache_key(run)
            self._rendered_graph_key = None
            self._rendered_table_key = None
            if active_view == "graph":
                self._render_graph_for_run(run)
            elif active_view == "table":
                self._render_table_for_run(run)
            else:
                self._update_ramping_filter_availability(run.settings, [])
            self.ui.update_loading(100, "Run loaded")
            QApplication.processEvents()
        finally:
            self.ui.hide_loading()

    def _active_run_view(self) -> str:
        current_widget = self.ui.tab_widget.currentWidget()
        if current_widget is self.ui.graph_tab:
            return "graph"
        if current_widget is self.ui.table_tab:
            return "table"
        return "settings"

    def handle_run_view_tab_changed(self, index: int) -> None:
        run = self._current_project_run()
        if run is None:
            return
        active_view = self._active_run_view()
        if active_view == "graph":
            self.ui.show_loading("Rendering graph...", 100)
            QApplication.processEvents()
            try:
                self._render_graph_for_run(run)
            finally:
                self.ui.hide_loading()
        elif active_view == "table":
            self.ui.show_loading("Rendering table...", 100)
            QApplication.processEvents()
            try:
                self._render_table_for_run(run)
            finally:
                self.ui.hide_loading()

    def _load_payloads_for_run(self, run: RunRecord) -> list[dict]:
        def load_progress(value: int, total: int, message: str) -> None:
            percent = 15 if total <= 0 else 15 + int((value / max(1, total)) * 45)
            self.ui.update_loading(min(60, percent), message)
            QApplication.processEvents()

        cache_key = self._run_cache_key(run)
        cached_payloads = self._run_data_cache.get(cache_key)
        if cached_payloads is not None:
            self.ui.update_loading(60, "Loading data from cache...")
            QApplication.processEvents()
            run.data = cached_payloads
            return cached_payloads
        payloads = self.workspace.load_run_points(run, progress_callback=load_progress)
        self._run_data_cache.set(cache_key, payloads)
        return payloads

    def _render_graph_for_run(self, run: RunRecord) -> None:
        cache_key = self._run_cache_key(run)
        if self._rendered_graph_key == cache_key:
            return
        payloads = self._load_payloads_for_run(run)
        self._update_ramping_filter_availability(run.settings, payloads)
        self.ui.update_loading(88, "Rendering graph...")
        QApplication.processEvents()
        self.ui.graph_plot_placeholder.set_payloads(payloads)
        self._rendered_graph_key = cache_key

    def _render_table_for_run(self, run: RunRecord) -> None:
        cache_key = self._run_cache_key(run)
        if self._rendered_table_key == cache_key:
            return
        payloads = self._load_payloads_for_run(run)
        self._update_ramping_filter_availability(run.settings, payloads)
        self.ui.update_loading(62, "Rendering table...")
        QApplication.processEvents()
        self._render_table_payloads(
            self._table_payloads(payloads),
            progress_callback=lambda value, total: self._update_render_progress(
                value,
                total,
                62,
                84,
                "Rendering table...",
            ),
        )
        self._rendered_table_key = cache_key

    def _run_cache_key(
        self,
        run: RunRecord,
        project_id: int | None = None,
    ) -> RunCacheKey | None:
        if project_id is None:
            project = self.workspace.active_project()
            project_id = project.id
        return self._run_data_cache.make_key(
            self.workspace.path,
            int(project_id),
            int(run.id),
            run.storage_id,
        )

    def _update_render_progress(
        self,
        value: int,
        total: int,
        start: int,
        end: int,
        message: str,
    ) -> None:
        if total <= 0:
            self.ui.update_loading(end, message)
        else:
            self.ui.update_loading(start + int((value / max(1, total)) * (end - start)), message)
        QApplication.processEvents()

    def _render_table_payloads(
        self,
        payloads: list[dict],
        progress_callback: Any | None = None,
    ) -> None:
        self.ui.data_table.setRowCount(0)
        total = len(payloads)
        notify_every = max(1, total // 100) if total else 1
        for payload in payloads:
            row = self.ui.data_table.rowCount()
            self.ui.data_table.insertRow(row)
            self._fill_table_row(row, payload)
            if progress_callback is not None and (row + 1 == total or (row + 1) % notify_every == 0):
                progress_callback(row + 1, total)

    def _active_graph_settings(self) -> dict[str, Any]:
        return {
            "x_axis": str(self.ui.x_axis_combo.currentText() or "SMU1 Voltage"),
            "y_axis": str(self.ui.y_axis_combo.currentText() or "SMU1 Current"),
            "display_mode": "log" if self.ui.graph_log_btn.isChecked() else "linear",
            "show_ramping": bool(self.ui.graph_show_ramping_check.isChecked()),
        }

    def _begin_project_run(self, settings: dict[str, Any]) -> RunRecord:
        project = self.workspace.active_project()
        run = project.add_run(settings)
        run.status = "running"
        run.started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        run.finished_at = None
        run.data.clear()
        run.graph = self._active_graph_settings()
        self._current_run_id = run.id
        self._active_run_view_key = self._run_cache_key(run, project.id)
        self._rendered_graph_key = None
        self._rendered_table_key = None
        self._run_failed = False
        self._run_aborted = False
        self.workspace.mark_dirty()
        self.refresh_project_ui()
        self._run_data_cache.invalidate(self._run_cache_key(run, project.id))
        return run

    def _current_project_run(self) -> RunRecord | None:
        return self.workspace.active_project().get_run(self._current_run_id)

    def _store_current_settings_as_project_default(self) -> None:
        project = self.workspace.active_project()
        project.default_settings = self.ui.collect_settings()
        project.mark_dirty()
        self.workspace.mark_dirty()

    def handle_project_default_settings_changed(self, *args: object) -> None:
        if self._suppress_project_default_update:
            return
        self._store_current_settings_as_project_default()
        self.refresh_project_ui()

    def _apply_settings_without_default_update(self, settings: dict[str, Any]) -> None:
        self._suppress_project_default_update = True
        try:
            self.ui.apply_settings(settings, emit_changed=False)
            self.update_preview_and_summary()
        finally:
            self._suppress_project_default_update = False

    def handle_step_value_changed(self, smu_index: int, requested_step: float) -> None:
        if self._syncing_step_controls:
            return

        mode = str(
            getattr(self.ui, f"mode_combo_smu{smu_index}").currentText() or ""
        ).strip().lower()
        enabled = bool(getattr(self.ui, f"smu{smu_index}_enable_group").isChecked())
        if not enabled or mode != "sweep":
            return

        start = float(getattr(self.ui, f"start_spin_smu{smu_index}").value())
        stop = float(getattr(self.ui, f"stop_spin_smu{smu_index}").value())
        span = abs(stop - start)
        step = abs(float(requested_step))
        if span <= 0.0 or step <= 0.0:
            self.update_preview_and_summary()
            return

        points_spin = self._points_spin_for_smu(smu_index)
        if points_spin is None:
            self.update_preview_and_summary()
            return

        intervals = max(1, int(round(span / step)))
        points = intervals + 1
        points = max(points_spin.minimum(), min(points_spin.maximum(), points))

        self._syncing_step_controls = True
        try:
            points_spin.blockSignals(True)
            points_spin.setValue(points)
        finally:
            points_spin.blockSignals(False)
            self._syncing_step_controls = False

        self.update_preview_and_summary()

    def _points_spin_for_smu(self, smu_index: int):
        mode = str(
            getattr(self.ui, f"mode_combo_smu{smu_index}").currentText() or ""
        ).strip().lower()
        enabled = bool(getattr(self.ui, f"smu{smu_index}_enable_group").isChecked())
        if not enabled or mode != "sweep":
            return None

        enabled1 = bool(self.ui.smu1_enable_group.isChecked())
        enabled2 = bool(self.ui.smu2_enable_group.isChecked())
        stepper_text = str(self.ui.stepper_selector.currentText() or "").strip()
        if enabled1 and enabled2 and stepper_text == f"SMU {smu_index}":
            return self.ui.stepper_points_spin
        return self.ui.sweep_points_spin

    def _line_frequency_hz(self) -> float:
        return 50.0

    def _refresh_summary_tooltips(self) -> None:
        for smu_index in (1, 2):
            for field in ("function", "mode", "source", "limit", "measure", "ramp"):
                label = getattr(self.ui, f"smu{smu_index}_{field}_label", None)
                if label is not None:
                    label.setToolTip(label.text())

    def _set_step_control_value(
        self,
        smu_index: int,
        step_value: float,
        function_type: str,
        enabled: bool,
    ) -> None:
        step_spin = getattr(self.ui, f"step_display_smu{smu_index}")
        suffix = " V" if function_type == "Voltage" else " A"
        self._syncing_step_controls = True
        try:
            step_spin.blockSignals(True)
            step_spin.setSuffix(suffix)
            step_spin.setValue(abs(float(step_value)))
            step_spin.setEnabled(enabled)
        finally:
            step_spin.blockSignals(False)
            self._syncing_step_controls = False

    def handle_clear_plot(self) -> None:
        """Clear only the Graph tab plot (keep table data)."""
        self.ui.graph_plot_placeholder.clear_plot()

    def handle_autoscale(self) -> None:
        """Autoscale the Graph tab plot."""
        self.ui.graph_plot_placeholder.autoscale()

    def handle_graph_scale_change(self, mode: str) -> None:
        """Switch the measurement graph between linear and log current display."""
        normalized = "log" if str(mode).strip().lower() == "log" else "linear"
        self.ui.graph_linear_btn.setChecked(normalized == "linear")
        self.ui.graph_log_btn.setChecked(normalized == "log")
        self.ui.graph_plot_placeholder.set_display_mode(normalized)

    def handle_graph_axis_change(self, *args: object) -> None:
        """Redraw the measurement graph with the selected payload axes."""
        self._rendered_graph_key = None
        self.ui.graph_plot_placeholder.set_axes(
            str(self.ui.x_axis_combo.currentText() or "Time"),
            str(self.ui.y_axis_combo.currentText() or "SMU1 Current"),
        )
        run = self._current_project_run()
        if run is not None and not self._suppress_project_default_update:
            run.graph = self._active_graph_settings()
            self.workspace.active_project().mark_dirty()
            self.workspace.mark_dirty()

    def handle_graph_ramping_visibility_change(self, *args: object) -> None:
        """Show or hide ramp-up/down points in the graph without deleting them."""
        self._rendered_graph_key = None
        self.ui.graph_plot_placeholder.set_show_ramping(
            bool(self.ui.graph_show_ramping_check.isChecked())
        )
        run = self._current_project_run()
        if run is not None and not self._suppress_project_default_update:
            run.graph = self._active_graph_settings()
            self.workspace.active_project().mark_dirty()
            self.workspace.mark_dirty()

    def handle_table_ramping_visibility_change(self, *args: object) -> None:
        """Show or hide ramp-up/down points in the table and CSV export."""
        run = self._current_project_run()
        self._rendered_table_key = None
        payloads = list(run.data) if run is not None else []
        self._render_table_payloads(self._table_payloads(payloads))

    def handle_ramping_filter_availability_change(self, *args: object) -> None:
        """Refresh ramping controls when the current setup changes."""
        self._update_ramping_filter_availability(self.ui.collect_settings())

    def _update_ramping_filter_availability(
        self,
        settings: dict[str, Any] | None = None,
        payloads: list[dict] | None = None,
    ) -> None:
        has_ramp = self._settings_have_enabled_ramp(settings or {})
        if payloads is None:
            run = self._current_project_run()
            if run is not None:
                payloads = list(run.data)
        if payloads is not None:
            has_ramp = has_ramp or any(self._is_ramping_payload(payload) for payload in payloads)

        self.ui.graph_show_ramping_check.setVisible(has_ramp)
        self.ui.csv_show_ramping_check.setVisible(has_ramp)

    def _settings_have_enabled_ramp(self, settings: dict[str, Any]) -> bool:
        for smu_key in ("smu1", "smu2"):
            smu_cfg = settings.get(smu_key, {})
            if not isinstance(smu_cfg, dict):
                continue
            if str(smu_cfg.get("mode", "")).strip().lower() != "sweep":
                continue
            for ramp_key in ("ramp_up", "ramp_down"):
                ramp_cfg = smu_cfg.get(ramp_key, {})
                if isinstance(ramp_cfg, dict) and bool(ramp_cfg.get("enabled", False)):
                    return True
        return False

    def _is_ramping_payload(self, payload: dict) -> bool:
        phase = str(payload.get("sweep_phase", "")).strip().lower()
        return phase in {"ramp_up", "ramp_down"} or bool(payload.get("is_ramp", False))

    def _table_payloads(self, payloads: list[dict]) -> list[dict]:
        if bool(self.ui.csv_show_ramping_check.isChecked()):
            return payloads
        return [payload for payload in payloads if not self._is_ramping_payload(payload)]

    def _set_connected_status(self, model_name: str | None = None) -> None:
        status_text = "Connected"
        if model_name:
            status_text = f"Connected ({model_name})"
        self.ui.connection_status_label.setText(status_text)
        self.ui.connection_status_label.setStyleSheet(
            "color: #2d7d2d; font-weight: bold;"
        )
        self.ui.run_btn.setEnabled(True)
        self._connected = True

    def _set_disconnected_status(self, text: str = "Disconnected") -> None:
        self.ui.connection_status_label.setText(text)
        self.ui.connection_status_label.setStyleSheet(
            "color: gray; font-weight: bold;"
        )
        self.ui.run_btn.setEnabled(False)
        self._connected = False

    def _set_connecting_status(self, text: str = "Connecting...") -> None:
        self.ui.connection_status_label.setText(text)
        self.ui.connection_status_label.setStyleSheet(
            "color: #9a6b00; font-weight: bold;"
        )
        self.ui.run_btn.setEnabled(False)

    def _worker_is_running(self) -> bool:
        worker = getattr(self, "worker", None)
        is_running = getattr(worker, "isRunning", None)
        return bool(callable(is_running) and is_running())

    def _instrument_is_connected(self) -> bool:
        checker = getattr(self.instrument, "is_connected", None)
        if not callable(checker):
            return self._connected
        try:
            return bool(checker())
        except Exception:
            return False

    def _handle_connection_lost(self) -> None:
        try:
            self.instrument.disconnect()
        except Exception:
            pass
        self._set_disconnected_status("Disconnected")

    def _connect_resource(self, resource_str: str, *, quiet_failure: bool = False) -> bool:
        if self._connecting:
            return False

        self._connecting = True
        self.ui.connect_btn.setEnabled(False)
        if not quiet_failure:
            self._set_connecting_status()
            QApplication.processEvents()

        try:
            ok = self.instrument.connect(resource_str)
        finally:
            self._connecting = False
            self.ui.connect_btn.setEnabled(True)

        if ok:
            model_getter = getattr(self.instrument, "get_model", None)
            model_name = model_getter() if callable(model_getter) else None
            set_model = getattr(self.ui, "set_instrument_model", None)
            if callable(set_model):
                set_model(model_name)
            self._set_connected_status(model_name)
            return True

        if quiet_failure:
            self._set_disconnected_status("Scanning...")
        else:
            self.ui.connection_status_label.setText("Failed")
            self.ui.connection_status_label.setStyleSheet(
                "color: #b71c1c; font-weight: bold;"
            )
            self.ui.run_btn.setEnabled(False)
            self._connected = False
        return False

    def try_auto_connect(self) -> None:
        """Monitor the current session and silently reconnect when possible."""
        if self._connecting or self._worker_is_running():
            return

        if self._connected:
            if not self._instrument_is_connected():
                self._handle_connection_lost()
            return

        finder = getattr(self.instrument, "find_resource_address", None)
        if not callable(finder):
            return

        try:
            resource_str = finder(preferred_serial="4399155")
        except Exception:
            return

        if not resource_str:
            if self.ui.connection_status_label.text() == "Disconnected":
                self._set_disconnected_status("Scanning...")
            return

        self.ui.resource_address_edit.setText(resource_str)
        self._connect_resource(resource_str, quiet_failure=True)

    def handle_connect(self) -> None:
        """Read address from UI, connect instrument, update status label to Connected (green)."""
        resource_str = self.ui.resource_address_edit.text().strip()
        if not resource_str:
            return
        self._connect_resource(resource_str)

    def handle_scan(self) -> None:
        """Scan VISA resources and populate the most likely Keithley 2636 address."""
        finder = getattr(self.instrument, "find_resource_address", None)
        resource_str = None
        if callable(finder):
            resource_str = finder(preferred_serial="4399155")

        if resource_str:
            self.ui.resource_address_edit.setText(resource_str)
            QMessageBox.information(
                self.ui,
                "Scan Complete",
                f"Found instrument address:\n{resource_str}",
            )
            return

        QMessageBox.warning(
            self.ui,
            "Scan Complete",
            "No matching Keithley instrument was found.",
        )

    def handle_export_config(self) -> None:
        """Save the current UI configuration to a JSON file."""
        file_path, _ = QFileDialog.getSaveFileName(
            self.ui,
            "Export Configuration",
            "keithley_config.json",
            "JSON Files (*.json)",
        )
        if not file_path:
            return

        payload = {
            "format": "keithley-gui-config",
            "version": 1,
            "settings": self.ui.collect_settings(),
        }
        with open(file_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def handle_import_config(self) -> None:
        """Load a previously saved UI configuration from a JSON file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self.ui,
            "Load Configuration",
            "",
            "JSON Files (*.json)",
        )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            QMessageBox.critical(
                self.ui,
                "Load Failed",
                f"Could not read configuration file.\n{exc}",
            )
            return

        settings = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(settings, dict):
            QMessageBox.critical(
                self.ui,
                "Load Failed",
                "The selected file is not a valid configuration file.",
            )
            return

        self.ui.apply_settings(settings)

    def handle_reset_config(self) -> None:
        """Restore the UI to its default settings."""
        self.ui.reset_settings()

    def handle_export_csv(self) -> None:
        """Export the Table tab contents to CSV."""
        file_path, _ = QFileDialog.getSaveFileName(
            self.ui,
            "Export Table to CSV",
            "keithley_measurements.csv",
            "CSV Files (*.csv)",
        )
        if not file_path:
            return

        try:
            headers = [
                self.ui.data_table.horizontalHeaderItem(col).text()
                for col in range(self.ui.data_table.columnCount())
            ]
            with open(file_path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(headers)
                for row in range(self.ui.data_table.rowCount()):
                    values: list[str] = []
                    for col in range(self.ui.data_table.columnCount()):
                        item = self.ui.data_table.item(row, col)
                        values.append("" if item is None else item.text())
                    writer.writerow(values)
        except Exception as exc:
            QMessageBox.critical(
                self.ui,
                "Export Failed",
                f"Could not export CSV.\n{exc}",
            )
            return

        QMessageBox.information(
            self.ui,
            "Export Complete",
            f"CSV saved to:\n{file_path}",
        )

    def _log_pulse_list_sweep_plan(self, plan: Any) -> None:
        print(
            "Pulse list sweep metadata: "
            f"measurement_window_s={plan.measurement_window_s:.9g}, "
            f"src_to_meas_delay_s={plan.src_to_meas_delay_s:.9g}, "
            f"point_interval_s={plan.point_interval_s:.9g}, "
            f"total_points={plan.total_points}"
        )
        if plan.warnings:
            for warning in plan.warnings:
                print(f"Pulse list sweep warning: {warning}")
        else:
            print("Pulse list sweep warnings: none")

    def update_preview_and_summary(self, *args: object) -> None:
        """
        Read SMU 1 and SMU 2 form state (UI setattr names), update Limit suffix,
        Device Summary labels, and Output Preview.
        """
        # ----- Limit suffix from Function (SMU 1 and SMU 2) -----
        func1 = str(self.ui.function_combo_smu1.currentText() or "").strip()
        func2 = str(self.ui.function_combo_smu2.currentText() or "").strip()
        # Block suffix signals to avoid recursive refresh/valueChanged loops.
        self.ui.limit_spin_smu1.blockSignals(True)
        self.ui.limit_spin_smu1.setSuffix(" A" if func1 == "Voltage" else " V")
        self.ui.limit_spin_smu1.blockSignals(False)

        self.ui.limit_spin_smu2.blockSignals(True)
        self.ui.limit_spin_smu2.setSuffix(" A" if func2 == "Voltage" else " V")
        self.ui.limit_spin_smu2.blockSignals(False)

        # ----- Measure Window (based on NPLC, 50 Hz assumed) -----
        nplc = float(self.ui.nplc_spin.value())
        window_ms = nplc * 20.0  # 1 PLC at 50 Hz = 20 ms
        self.ui.measure_window_label.setText(f"{window_ms:.1f} ms")
        settings = self.ui.collect_settings()

        # ----- Read SMU 1 form -----
        mode1 = str(self.ui.mode_combo_smu1.currentText() or "").strip().lower()
        level1 = self.ui.level_spin_smu1.value()
        start1 = self.ui.start_spin_smu1.value()
        stop1 = self.ui.stop_spin_smu1.value()
        limit1 = self.ui.limit_spin_smu1.value()
        dual1 = self.ui.dual_sweep_check_smu1.isChecked()
        enabled1 = self.ui.smu1_enable_group.isChecked()

        # ----- Read SMU 2 form -----
        mode2 = str(self.ui.mode_combo_smu2.currentText() or "").strip().lower()
        level2 = self.ui.level_spin_smu2.value()
        start2 = self.ui.start_spin_smu2.value()
        stop2 = self.ui.stop_spin_smu2.value()
        limit2 = self.ui.limit_spin_smu2.value()
        dual2 = self.ui.dual_sweep_check_smu2.isChecked()
        enabled2 = self.ui.smu2_enable_group.isChecked()

        # ----- Global sweep / stepper points & repeat -----
        sweep_points = int(self.ui.sweep_points_spin.value())
        stepper_points = int(self.ui.stepper_points_spin.value())
        repeats = int(self.ui.repeat_spin.value())
        if sweep_points < 0:
            sweep_points = 0
        if stepper_points < 0:
            stepper_points = 0
        if repeats < 1:
            repeats = 1

        stepper_text = str(self.ui.stepper_selector.currentText() or "").strip()

        # Default assignments (base points per SMU, before dual-sweep expansion)
        pts1_base = 0
        pts2_base = 0

        if not enabled1 and not enabled2:
            pts1_base = 0
            pts2_base = 0
            pri_dual = False
            step_dual = False
            pri_mode = "disabled"
            step_mode = "disabled"
        elif enabled1 and not enabled2:
            pts1_base = sweep_points if mode1 == "sweep" else 0
            pts2_base = 0
            pri_dual = dual1
            step_dual = False
            pri_mode = mode1
            step_mode = "disabled"
        elif enabled2 and not enabled1:
            pts1_base = 0
            pts2_base = sweep_points if mode2 == "sweep" else 0
            pri_dual = dual2
            step_dual = False
            pri_mode = mode2
            step_mode = "disabled"
        elif stepper_text == "SMU 2":
            # SMU1 = Primary, SMU2 = Stepper
            pts1_base = sweep_points
            pts2_base = stepper_points
            pri_dual = dual1
            step_dual = dual2
            pri_mode = mode1
            step_mode = mode2
        elif stepper_text == "SMU 1":
            # SMU2 = Primary, SMU1 = Stepper
            pts2_base = sweep_points
            pts1_base = stepper_points
            pri_dual = dual2
            step_dual = dual1
            pri_mode = mode2
            step_mode = mode1
        else:
            # No stepper: prefer the only sweep-capable SMU as primary.
            if mode2 == "sweep" and mode1 != "sweep":
                pts1_base = 0
                pts2_base = sweep_points
                pri_dual = dual2
                pri_mode = mode2
            elif mode1 == "sweep":
                pts1_base = sweep_points
                pts2_base = 0
                pri_dual = dual1
                pri_mode = mode1
            else:
                pts1_base = 0
                pts2_base = 0
                pri_dual = False
                pri_mode = mode1 if enabled1 else mode2
            step_dual = False
            step_mode = "fixed"

        def compute_step(start: float, stop: float, points: int) -> float:
            if points is None or points <= 1:
                return 0.0
            return (stop - start) / float(points - 1)

        def format_step(step_val: float, function_type: str) -> str:
            function_type = function_type or "Voltage"
            abs_val = abs(step_val)
            if function_type == "Voltage":
                if 0 < abs_val < 1.0:
                    return f"{step_val * 1000.0:.2f} mV"
                return f"{step_val:.4f} V"
            else:
                # Current
                if abs_val < 1e-3:
                    return f"{step_val * 1e6:.2f} uA"
                if abs_val < 1.0:
                    return f"{step_val * 1e3:.2f} mA"
                return f"{step_val:.4f} A"

        def format_measure_summary(items: list[str]) -> str:
            symbol_map = {
                "Voltage": "V",
                "Current": "A",
                "Resistance": "Ohm",
            }
            if not items:
                return "None"
            return ", ".join(symbol_map.get(item, item) for item in items)

        def display_points_for_smu(
            smu_index: int,
            enabled: bool,
            mode: str,
        ) -> int:
            if not enabled or mode != "sweep":
                return 0
            if enabled1 and enabled2 and stepper_text == f"SMU {smu_index}":
                return stepper_points
            return sweep_points

        # Step labels should describe each SMU's own sweep settings, even
        # before the controller assigns primary/stepper execution roles.
        step1_points = display_points_for_smu(1, enabled1, mode1)
        step2_points = display_points_for_smu(2, enabled2, mode2)
        step1_val = compute_step(start1, stop1, step1_points) if mode1 == "sweep" else 0.0
        step2_val = compute_step(start2, stop2, step2_points) if mode2 == "sweep" else 0.0

        self._set_step_control_value(1, step1_val, func1, enabled1 and mode1 == "sweep")
        self._set_step_control_value(2, step2_val, func2, enabled2 and mode2 == "sweep")

        # ----- Show/hide Dual Sweep checkbox (only when mode is Sweep) -----
        self.ui.dual_sweep_check_smu1.setVisible(mode1 == "sweep")
        self.ui.dual_sweep_check_smu2.setVisible(mode2 == "sweep")

        # ----- Device Summary: SMU 1 -----
        mode1_display = mode1.capitalize() if mode1 else "-"
        self.ui.smu1_function_label.setText(func1 or "-")
        self.ui.smu1_mode_label.setText(mode1_display)
        self.ui.smu1_measure_label.setText(
            format_measure_summary(self.ui.measure_combo_smu1.selected_items())
        )
        if func1 == "Voltage":
            if mode1 == "fixed":
                self.ui.smu1_source_label.setText(f"Bias Level: {level1:.4g} V")
            elif mode1 == "pulse":
                pulse_count = len(settings["smu1"].get("pulse", {}).get("combinations", []))
                self.ui.smu1_source_label.setText(f"Pulse: {pulse_count} combinations")
            else:
                step1_str = format_step(step1_val, func1)
                self.ui.smu1_source_label.setText(
                    f"{start1:.4g} - {stop1:.4g} V Step: {step1_str}"
                )
            self.ui.smu1_limit_label.setText(f"{limit1:.4g} A")
        else:
            if mode1 == "fixed":
                self.ui.smu1_source_label.setText(f"Bias Level: {level1:.4g} A")
            elif mode1 == "pulse":
                pulse_count = len(settings["smu1"].get("pulse", {}).get("combinations", []))
                self.ui.smu1_source_label.setText(f"Pulse: {pulse_count} combinations")
            else:
                step1_str = format_step(step1_val, func1 or "Current")
                self.ui.smu1_source_label.setText(
                    f"{start1:.4g} - {stop1:.4g} A Step: {step1_str}"
                )
            self.ui.smu1_limit_label.setText(f"{limit1:.4g} V")

        # Ramp summary (only meaningful in Voltage mode)
        if func1 == "Voltage" and mode1 == "sweep":
            ramp_parts: list[str] = []
            if self.ui.ramp_up_check_smu1.isChecked():
                ramp_parts.append("Up")
            if self.ui.ramp_down_check_smu1.isChecked():
                ramp_parts.append("Down")
            if ramp_parts:
                ramp_text = " / ".join(ramp_parts) + " enabled"
            else:
                ramp_text = "Disabled"
            self.ui.smu1_ramp_label.setText(ramp_text)
            self.ui.smu1_ramp_label.setVisible(True)
        else:
            self.ui.smu1_ramp_label.setText("-")
            self.ui.smu1_ramp_label.setVisible(False)

        # ----- Device Summary: SMU 2 -----
        mode2_display = mode2.capitalize() if mode2 else "-"
        self.ui.smu2_function_label.setText(func2 or "-")
        self.ui.smu2_mode_label.setText(mode2_display)
        self.ui.smu2_measure_label.setText(
            format_measure_summary(self.ui.measure_combo_smu2.selected_items())
        )
        if func2 == "Voltage":
            if mode2 == "fixed":
                self.ui.smu2_source_label.setText(f"Bias Level: {level2:.4g} V")
            elif mode2 == "pulse":
                pulse_count = len(settings["smu2"].get("pulse", {}).get("combinations", []))
                self.ui.smu2_source_label.setText(f"Pulse: {pulse_count} combinations")
            else:
                step2_str = format_step(step2_val, func2)
                self.ui.smu2_source_label.setText(
                    f"{start2:.4g} - {stop2:.4g} V Step: {step2_str}"
                )
            self.ui.smu2_limit_label.setText(f"{limit2:.4g} A")
        else:
            if mode2 == "fixed":
                self.ui.smu2_source_label.setText(f"Bias Level: {level2:.4g} A")
            elif mode2 == "pulse":
                pulse_count = len(settings["smu2"].get("pulse", {}).get("combinations", []))
                self.ui.smu2_source_label.setText(f"Pulse: {pulse_count} combinations")
            else:
                step2_str = format_step(step2_val, func2 or "Current")
                self.ui.smu2_source_label.setText(
                    f"{start2:.4g} - {stop2:.4g} A Step: {step2_str}"
                )
            self.ui.smu2_limit_label.setText(f"{limit2:.4g} V")

        # Ramp summary (only meaningful in Voltage mode)
        if func2 == "Voltage" and mode2 == "sweep":
            ramp_parts2: list[str] = []
            if self.ui.ramp_up_check_smu2.isChecked():
                ramp_parts2.append("Up")
            if self.ui.ramp_down_check_smu2.isChecked():
                ramp_parts2.append("Down")
            if ramp_parts2:
                ramp_text2 = " / ".join(ramp_parts2) + " enabled"
            else:
                ramp_text2 = "Disabled"
            self.ui.smu2_ramp_label.setText(ramp_text2)
            self.ui.smu2_ramp_label.setVisible(True)
        else:
            self.ui.smu2_ramp_label.setText("-")
            self.ui.smu2_ramp_label.setVisible(False)

        def mark_summary_disabled(smu_index: int) -> None:
            getattr(self.ui, f"smu{smu_index}_function_label").setText("Disabled")
            getattr(self.ui, f"smu{smu_index}_mode_label").setText("Disabled")
            getattr(self.ui, f"smu{smu_index}_source_label").setText("Output off")
            getattr(self.ui, f"smu{smu_index}_limit_label").setText("-")
            getattr(self.ui, f"smu{smu_index}_measure_label").setText("-")
            getattr(self.ui, f"smu{smu_index}_ramp_label").setText("-")
            getattr(self.ui, f"smu{smu_index}_ramp_label").setVisible(True)

        if not enabled1:
            mark_summary_disabled(1)
        if not enabled2:
            mark_summary_disabled(2)
        self._refresh_summary_tooltips()

        # ----- Calculated total points (global) -----
        if enabled1 and not enabled2:
            pri_actual_pts = int(pts1_base)
            step_actual_pts = 0
        elif enabled2 and not enabled1:
            pri_actual_pts = int(pts2_base)
            step_actual_pts = 0
        elif not enabled1 and not enabled2:
            pri_actual_pts = 0
            step_actual_pts = 0
        else:
            pri_actual_pts = int(pts1_base if stepper_text != "SMU 1" else pts2_base)
            step_actual_pts = int(pts2_base if stepper_text == "SMU 2" else pts1_base)
        if pri_mode == "sweep" and pri_actual_pts > 0 and pri_dual:
            pri_actual_pts = pri_actual_pts * 2 - 1

        if step_mode == "sweep" and step_actual_pts > 0 and step_dual:
            step_actual_pts = step_actual_pts * 2 - 1

        pulse_total_points: int | None = None
        pulse_point_interval_s: float | None = None
        pulse_configs = []
        if enabled1 and mode1 == "pulse":
            pulse_configs.append(settings["smu1"].get("pulse", {}))
        if enabled2 and mode2 == "pulse":
            pulse_configs.append(settings["smu2"].get("pulse", {}))
        if len(pulse_configs) == 1:
            try:
                pulse_events = flatten_pulse_config(pulse_configs[0], repeat=repeats)
                common_settings = settings.get("common", {})
                if bool(common_settings.get("pulse_list_sweep_sampling", False)):
                    pulse_plan = build_pulse_list_sweep_plan(
                        pulse_events,
                        nplc=float(common_settings.get("nplc", self.ui.nplc_spin.value())),
                        line_frequency_hz=self._line_frequency_hz(),
                        src_to_meas_delay_s=float(
                            common_settings.get(
                                "src_meas_delay",
                                self.ui.src_meas_delay_spin.value(),
                            )
                        ),
                    )
                    pulse_total_points = len(pulse_plan.source_levels)
                    pulse_point_interval_s = pulse_plan.point_interval_s
                else:
                    pulse_timeline = build_pulse_timeline(
                        pulse_events,
                        float(
                            common_settings.get(
                                "pulse_sample_interval",
                                0.001,
                            )
                        ),
                    )
                    pulse_total_points = len(pulse_timeline)
            except ValueError as exc:
                print(f"Invalid pulse sequence: {exc}")
                pulse_total_points = 0
            except Exception:
                pulse_total_points = 0

        if pulse_total_points is not None:
            total_points = pulse_total_points
        elif stepper_text == "None" or step_mode == "fixed":
            total_points = max(0, pri_actual_pts) * repeats
        else:
            total_points = max(0, pri_actual_pts) * max(0, step_actual_pts) * repeats

        if pulse_point_interval_s is not None:
            calculated_points_text = (
                f"{total_points:,} points, interval \u2248 {pulse_point_interval_s:.6g} s"
            )
        else:
            calculated_points_text = f"{total_points:,}"
        self.ui.calculated_points_label.setText(calculated_points_text)
        self.ui.calculated_points_label.setToolTip(calculated_points_text)

        # ----- Pack config and update Output Preview -----
        src_meas_delay = float(self.ui.src_meas_delay_spin.value())
        step_sweep_delay = float(self.ui.step_sweep_delay_spin.value())

        if not enabled1:
            smu1_cfg = {"mode": "disabled"}
        elif mode1 == "fixed":
            smu1_cfg = {
                "mode": "fixed",
                "level": level1,
                "src_meas_delay": src_meas_delay,
                "step_sweep_delay": step_sweep_delay,
                "repeat": repeats,
            }
        elif mode1 == "pulse":
            smu1_cfg = {
                "mode": "pulse",
                "pulse": settings["smu1"].get("pulse", {}),
                "repeat": repeats,
            }
        else:
            smu1_cfg = {
                "mode": "sweep",
                "start": start1,
                "stop": stop1,
                "points": max(0, int(pts1_base)),
                "dual": dual1,
                "src_meas_delay": src_meas_delay,
                "step_sweep_delay": step_sweep_delay,
                "repeat": repeats,
            }
        if not enabled2:
            smu2_cfg = {"mode": "disabled"}
        elif mode2 == "fixed":
            smu2_cfg = {
                "mode": "fixed",
                "level": level2,
                "src_meas_delay": src_meas_delay,
                "step_sweep_delay": step_sweep_delay,
                "repeat": repeats,
            }
        elif mode2 == "pulse":
            smu2_cfg = {
                "mode": "pulse",
                "pulse": settings["smu2"].get("pulse", {}),
                "repeat": repeats,
            }
        else:
            smu2_cfg = {
                "mode": "sweep",
                "start": start2,
                "stop": stop2,
                "points": max(0, int(pts2_base)),
                "dual": dual2,
                "src_meas_delay": src_meas_delay,
                "step_sweep_delay": step_sweep_delay,
                "repeat": repeats,
            }

        # Sweep points estimate (print to terminal)
        for label, mode, base_pts, dual in [
            ("SMU 1", mode1, pts1_base, dual1),
            ("SMU 2", mode2, pts2_base, dual2),
        ]:
            if mode == "sweep" and base_pts > 0:
                eff_pts = int(base_pts) * 2 - 1 if dual else int(base_pts)
                print(
                    f"[Preview] {label} Sweep Points: {eff_pts}"
                    + (" (dual)" if dual else "")
                )

        self.ui.preview_plot_placeholder.update_preview(
            smu1_cfg, smu2_cfg, duration=1.0
        )

    def handle_run(self) -> None:
        """
        Run sweep based on stepper_selector using a background worker thread.

        Primary does the main IV sweep, Stepper provides bias (Fixed) or
        staircase (Sweep). Worker handles all instrument I/O; this method only
        reads UI state, prepares parameters and wires signals.
        """
        self.ui.run_btn.setEnabled(False)
        self.ui.abort_btn.setEnabled(True)

        # ----- Global sweep / stepper points -----
        sweep_points = int(self.ui.sweep_points_spin.value())
        stepper_points = int(self.ui.stepper_points_spin.value())
        if sweep_points < 0:
            sweep_points = 0
        if stepper_points < 0:
            stepper_points = 0

        # ----- Role assignment from UI -----
        settings = self.ui.collect_settings()
        smu1_enabled = bool(settings["smu1"].get("enabled", True))
        smu2_enabled = bool(settings["smu2"].get("enabled", True))
        enabled_channels = [
            channel
            for channel, enabled in (("smua", smu1_enabled), ("smub", smu2_enabled))
            if enabled
        ]
        pulse_channels = [
            (name, channel, cfg)
            for name, channel, enabled, cfg in (
                ("SMU 1", "smua", smu1_enabled, settings["smu1"]),
                ("SMU 2", "smub", smu2_enabled, settings["smu2"]),
            )
            if enabled and str(cfg.get("mode", "")).strip().lower() == "pulse"
        ]
        if pulse_channels:
            if len(pulse_channels) != 1:
                QMessageBox.information(
                    self.ui,
                    "Pulse Mode",
                    (
                        "Only one Pulse SMU is supported now. "
                        "SMU1 Pulse + SMU2 Pulse is not supported yet."
                    ),
                )
                self.ui.run_btn.setEnabled(True)
                self.ui.abort_btn.setEnabled(False)
                return

            smu_name, channel, pulse_cfg = pulse_channels[0]
            other_name = "SMU 2" if smu_name == "SMU 1" else "SMU 1"
            other_channel = "smub" if channel == "smua" else "smua"
            other_settings = settings["smu2"] if channel == "smua" else settings["smu1"]
            other_enabled = smu2_enabled if channel == "smua" else smu1_enabled
            other_mode = str(other_settings.get("mode", "")).strip().lower()
            if other_enabled and other_mode != "fixed":
                QMessageBox.information(
                    self.ui,
                    "Pulse Mode",
                    "Pulse + Sweep is not supported yet. Use Fixed or disable the other SMU.",
                )
                self.ui.run_btn.setEnabled(True)
                self.ui.abort_btn.setEnabled(False)
                return

            common_settings = settings.get("common", {})
            repeat = int(common_settings.get("repeat", 1))
            use_list_sweep_sampling = bool(
                common_settings.get("pulse_list_sweep_sampling", False)
            )
            timeline: list[PulseTimelinePoint] | None = None
            source_levels: list[float] | None = None
            src_to_meas_delay_s = float(
                common_settings.get(
                    "src_meas_delay",
                    self.ui.src_meas_delay_spin.value(),
                )
            )
            try:
                events = flatten_pulse_config(pulse_cfg.get("pulse", {}), repeat=repeat)
                if use_list_sweep_sampling:
                    plan = build_pulse_list_sweep_plan(
                        events,
                        nplc=float(common_settings.get("nplc", self.ui.nplc_spin.value())),
                        line_frequency_hz=self._line_frequency_hz(),
                        src_to_meas_delay_s=src_to_meas_delay_s,
                    )
                    source_levels = list(plan.source_levels)
                    self._log_pulse_list_sweep_plan(plan)
                    with open(
                        "pulse_generated_list.csv",
                        "w",
                        newline="",
                        encoding="utf-8",
                    ) as fh:
                        fh.write(pulse_list_sweep_levels_to_csv(source_levels))
                else:
                    sample_interval = float(
                        common_settings.get(
                            "pulse_sample_interval",
                            self.ui.pulse_sample_interval_spin.value(),
                        )
                    )
                    timeline = build_pulse_timeline(events, sample_interval)
                    # TEMP: export the final generated pulse list for inspection.
                    # Comment/remove this block after debugging.
                    with open("pulse_generated_list.csv", "w", newline="", encoding="utf-8") as fh:
                        writer = csv.writer(fh)
                        writer.writerow(["index", "time_s", "source_level", "dwell_to_next_s"])
                        for index, point in enumerate(timeline, start=1):
                            writer.writerow(
                                [
                                    index,
                                    point.time_s,
                                    point.source_level,
                                    point.dwell_to_next_s,
                                ]
                            )
            except ValueError as exc:
                QMessageBox.warning(self.ui, "Invalid Pulse Sequence", str(exc))
                self.ui.run_btn.setEnabled(True)
                self.ui.abort_btn.setEnabled(False)
                return
            except Exception as exc:
                QMessageBox.warning(self.ui, "Invalid Pulse Sequence", str(exc))
                self.ui.run_btn.setEnabled(True)
                self.ui.abort_btn.setEnabled(False)
                return

            nplc = float(self.ui.nplc_spin.value())
            if not use_list_sweep_sampling:
                sample_interval = float(
                    common_settings.get(
                        "pulse_sample_interval",
                        self.ui.pulse_sample_interval_spin.value(),
                    )
                )
                measure_window_s = nplc * 0.02
            else:
                sample_interval = None
                measure_window_s = 0.0
            if (
                not use_list_sweep_sampling
                and sample_interval is not None
                and sample_interval < measure_window_s
            ):
                QMessageBox.information(
                    self.ui,
                    "Pulse Timing Notice",
                    (
                        f"NPLC={nplc:.3g} gives an approximate {measure_window_s:.6g} s "
                        "measurement window at 50 Hz, which is longer than the pulse "
                        f"sample interval ({sample_interval:.6g} s). The real instrument "
                        "may run slower than the requested timeline."
                    ),
                )

            bias_config = None
            bias_measure_cfg: dict[str, Any] = {}
            if other_enabled:
                bias_config = {
                    "smu_name": other_name,
                    "channel": other_channel,
                    "source_mode": str(other_settings.get("function", "Voltage")).strip().lower(),
                    "level": float(other_settings.get("level", 0.0)),
                    "limit": float(other_settings.get("limit", 0.0)),
                }
                bias_measure_cfg = other_settings.get("measure", {})

            current_run = self._begin_project_run(settings)
            self.ui.graph_plot_placeholder.clear_plot()
            self.ui.data_table.setRowCount(0)
            self._update_ramping_filter_availability(settings)
            self.ui.tab_widget.setCurrentWidget(self.ui.graph_tab)
            self.ui.graph_plot_placeholder.set_axes(
                str(self.ui.x_axis_combo.currentText() or "Time"),
                str(self.ui.y_axis_combo.currentText() or "SMU1 Current"),
            )
            current_run.graph = self._active_graph_settings()
            self.workspace.active_project().mark_dirty()
            self.workspace.mark_dirty()
            self._autoscale_after_first_point = True
            QTimer.singleShot(0, self.ui.graph_plot_placeholder.autoscale)

            params: Dict[str, Any] = {
                "smu_name": smu_name,
                "channel": channel,
                "source_mode": str(pulse_cfg.get("function", "Voltage")).strip().lower(),
                "source_limit": float(pulse_cfg.get("limit", 0.0)),
                "pulse_list_sweep_sampling": use_list_sweep_sampling,
                "timeline": timeline or [],
                "source_levels": source_levels or [],
                "src_to_meas_delay_s": src_to_meas_delay_s,
                "measure_cfg": settings["smu1"]["measure"]
                if channel == "smua"
                else settings["smu2"]["measure"],
                "bias_config": bias_config,
                "bias_measure_cfg": bias_measure_cfg,
                "nplc": float(self.ui.nplc_spin.value()),
            }

            self.worker = PulseWorker(self.instrument, params, parent=self.ui)
            self.worker.data_ready.connect(self.handle_new_data_point)
            self.worker.error_occurred.connect(self.handle_sweep_error)
            self.worker.finished_sweep.connect(self.handle_sweep_finished)
            self.worker.start()
            return
        if not enabled_channels:
            QMessageBox.warning(
                self.ui,
                "No SMU Enabled",
                "Enable at least one SMU in Device Summary before running.",
            )
            self.ui.run_btn.setEnabled(True)
            self.ui.abort_btn.setEnabled(False)
            return

        single_smu = len(enabled_channels) == 1
        stepper_text = str(self.ui.stepper_selector.currentText() or "").strip()
        if single_smu:
            if smu1_enabled:
                primary_name = "SMU 1"
                stepper_name = "SMU 2"
                primary_channel = "smua"
                stepper_channel = "smub"
                pri_start = self.ui.start_spin_smu1.value()
                pri_stop = self.ui.stop_spin_smu1.value()
                primary_limit = self.ui.limit_spin_smu1.value()
                pri_mode = str(self.ui.mode_combo_smu1.currentText() or "").strip().lower()
                stepper_limit = self.ui.limit_spin_smu2.value()
                primary_dual = self.ui.dual_sweep_check_smu1.isChecked()
                ramp_up = self.ui.ramp_up_check_smu1.isChecked()
                ru_step = float(self.ui.ramp_up_step_smu1.value())
                ru_delay = float(self.ui.ramp_up_delay_smu1.value())
                ramp_down = self.ui.ramp_down_check_smu1.isChecked()
                rd_step = float(self.ui.ramp_down_step_smu1.value())
                rd_delay = float(self.ui.ramp_down_delay_smu1.value())
            else:
                primary_name = "SMU 2"
                stepper_name = "SMU 1"
                primary_channel = "smub"
                stepper_channel = "smua"
                pri_start = self.ui.start_spin_smu2.value()
                pri_stop = self.ui.stop_spin_smu2.value()
                primary_limit = self.ui.limit_spin_smu2.value()
                pri_mode = str(self.ui.mode_combo_smu2.currentText() or "").strip().lower()
                stepper_limit = self.ui.limit_spin_smu1.value()
                primary_dual = self.ui.dual_sweep_check_smu2.isChecked()
                ramp_up = self.ui.ramp_up_check_smu2.isChecked()
                ru_step = float(self.ui.ramp_up_step_smu2.value())
                ru_delay = float(self.ui.ramp_up_delay_smu2.value())
                ramp_down = self.ui.ramp_down_check_smu2.isChecked()
                rd_step = float(self.ui.ramp_down_step_smu2.value())
                rd_delay = float(self.ui.ramp_down_delay_smu2.value())
            stepper_level = 0.0
            stepper_start = 0.0
            stepper_stop = 0.0
            stepper_mode = "disabled"
            primary_points = sweep_points
            stepper_points_assigned = 0
            stepper_dual = False
        elif stepper_text == "SMU 2":
            primary_name = "SMU 1"
            stepper_name = "SMU 2"
            primary_channel = "smua"
            stepper_channel = "smub"
            pri_start = self.ui.start_spin_smu1.value()
            pri_stop = self.ui.stop_spin_smu1.value()
            primary_limit = self.ui.limit_spin_smu1.value()
            pri_mode = str(self.ui.mode_combo_smu1.currentText() or "").strip().lower()
            stepper_level = self.ui.level_spin_smu2.value()
            stepper_limit = self.ui.limit_spin_smu2.value()
            stepper_start = self.ui.start_spin_smu2.value()
            stepper_stop = self.ui.stop_spin_smu2.value()
            stepper_mode = str(self.ui.mode_combo_smu2.currentText() or "").strip().lower()
            primary_points = sweep_points
            stepper_points_assigned = stepper_points
            primary_dual = self.ui.dual_sweep_check_smu1.isChecked()
            stepper_dual = self.ui.dual_sweep_check_smu2.isChecked()
            ramp_up = self.ui.ramp_up_check_smu1.isChecked()
            ru_step = float(self.ui.ramp_up_step_smu1.value())
            ru_delay = float(self.ui.ramp_up_delay_smu1.value())
            ramp_down = self.ui.ramp_down_check_smu1.isChecked()
            rd_step = float(self.ui.ramp_down_step_smu1.value())
            rd_delay = float(self.ui.ramp_down_delay_smu1.value())
        elif stepper_text == "SMU 1":
            primary_name = "SMU 2"
            stepper_name = "SMU 1"
            primary_channel = "smub"
            stepper_channel = "smua"
            pri_start = self.ui.start_spin_smu2.value()
            pri_stop = self.ui.stop_spin_smu2.value()
            primary_limit = self.ui.limit_spin_smu2.value()
            pri_mode = str(self.ui.mode_combo_smu2.currentText() or "").strip().lower()
            stepper_level = self.ui.level_spin_smu1.value()
            stepper_limit = self.ui.limit_spin_smu1.value()
            stepper_start = self.ui.start_spin_smu1.value()
            stepper_stop = self.ui.stop_spin_smu1.value()
            stepper_mode = str(self.ui.mode_combo_smu1.currentText() or "").strip().lower()
            primary_points = sweep_points
            stepper_points_assigned = stepper_points
            primary_dual = self.ui.dual_sweep_check_smu2.isChecked()
            stepper_dual = self.ui.dual_sweep_check_smu1.isChecked()
            ramp_up = self.ui.ramp_up_check_smu2.isChecked()
            ru_step = float(self.ui.ramp_up_step_smu2.value())
            ru_delay = float(self.ui.ramp_up_delay_smu2.value())
            ramp_down = self.ui.ramp_down_check_smu2.isChecked()
            rd_step = float(self.ui.ramp_down_step_smu2.value())
            rd_delay = float(self.ui.ramp_down_delay_smu2.value())
        else:
            mode1 = str(self.ui.mode_combo_smu1.currentText() or "").strip().lower()
            mode2 = str(self.ui.mode_combo_smu2.currentText() or "").strip().lower()

            if mode2 == "sweep" and mode1 != "sweep":
                primary_name = "SMU 2"
                stepper_name = "SMU 1"
                primary_channel = "smub"
                stepper_channel = "smua"
                pri_start = self.ui.start_spin_smu2.value()
                pri_stop = self.ui.stop_spin_smu2.value()
                primary_limit = self.ui.limit_spin_smu2.value()
                pri_mode = mode2
                stepper_level = self.ui.level_spin_smu1.value()
                stepper_limit = self.ui.limit_spin_smu1.value()
                stepper_start = self.ui.start_spin_smu1.value()
                stepper_stop = self.ui.stop_spin_smu1.value()
                stepper_mode = mode1
                primary_dual = self.ui.dual_sweep_check_smu2.isChecked()
                ramp_up = self.ui.ramp_up_check_smu2.isChecked()
                ru_step = float(self.ui.ramp_up_step_smu2.value())
                ru_delay = float(self.ui.ramp_up_delay_smu2.value())
                ramp_down = self.ui.ramp_down_check_smu2.isChecked()
                rd_step = float(self.ui.ramp_down_step_smu2.value())
                rd_delay = float(self.ui.ramp_down_delay_smu2.value())
            else:
                primary_name = "SMU 1"
                stepper_name = "SMU 2"
                primary_channel = "smua"
                stepper_channel = "smub"
                pri_start = self.ui.start_spin_smu1.value()
                pri_stop = self.ui.stop_spin_smu1.value()
                primary_limit = self.ui.limit_spin_smu1.value()
                pri_mode = mode1
                stepper_level = self.ui.level_spin_smu2.value()
                stepper_limit = self.ui.limit_spin_smu2.value()
                stepper_start = self.ui.start_spin_smu2.value()
                stepper_stop = self.ui.stop_spin_smu2.value()
                stepper_mode = mode2
                primary_dual = self.ui.dual_sweep_check_smu1.isChecked()
                ramp_up = self.ui.ramp_up_check_smu1.isChecked()
                ru_step = float(self.ui.ramp_up_step_smu1.value())
                ru_delay = float(self.ui.ramp_up_delay_smu1.value())
                ramp_down = self.ui.ramp_down_check_smu1.isChecked()
                rd_step = float(self.ui.ramp_down_step_smu1.value())
                rd_delay = float(self.ui.ramp_down_delay_smu1.value())

            primary_points = sweep_points
            stepper_points_assigned = 0
            stepper_dual = False

        if pri_mode != "sweep":
            self.ui.run_btn.setEnabled(True)
            self.ui.abort_btn.setEnabled(False)
            return

        points = max(2, int(primary_points)) if pri_mode == "sweep" else 2

        current_run = self._begin_project_run(settings)
        # ----- Clear UI before starting -----
        self.ui.graph_plot_placeholder.clear_plot()
        self.ui.data_table.setRowCount(0)
        self._update_ramping_filter_availability(settings)
        self.ui.tab_widget.setCurrentWidget(self.ui.graph_tab)
        self.ui.graph_plot_placeholder.set_axes(
            str(self.ui.x_axis_combo.currentText() or "SMU1 Voltage"),
            str(self.ui.y_axis_combo.currentText() or "SMU1 Current"),
        )
        current_run.graph = self._active_graph_settings()
        self.workspace.active_project().mark_dirty()
        self.workspace.mark_dirty()
        self._autoscale_after_first_point = True
        QTimer.singleShot(0, self.ui.graph_plot_placeholder.autoscale)

        # Additional timing / measurement parameters
        src_meas_delay = float(self.ui.src_meas_delay_spin.value())
        step_sweep_delay = float(self.ui.step_sweep_delay_spin.value())
        nplc = float(self.ui.nplc_spin.value())
        measure_cfg = {
            "smua": settings["smu1"]["measure"],
            "smub": settings["smu2"]["measure"],
        }

        params: Dict[str, Any] = {
            "primary_name": primary_name,
            "stepper_name": stepper_name,
            "primary_channel": primary_channel,
            "stepper_channel": stepper_channel,
            "pri_start": pri_start,
            "pri_stop": pri_stop,
            "pri_mode": pri_mode,
            "primary_limit": primary_limit,
            "stepper_level": stepper_level,
            "stepper_limit": stepper_limit,
            "stepper_start": stepper_start,
            "stepper_stop": stepper_stop,
            "stepper_mode": stepper_mode,
            "primary_points": primary_points,
            "points": points,
            "stepper_points_assigned": stepper_points_assigned,
            "src_meas_delay": src_meas_delay,
            "step_sweep_delay": step_sweep_delay,
            "nplc": nplc,
            "primary_dual": primary_dual,
            "stepper_dual": stepper_dual,
            "ramp_up": ramp_up,
            "ru_step": ru_step,
            "ru_delay": ru_delay,
            "ramp_down": ramp_down,
            "rd_step": rd_step,
            "rd_delay": rd_delay,
            "measure_cfg": measure_cfg,
            "enabled_channels": enabled_channels,
            "single_smu": single_smu,
        }

        # Create and start worker thread
        self.worker = SweepWorker(self.instrument, params, parent=self.ui)
        self.worker.data_ready.connect(self.handle_new_data_point)
        self.worker.error_occurred.connect(self.handle_sweep_error)
        self.worker.finished_sweep.connect(self.handle_sweep_finished)

        self.worker.start()

    def handle_abort(self) -> None:
        """Request an in-flight sweep to stop."""
        worker = getattr(self, "worker", None)
        if worker is None or not worker.isRunning():
            self.ui.abort_btn.setEnabled(False)
            self.ui.run_btn.setEnabled(True)
            return
        self.ui.abort_btn.setEnabled(False)
        self._run_aborted = True
        run = self._current_project_run()
        if run is not None:
            run.status = "aborted"
            self.workspace.active_project().mark_dirty()
            self.workspace.mark_dirty()
            self.refresh_project_ui()
        worker.requestInterruption()

    def handle_sweep_error(self, message: str) -> None:
        """Show sweep failures from the worker thread in the UI."""
        self._run_failed = True
        run = self._current_project_run()
        if run is not None:
            run.status = "failed"
            run.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self.workspace.active_project().mark_dirty()
            self.workspace.mark_dirty()
            self.refresh_project_ui()
        QMessageBox.critical(
            self.ui,
            "Sweep Failed",
            message or "The sweep failed. See terminal output for details.",
        )

    def handle_new_data_point(self, payload: dict) -> None:
        """
        Slot for SweepWorker.data_ready: update graph and table in the UI thread.
        """
        run = self._current_project_run()
        if run is not None:
            run.data.append(clone_json(payload))
            run.graph = self._active_graph_settings()
            try:
                self.workspace.append_run_point(
                    self.workspace.active_project().id,
                    run.id,
                    clone_json(payload),
                )
            except Exception as exc:
                print(f"Failed to persist run point: {exc}")
            self._run_data_cache.append(
                self._run_cache_key(run, self.workspace.active_project().id),
                payload,
            )
            self.workspace.active_project().mark_dirty()
            self.workspace.mark_dirty()
        self.ui.graph_plot_placeholder.append_payload(payload)

        if self._table_payloads([payload]):
            row = self.ui.data_table.rowCount()
            self.ui.data_table.insertRow(row)
            self._fill_table_row(row, payload)
        if self._autoscale_after_first_point:
            self._autoscale_after_first_point = False
            QTimer.singleShot(0, self.ui.graph_plot_placeholder.autoscale)

    def handle_sweep_finished(self) -> None:
        """Restore Run/Abort button state after worker finishes."""
        self.ui.run_btn.setEnabled(True)
        self.ui.abort_btn.setEnabled(False)
        run = self._current_project_run()
        if run is not None:
            if self._run_failed:
                run.status = "failed"
            elif self._run_aborted:
                run.status = "aborted"
            elif run.status == "running":
                run.status = "completed"
            run.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            run.graph = self._active_graph_settings()
            self.workspace.active_project().mark_dirty()
            self.workspace.mark_dirty()
        self._autosave_project()

    def _fill_table_row(self, row: int, payload: dict) -> None:
        """Fill one table row with measured V/I/R values for both SMUs."""
        smu1_values = payload.get("smu1", {}).get("values", {})
        smu2_values = payload.get("smu2", {}).get("values", {})

        def format_value(values: dict, key: str) -> str:
            if key not in values:
                return ""
            return f"{float(values[key]):.6g}"

        self.ui.data_table.setItem(
            row, 0, QTableWidgetItem(f"{float(payload.get('time_s', 0.0)):.6f}")
        )
        self.ui.data_table.setItem(row, 1, QTableWidgetItem(format_value(smu1_values, "Voltage")))
        self.ui.data_table.setItem(row, 2, QTableWidgetItem(format_value(smu1_values, "Current")))
        self.ui.data_table.setItem(row, 3, QTableWidgetItem(format_value(smu1_values, "Resistance")))
        self.ui.data_table.setItem(row, 4, QTableWidgetItem(format_value(smu2_values, "Voltage")))
        self.ui.data_table.setItem(row, 5, QTableWidgetItem(format_value(smu2_values, "Current")))
        self.ui.data_table.setItem(row, 6, QTableWidgetItem(format_value(smu2_values, "Resistance")))
