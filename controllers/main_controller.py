"""
Main controller: connects MainWindowUI and AbstractSMU, binds signals and slots.
"""

import csv
import json
import time
import traceback
from typing import TYPE_CHECKING, Any, Dict

import numpy as np
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox, QTableWidgetItem

from core.instrument_base import AbstractSMU

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
                )
                self.data_ready.emit(payload)
                emitted_points += 1
            return emitted_points

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
                    emitted_points += emit_single_chunk_payloads(
                        started_at,
                        series_name,
                        list(source_chunk),
                        list(current_chunk),
                        None if voltage_chunk is None else list(voltage_chunk),
                        list(timestamp_chunk),
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
                emitted_points += emit_chunk_payloads(
                    started_at,
                    series_name,
                    list(primary_source_chunk),
                    list(primary_current_chunk),
                    None if primary_voltage_chunk is None else list(primary_voltage_chunk),
                    list(secondary_source_chunk),
                    list(secondary_current_chunk),
                    None if secondary_voltage_chunk is None else list(secondary_voltage_chunk),
                    list(timestamp_chunk),
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


class MainController:
    """
    Connects UI actions to the instrument: connect, run sweep, update preview
    and device summary from channel settings. Relies on UI's channel_config_changed
    signal and setattr-generated widget names (e.g. function_combo_smu1, limit_spin_smu1).
    """

    def __init__(self, ui: "MainWindowUI", instrument: AbstractSMU) -> None:
        self.ui = ui
        self.instrument = instrument
        self._connected = False
        self._autoscale_after_first_point = False
        self.bind_signals()
        self.update_preview_and_summary()
        self._auto_connect_timer = QTimer(self.ui)
        self._auto_connect_timer.setInterval(1000)
        self._auto_connect_timer.timeout.connect(self.try_auto_connect)
        self._auto_connect_timer.start()
        QTimer.singleShot(0, self.try_auto_connect)

    def bind_signals(self) -> None:
        """Bind UI signals: channel_config_changed and smu_selector change -> update summary and preview."""
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
        self.ui.export_csv_btn.clicked.connect(self.handle_export_csv)
        self.ui.channel_config_changed.connect(self.update_preview_and_summary)
        self.ui.smu_selector.currentIndexChanged.connect(self.update_preview_and_summary)

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

    def _connect_resource(self, resource_str: str, *, quiet_failure: bool = False) -> bool:
        ok = self.instrument.connect(resource_str)
        if ok:
            model_getter = getattr(self.instrument, "get_model", None)
            model_name = model_getter() if callable(model_getter) else None
            set_model = getattr(self.ui, "set_instrument_model", None)
            if callable(set_model):
                set_model(model_name)
            self._set_connected_status(model_name)
            timer = getattr(self, "_auto_connect_timer", None)
            if timer is not None:
                timer.stop()
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
        """Periodically scan for a Keithley and connect silently when found."""
        if self._connected:
            timer = getattr(self, "_auto_connect_timer", None)
            if timer is not None:
                timer.stop()
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
            "Import Configuration",
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
                "Import Failed",
                f"Could not read configuration file.\n{exc}",
            )
            return

        settings = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(settings, dict):
            QMessageBox.critical(
                self.ui,
                "Import Failed",
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
            else:
                pts1_base = sweep_points
                pts2_base = 0
                pri_dual = dual1
                pri_mode = mode1
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

        # Update read-only Step displays
        self.ui.step_display_smu1.setText(format_step(step1_val, func1))
        self.ui.step_display_smu2.setText(format_step(step2_val, func2))

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
            else:
                step1_str = format_step(step1_val, func1)
                self.ui.smu1_source_label.setText(
                    f"{start1:.4g} - {stop1:.4g} V Step: {step1_str}"
                )
            self.ui.smu1_limit_label.setText(f"{limit1:.4g} A")
        else:
            if mode1 == "fixed":
                self.ui.smu1_source_label.setText(f"Bias Level: {level1:.4g} A")
            else:
                step1_str = format_step(step1_val, func1 or "Current")
                self.ui.smu1_source_label.setText(
                    f"{start1:.4g} - {stop1:.4g} A Step: {step1_str}"
                )
            self.ui.smu1_limit_label.setText(f"{limit1:.4g} V")

        # Ramp summary (only meaningful in Voltage mode)
        if func1 == "Voltage":
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
            else:
                step2_str = format_step(step2_val, func2)
                self.ui.smu2_source_label.setText(
                    f"{start2:.4g} - {stop2:.4g} V Step: {step2_str}"
                )
            self.ui.smu2_limit_label.setText(f"{limit2:.4g} A")
        else:
            if mode2 == "fixed":
                self.ui.smu2_source_label.setText(f"Bias Level: {level2:.4g} A")
            else:
                step2_str = format_step(step2_val, func2 or "Current")
                self.ui.smu2_source_label.setText(
                    f"{start2:.4g} - {stop2:.4g} A Step: {step2_str}"
                )
            self.ui.smu2_limit_label.setText(f"{limit2:.4g} V")

        # Ramp summary (only meaningful in Voltage mode)
        if func2 == "Voltage":
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

        if stepper_text == "None" or step_mode == "fixed":
            total_points = max(0, pri_actual_pts) * repeats
        else:
            total_points = max(0, pri_actual_pts) * max(0, step_actual_pts) * repeats

        self.ui.calculated_points_label.setText(f"{total_points:,}")

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

        # ----- Clear UI before starting -----
        self.ui.graph_plot_placeholder.clear_plot()
        self.ui.data_table.setRowCount(0)
        self.ui.tab_widget.setCurrentWidget(self.ui.graph_tab)
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
        worker.requestInterruption()

    def handle_sweep_error(self, message: str) -> None:
        """Show sweep failures from the worker thread in the UI."""
        QMessageBox.critical(
            self.ui,
            "Sweep Failed",
            message or "The sweep failed. See terminal output for details.",
        )

    def handle_new_data_point(self, payload: dict) -> None:
        """
        Slot for SweepWorker.data_ready: update graph and table in the UI thread.
        """
        series_name = str(payload.get("series_name", ""))
        primary_name = str(payload.get("primary_name", ""))
        smu1_values = payload.get("smu1", {}).get("values", {})
        smu2_values = payload.get("smu2", {}).get("values", {})

        if primary_name == "SMU 1":
            sweep_x = smu1_values.get("Voltage")
        else:
            sweep_x = smu2_values.get("Voltage")

        if sweep_x is not None and "Current" in smu1_values:
            self.ui.graph_plot_placeholder.append_data_point(
                "SMU 1",
                float(sweep_x),
                float(smu1_values["Current"]),
                series_name,
            )
        if sweep_x is not None and "Current" in smu2_values:
            self.ui.graph_plot_placeholder.append_data_point(
                "SMU 2",
                float(sweep_x),
                float(smu2_values["Current"]),
                series_name,
            )

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
