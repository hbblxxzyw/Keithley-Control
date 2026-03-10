"""
Main controller: connects MainWindowUI and AbstractSMU, binds signals and slots.
"""

import time
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtWidgets import QTableWidgetItem

from core.instrument_base import AbstractSMU

if TYPE_CHECKING:
    from ui.main_window_ui import MainWindowUI


class MainController:
    """
    Connects UI actions to the instrument: connect, run sweep, update preview
    and device summary from channel settings. Relies on UI's channel_config_changed
    signal and setattr-generated widget names (e.g. function_combo_smu1, limit_spin_smu1).
    """

    def __init__(self, ui: "MainWindowUI", instrument: AbstractSMU) -> None:
        self.ui = ui
        self.instrument = instrument
        self.bind_signals()
        self.update_preview_and_summary()

    def bind_signals(self) -> None:
        """Bind UI signals: channel_config_changed and smu_selector change -> update summary and preview."""
        self.ui.connect_btn.clicked.connect(self.handle_connect)
        self.ui.run_btn.clicked.connect(self.handle_run)
        self.ui.clear_plot_btn.clicked.connect(self.handle_clear_plot)
        self.ui.channel_config_changed.connect(self.update_preview_and_summary)
        self.ui.smu_selector.currentIndexChanged.connect(self.update_preview_and_summary)

    def handle_clear_plot(self) -> None:
        """Clear only the Graph tab plot (keep table data)."""
        self.ui.graph_plot_placeholder.clear_plot()

    def handle_connect(self) -> None:
        """Read address from UI, connect instrument, update status label to Connected (green)."""
        resource_str = self.ui.resource_address_edit.text().strip()
        if not resource_str:
            return
        ok = self.instrument.connect(resource_str)
        if ok:
            self.ui.connection_status_label.setText("Connected")
            self.ui.connection_status_label.setStyleSheet(
                "color: #2d7d2d; font-weight: bold;"
            )
        else:
            self.ui.connection_status_label.setText("Failed")
            self.ui.connection_status_label.setStyleSheet(
                "color: #b71c1c; font-weight: bold;"
            )

    def update_preview_and_summary(self, *args: object) -> None:
        """
        Read SMU 1 and SMU 2 form state (UI setattr names), update Limit suffix,
        Device Summary labels, and Output Preview.
        """
        # ----- Limit suffix from Function (SMU 1 and SMU 2) -----
        func1 = str(self.ui.function_combo_smu1.currentText() or "").strip()
        func2 = str(self.ui.function_combo_smu2.currentText() or "").strip()
        # 拦截信号，避免 setSuffix 触发的重绘/valueChanged 导致无限循环或静默崩溃
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

        # ----- Read SMU 2 form -----
        mode2 = str(self.ui.mode_combo_smu2.currentText() or "").strip().lower()
        level2 = self.ui.level_spin_smu2.value()
        start2 = self.ui.start_spin_smu2.value()
        stop2 = self.ui.stop_spin_smu2.value()
        limit2 = self.ui.limit_spin_smu2.value()
        dual2 = self.ui.dual_sweep_check_smu2.isChecked()

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

        if stepper_text == "SMU 2":
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
            # No stepper: SMU1 as primary, SMU2 disabled
            pts1_base = sweep_points
            pts2_base = 0
            pri_dual = dual1
            step_dual = False
            pri_mode = mode1
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

        # Compute per-SMU step based on assigned base points (single-direction)
        step1_val = compute_step(start1, stop1, pts1_base) if mode1 == "sweep" else 0.0
        step2_val = compute_step(start2, stop2, pts2_base) if mode2 == "sweep" else 0.0

        # Update read-only Step displays
        self.ui.step_display_smu1.setText(format_step(step1_val, func1))
        self.ui.step_display_smu2.setText(format_step(step2_val, func2))

        # ----- Show/hide Dual Sweep checkbox (only when mode is Sweep) -----
        self.ui.dual_sweep_check_smu1.setVisible(mode1 == "sweep")
        self.ui.dual_sweep_check_smu2.setVisible(mode2 == "sweep")

        # ----- Device Summary: SMU 1 -----
        mode1_display = mode1.capitalize() if mode1 else "—"
        self.ui.smu1_mode_label.setText(f"{func1 or '—'} / {mode1_display}")
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
        # Points (sweep only, based on assigned base points)
        if mode1 == "sweep" and pts1_base > 0:
            pts1 = int(pts1_base)
            pts1_effective = pts1 * 2 - 1 if dual1 else pts1
            self.ui.smu1_points_label.setText(
                str(pts1_effective) + (" (dual)" if dual1 else "")
            )
            self.ui.smu1_points_label.setVisible(True)
        else:
            self.ui.smu1_points_label.setVisible(mode1 == "sweep")
            self.ui.smu1_points_label.setText("—")

        # ----- Device Summary: SMU 2 -----
        mode2_display = mode2.capitalize() if mode2 else "—"
        self.ui.smu2_mode_label.setText(f"{func2 or '—'} / {mode2_display}")
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
        # Points (sweep only, based on assigned base points)
        if mode2 == "sweep" and pts2_base > 0:
            pts2 = int(pts2_base)
            pts2_effective = pts2 * 2 - 1 if dual2 else pts2
            self.ui.smu2_points_label.setText(
                str(pts2_effective) + (" (dual)" if dual2 else "")
            )
            self.ui.smu2_points_label.setVisible(True)
        else:
            self.ui.smu2_points_label.setVisible(mode2 == "sweep")
            self.ui.smu2_points_label.setText("—")

        # ----- Calculated total points (global) -----
        pri_actual_pts = int(pts1_base if stepper_text != "SMU 1" else pts2_base)
        if pri_mode == "sweep" and pri_actual_pts > 0 and pri_dual:
            pri_actual_pts = pri_actual_pts * 2 - 1

        step_actual_pts = int(pts2_base if stepper_text == "SMU 2" else pts1_base)
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

        if mode1 == "fixed":
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
        if mode2 == "fixed":
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

        duration = self.ui.time_resolution_spin.value()
        if duration <= 0:
            duration = 1.0
        self.ui.preview_plot_placeholder.update_preview(
            smu1_cfg, smu2_cfg, duration=duration
        )

    def handle_run(self) -> None:
        """
        Run sweep based on stepper_selector: Primary does the main IV sweep,
        Stepper provides bias (Fixed) or staircase (Sweep). Supports single-sweep
        with bias and nested family-of-curves.
        """
        # ----- Global sweep / stepper points -----
        sweep_points = int(self.ui.sweep_points_spin.value())
        stepper_points = int(self.ui.stepper_points_spin.value())
        if sweep_points < 0:
            sweep_points = 0
        if stepper_points < 0:
            stepper_points = 0

        # ----- Role assignment from UI -----
        stepper_text = str(self.ui.stepper_selector.currentText() or "").strip()
        if stepper_text == "SMU 2":
            primary_name = "SMU 1"
            stepper_name = "SMU 2"
            primary_channel = "smua"
            stepper_channel = "smub"
            pri_start = self.ui.start_spin_smu1.value()
            pri_stop = self.ui.stop_spin_smu1.value()
            pri_mode = str(self.ui.mode_combo_smu1.currentText() or "").strip().lower()
            stepper_level = self.ui.level_spin_smu2.value()
            stepper_limit = self.ui.limit_spin_smu2.value()
            stepper_start = self.ui.start_spin_smu2.value()
            stepper_stop = self.ui.stop_spin_smu2.value()
            stepper_mode = str(self.ui.mode_combo_smu2.currentText() or "").strip().lower()
            primary_points = sweep_points
            stepper_points_assigned = stepper_points
        elif stepper_text == "SMU 1":
            primary_name = "SMU 2"
            stepper_name = "SMU 1"
            primary_channel = "smub"
            stepper_channel = "smua"
            pri_start = self.ui.start_spin_smu2.value()
            pri_stop = self.ui.stop_spin_smu2.value()
            pri_mode = str(self.ui.mode_combo_smu2.currentText() or "").strip().lower()
            stepper_level = self.ui.level_spin_smu1.value()
            stepper_limit = self.ui.limit_spin_smu1.value()
            stepper_start = self.ui.start_spin_smu1.value()
            stepper_stop = self.ui.stop_spin_smu1.value()
            stepper_mode = str(self.ui.mode_combo_smu1.currentText() or "").strip().lower()
            primary_points = sweep_points
            stepper_points_assigned = stepper_points
        else:
            # Fallback: no stepper, SMU1 primary
            primary_name = "SMU 1"
            stepper_name = "SMU 2"
            primary_channel = "smua"
            stepper_channel = "smub"
            pri_start = self.ui.start_spin_smu1.value()
            pri_stop = self.ui.stop_spin_smu1.value()
            pri_mode = str(self.ui.mode_combo_smu1.currentText() or "").strip().lower()
            stepper_level = self.ui.level_spin_smu2.value()
            stepper_limit = self.ui.limit_spin_smu2.value()
            stepper_start = self.ui.start_spin_smu2.value()
            stepper_stop = self.ui.stop_spin_smu2.value()
            stepper_mode = str(self.ui.mode_combo_smu2.currentText() or "").strip().lower()
            primary_points = sweep_points
            stepper_points_assigned = 0

        points = max(2, int(primary_points)) if pri_mode == "sweep" else 2

        # ----- Clear UI -----
        self.ui.graph_plot_placeholder.clear_plot()
        self.ui.data_table.setRowCount(0)

        delay_s = max(0.0, self.ui.src_meas_delay_spin.value())

        try:
            if stepper_mode == "fixed":
                # ----- Branch A: Stepper Fixed → single sweep with bias -----
                self.instrument.set_voltage_source(
                    stepper_channel, stepper_level, stepper_limit
                )
                self.instrument.set_output(stepper_channel, True)

                voltages, currents = self.instrument.run_iv_sweep(
                    primary_channel, pri_start, pri_stop, points
                )
                series_name = f"{primary_name} (Bias={stepper_level:.2f}V)"
                for i, (v, i_val) in enumerate(zip(voltages, currents)):
                    self.ui.graph_plot_placeholder.append_data_point(
                        v, i_val, series_name
                    )
                    row = self.ui.data_table.rowCount()
                    self.ui.data_table.insertRow(row)
                    self._fill_table_row(
                        row, primary_name, v, i_val, stepper_name, stepper_level
                    )
            else:
                # ----- Branch B: Stepper Sweep → nested family of curves -----
                # Generate stepper values from global stepper_points setting
                n_stepper = max(1, int(stepper_points_assigned))
                if n_stepper == 1:
                    stepper_vals = np.array([float(stepper_start)])
                else:
                    stepper_vals = np.linspace(
                        float(stepper_start),
                        float(stepper_stop),
                        n_stepper,
                    )
                for step_val in stepper_vals:
                    self.instrument.set_voltage_source(
                        stepper_channel, float(step_val), stepper_limit
                    )
                    self.instrument.set_output(stepper_channel, True)
                    if delay_s > 0:
                        time.sleep(delay_s)

                    voltages, currents = self.instrument.run_iv_sweep(
                        primary_channel, pri_start, pri_stop, points
                    )
                    series_name = f"{primary_name} (Step={step_val:.2f}V)"
                    for i, (v, i_val) in enumerate(zip(voltages, currents)):
                        self.ui.graph_plot_placeholder.append_data_point(
                            v, i_val, series_name
                        )
                        row = self.ui.data_table.rowCount()
                        self.ui.data_table.insertRow(row)
                        self._fill_table_row(
                            row, primary_name, v, i_val, stepper_name, float(step_val)
                        )
            self.instrument.set_output("smua", False)
            self.instrument.set_output("smub", False)
        except Exception:
            self.instrument.set_output("smua", False)
            self.instrument.set_output("smub", False)
            raise

    def _fill_table_row(
        self,
        row: int,
        primary_name: str,
        primary_v: float,
        primary_i: float,
        stepper_name: str,
        stepper_v: float,
    ) -> None:
        """Fill one table row: col0=index, col1/2=SMU1 V/I, col3/4=SMU2 V/I."""
        self.ui.data_table.setItem(row, 0, QTableWidgetItem(f"{row}"))
        if primary_name == "SMU 1":
            self.ui.data_table.setItem(row, 1, QTableWidgetItem(f"{primary_v:.6g}"))
            self.ui.data_table.setItem(row, 2, QTableWidgetItem(f"{primary_i:.6g}"))
            self.ui.data_table.setItem(row, 3, QTableWidgetItem(f"{stepper_v:.6g}"))
            self.ui.data_table.setItem(row, 4, QTableWidgetItem(""))
        else:
            self.ui.data_table.setItem(row, 1, QTableWidgetItem(f"{stepper_v:.6g}"))
            self.ui.data_table.setItem(row, 2, QTableWidgetItem(""))
            self.ui.data_table.setItem(row, 3, QTableWidgetItem(f"{primary_v:.6g}"))
            self.ui.data_table.setItem(row, 4, QTableWidgetItem(f"{primary_i:.6g}"))
