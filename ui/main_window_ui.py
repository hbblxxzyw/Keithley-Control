"""
Main window UI with top-level 3-tab architecture: Settings, Graph, Table.

Industrial-style layout: configuration and preview in Settings; full-screen
Graph and Table tabs for measurement data.
"""

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QTabWidget,
    QStackedWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QDoubleSpinBox,
    QComboBox,
    QCheckBox,
    QPushButton,
    QTableWidget,
    QHeaderView,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QToolButton,
    QMenu,
    QWidgetAction,
    QSizePolicy,
    QFrame,
    QProgressBar,
)

from ui.graph_widget import PreviewGraphWidget, MeasurementGraphWidget
from ui.numeric_spinbox import AdaptiveDelaySpinBox, ScientificDoubleSpinBox
from ui.pulse_config_dialog import (
    PulseConfigDialog,
    default_pulse_config,
    normalize_pulse_config,
)


KEITHLEY_DELAY_MAX_S = 100000.0
KEITHLEY_DELAY_STEP_S = 500e-9
KEITHLEY_DELAY_DECIMALS = 9
SETTINGS_SUMMARY_WIDTH = 300
SETTINGS_CHANNEL_WIDTH = 320
SETTINGS_COMMON_WIDTH = 340


class MultiSelectComboBox(QComboBox):
    """A simple checkable combo box for multi-select measurement items."""

    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setPlaceholderText("Select...")
        self.view().pressed.connect(self._toggle_item)

    def add_check_items(self, items: list[str]) -> None:
        for text in items:
            self.addItem(text)
            item = self.model().item(self.count() - 1, 0)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setData(Qt.Unchecked, Qt.CheckStateRole)
        self._refresh_text()

    def _toggle_item(self, index) -> None:
        item = self.model().itemFromIndex(index)
        item.setCheckState(
            Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
        )
        self._refresh_text()
        self.selection_changed.emit()

    def selected_items(self) -> list[str]:
        out: list[str] = []
        for row in range(self.count()):
            item = self.model().item(row, 0)
            if item.checkState() == Qt.Checked:
                out.append(self.itemText(row))
        return out

    def set_selected_items(self, values: list[str]) -> None:
        selected = set(values)
        for row in range(self.count()):
            item = self.model().item(row, 0)
            item.setCheckState(
                Qt.Checked if self.itemText(row) in selected else Qt.Unchecked
            )
        self._refresh_text()

    def _refresh_text(self) -> None:
        selected = self.selected_items()
        self.lineEdit().setText(", ".join(selected) if selected else "None")


class MeasurePopupButton(QWidget):
    """Compact button that exposes measure settings in a popup menu."""

    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.button = QToolButton()
        self.button.setPopupMode(QToolButton.InstantPopup)
        self.button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        layout.addWidget(self.button)

        self.menu = QMenu(self.button)
        self.menu.setMinimumWidth(260)
        self.button.setMenu(self.menu)

        panel = QWidget(self.menu)
        panel_layout = QFormLayout(panel)
        panel_layout.setContentsMargins(8, 8, 8, 8)

        self.measure_combo = MultiSelectComboBox()
        self.measure_combo.add_check_items(["Voltage", "Current", "Resistance"])
        self.measure_combo.set_selected_items(["Voltage", "Current"])
        panel_layout.addRow("Items:", self.measure_combo)

        self.measure_range_combo = QComboBox()
        self.measure_range_combo.addItems(
            [
                "Auto",
                "100 pA",
                "1 nA",
                "10 nA",
                "100 nA",
                "1 uA",
                "10 uA",
                "100 uA",
                "1 mA",
                "10 mA",
                "100 mA",
                "1 A",
            ]
        )
        panel_layout.addRow("Range:", self.measure_range_combo)

        self.measure_autozero_combo = QComboBox()
        self.measure_autozero_combo.addItems(["On", "Off", "Once"])
        panel_layout.addRow("Auto Zero:", self.measure_autozero_combo)

        action = QWidgetAction(self.menu)
        action.setDefaultWidget(panel)
        self.menu.addAction(action)

        self.measure_combo.selection_changed.connect(self._handle_change)
        self.measure_range_combo.currentTextChanged.connect(self._handle_change)
        self.measure_autozero_combo.currentTextChanged.connect(self._handle_change)
        self._refresh_text()

    def _handle_change(self, *args: object) -> None:
        self._refresh_text()
        self.selection_changed.emit()

    def _refresh_text(self) -> None:
        items = self.measure_combo.selected_items()
        if items:
            short_items = ", ".join(
                {"Voltage": "V", "Current": "A", "Resistance": "Ohm"}.get(item, item)
                for item in items
            )
        else:
            short_items = "None"
        self.button.setText(
            f"{short_items} | {self.measure_range_combo.currentText()} | {self.measure_autozero_combo.currentText()}"
        )


class DeviceSummaryGroupBox(QGroupBox):
    """Checkable summary tile that also selects the matching settings page."""

    clicked = Signal(int)

    def __init__(self, title: str, smu_index: int, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self._smu_index = smu_index
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self._smu_index)


class LoadingOverlay(QWidget):
    """Semi-transparent progress overlay for blocking workspace operations."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "LoadingOverlay { background-color: rgba(0, 0, 0, 105); }"
            "QFrame { background-color: #f7f7f7; border: 1px solid #8a8a8a; border-radius: 6px; }"
            "QLabel { color: #202020; font-weight: bold; }"
        )
        layout = QVBoxLayout(self)
        layout.addStretch(1)

        panel = QFrame(self)
        panel.setMinimumWidth(360)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 16, 18, 16)
        panel_layout.setSpacing(10)

        self.message_label = QLabel("Loading...")
        self.message_label.setAlignment(Qt.AlignCenter)
        panel_layout.addWidget(self.message_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        panel_layout.addWidget(self.progress_bar)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(panel)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        self.hide()

    def show_loading(self, message: str, maximum: int = 100) -> None:
        self.message_label.setText(message)
        self.progress_bar.setRange(0, max(0, int(maximum)))
        self.progress_bar.setValue(0)
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()

    def update_loading(self, value: int, message: str | None = None) -> None:
        if message is not None:
            self.message_label.setText(message)
        self.progress_bar.setValue(max(0, int(value)))


class MainWindowUI(QMainWindow):
    """
    Main application window with a top-level QTabWidget:
    Tab 1 Settings (config + preview), Tab 2 Graph (I-V curves), Tab 3 Table (data).
    """

    # Emitted when any Channel Settings form widget (SMU 1 or SMU 2) changes
    channel_config_changed = Signal()
    project_selected = Signal(int)
    project_rename_requested = Signal(int)
    project_delete_requested = Signal(int)
    project_create_requested = Signal()
    run_selected = Signal(int, int)
    run_rename_requested = Signal(int, int)
    run_delete_requested = Signal(int, int)
    quick_config_clicked = Signal(int)
    quick_config_rename_requested = Signal(int)
    quick_config_delete_requested = Signal(int)
    quick_config_quick_access_toggled = Signal(int, bool)
    CURRENT_LIMIT_RULES = {
        "260X": (100e-9, 3.03),
        "261X": (100e-9, 1.515),
        "263X": (1e-9, 1.515),
    }
    VOLTAGE_LIMIT_RULES = {
        "260X": (1e-3, 40.4),
        "261X": (1e-3, 202.0),
        "263X": (1e-3, 202.0),
    }

    def emit_config_changed(self, *args: object) -> None:
        """统一的槽函数，用于转发所有表单变动"""
        self.channel_config_changed.emit()

    def set_instrument_model(self, model_name: str | None) -> None:
        """Update limit ranges to match the connected 2600B-series model."""
        normalized = str(model_name or "").strip().upper()
        self.instrument_model = normalized or "2636B"
        self._refresh_dynamic_ui_state()

    def _instrument_model_family(self) -> str:
        model = str(getattr(self, "instrument_model", "2636B") or "").upper()
        if model.startswith(("2601B", "2602B", "2604B")):
            return "260X"
        if model.startswith(("2611B", "2612B", "2614B")):
            return "261X"
        if model.startswith(("2634B", "2635B", "2636B")):
            return "263X"
        return "263X"

    def _apply_limit_spin_config(self, smu_index: int) -> None:
        function_text = str(
            getattr(self, f"function_combo_smu{smu_index}").currentText() or ""
        ).strip()
        limit_spin = getattr(self, f"limit_spin_smu{smu_index}")
        model_family = self._instrument_model_family()

        if function_text == "Voltage":
            min_limit, max_limit = self.CURRENT_LIMIT_RULES[model_family]
            suffix = " A"
            step = 1e-3
        else:
            min_limit, max_limit = self.VOLTAGE_LIMIT_RULES[model_family]
            suffix = " V"
            step = 0.1

        limit_spin.blockSignals(True)
        limit_spin.setDecimals(9)
        limit_spin.setSingleStep(step)
        limit_spin.setRange(min_limit, max_limit)
        limit_spin.setSuffix(suffix)
        limit_spin.setValue(min(max(limit_spin.value(), min_limit), max_limit))
        limit_spin.blockSignals(False)

    def _mode_text(self, smu_index: int) -> str:
        combo = getattr(self, f"mode_combo_smu{smu_index}", None)
        return str(combo.currentText() or "").strip().lower() if combo else "fixed"

    def _apply_channel_mode_state(self, smu_index: int) -> None:
        mode = self._mode_text(smu_index)
        is_sweep = mode == "sweep"
        is_pulse = mode == "pulse"
        getattr(self, f"level_spin_smu{smu_index}").setEnabled(
            not is_sweep and not is_pulse
        )
        getattr(self, f"start_spin_smu{smu_index}").setEnabled(is_sweep)
        getattr(self, f"stop_spin_smu{smu_index}").setEnabled(is_sweep)
        getattr(self, f"step_display_smu{smu_index}").setEnabled(is_sweep)
        dual_check = getattr(self, f"dual_sweep_check_smu{smu_index}")
        dual_check.setEnabled(is_sweep)
        dual_check.setVisible(is_sweep)
        pulse_button = getattr(self, f"pulse_config_btn_smu{smu_index}")
        pulse_button.setEnabled(is_pulse)
        pulse_button.setVisible(is_pulse)
        self._refresh_pulse_button_text(smu_index)

    def _apply_ramp_visibility(self, smu_index: int) -> None:
        function_text = str(
            getattr(self, f"function_combo_smu{smu_index}").currentText() or ""
        ).strip()
        is_voltage_sweep = function_text == "Voltage" and self._mode_text(smu_index) == "sweep"
        getattr(self, f"ramp_container_smu{smu_index}").setVisible(is_voltage_sweep)
        getattr(self, f"ramp_up_row_smu{smu_index}").setVisible(
            is_voltage_sweep
            and bool(getattr(self, f"ramp_up_check_smu{smu_index}").isChecked())
        )
        getattr(self, f"ramp_down_row_smu{smu_index}").setVisible(
            is_voltage_sweep
            and bool(getattr(self, f"ramp_down_check_smu{smu_index}").isChecked())
        )

    def _apply_common_settings_state(self) -> None:
        mode1 = self._mode_text(1)
        mode2 = self._mode_text(2)
        enabled1 = bool(self.smu1_enable_group.isChecked())
        enabled2 = bool(self.smu2_enable_group.isChecked())
        sweep_modes = [
            idx
            for idx, mode, enabled in ((1, mode1, enabled1), (2, mode2, enabled2))
            if enabled and mode == "sweep"
        ]
        pulse_modes = [
            idx
            for idx, mode, enabled in ((1, mode1, enabled1), (2, mode2, enabled2))
            if enabled and mode == "pulse"
        ]

        self.sweep_points_spin.setEnabled(bool(sweep_modes))
        use_list_sweep_sampling = bool(self.pulse_list_sweep_sampling_check.isChecked())
        self.pulse_sample_interval_spin.setEnabled(
            bool(pulse_modes) and not use_list_sweep_sampling
        )

        current_stepper = str(self.stepper_selector.currentText() or "").strip()
        allowed_stepper_options = ["None"]
        if len(sweep_modes) == 2:
            allowed_stepper_options.extend(["SMU 2", "SMU 1"])

        self.stepper_selector.blockSignals(True)
        self.stepper_selector.clear()
        self.stepper_selector.addItems(allowed_stepper_options)
        if current_stepper in allowed_stepper_options:
            self.stepper_selector.setCurrentText(current_stepper)
        else:
            self.stepper_selector.setCurrentText("None")
        self.stepper_selector.setEnabled(len(allowed_stepper_options) > 1)
        self.stepper_selector.blockSignals(False)

        has_stepper_sweep = self.stepper_selector.currentText() != "None"
        self.stepper_points_spin.setEnabled(has_stepper_sweep)
        self.step_sweep_delay_spin.setEnabled(has_stepper_sweep)

    def _select_smu_settings(self, smu_index: int) -> None:
        if smu_index not in (1, 2):
            return
        if not bool(getattr(self, f"smu{smu_index}_enable_group").isChecked()):
            return
        self.smu_selector.setCurrentIndex(smu_index - 1)

    def _set_smu_selector_item_enabled(self, smu_index: int, enabled: bool) -> None:
        item = self.smu_selector.model().item(smu_index - 1)
        if item is None:
            return
        flags = item.flags()
        if enabled:
            item.setFlags(flags | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        else:
            item.setFlags(flags & ~Qt.ItemIsEnabled & ~Qt.ItemIsSelectable)

    def _apply_smu_settings_enabled_state(self) -> None:
        enabled1 = bool(self.smu1_enable_group.isChecked())
        enabled2 = bool(self.smu2_enable_group.isChecked())
        enabled_by_index = {1: enabled1, 2: enabled2}

        for smu_index, enabled in enabled_by_index.items():
            self.channel_stacked.widget(smu_index - 1).setEnabled(enabled)
            self._set_smu_selector_item_enabled(smu_index, enabled)

        current_smu = int(self.smu_selector.currentIndex()) + 1
        if enabled_by_index.get(current_smu, False):
            return
        for fallback_smu in (1, 2):
            if enabled_by_index[fallback_smu]:
                self.smu_selector.setCurrentIndex(fallback_smu - 1)
                return

    def _refresh_dynamic_ui_state(self) -> None:
        self._apply_channel_mode_state(1)
        self._apply_channel_mode_state(2)
        self._apply_ramp_visibility(1)
        self._apply_ramp_visibility(2)
        self._apply_common_settings_state()
        self._apply_limit_spin_config(1)
        self._apply_limit_spin_config(2)
        self._apply_smu_settings_enabled_state()

    def _config_widgets(self) -> list[QWidget]:
        widgets: list[QWidget] = [
            self.resource_address_edit,
            self.nplc_spin,
            self.src_meas_delay_spin,
            self.step_sweep_delay_spin,
            self.pulse_sample_interval_spin,
            self.pulse_list_sweep_sampling_check,
            self.stepper_selector,
            self.sweep_points_spin,
            self.stepper_points_spin,
            self.repeat_spin,
            self.smu_selector,
            self.smu1_enable_group,
            self.smu2_enable_group,
        ]
        for i in (1, 2):
            widgets.extend(
                [
                    getattr(self, f"function_combo_smu{i}"),
                    getattr(self, f"mode_combo_smu{i}"),
                    getattr(self, f"dual_sweep_check_smu{i}"),
                    getattr(self, f"ramp_up_check_smu{i}"),
                    getattr(self, f"ramp_up_step_smu{i}"),
                    getattr(self, f"ramp_up_delay_smu{i}"),
                    getattr(self, f"ramp_down_check_smu{i}"),
                    getattr(self, f"ramp_down_step_smu{i}"),
                    getattr(self, f"ramp_down_delay_smu{i}"),
                    getattr(self, f"level_spin_smu{i}"),
                    getattr(self, f"start_spin_smu{i}"),
                    getattr(self, f"stop_spin_smu{i}"),
                    getattr(self, f"pulse_config_btn_smu{i}"),
                    getattr(self, f"limit_spin_smu{i}"),
                    getattr(self, f"measure_combo_smu{i}"),
                    getattr(self, f"measure_range_combo_smu{i}"),
                    getattr(self, f"measure_autozero_combo_smu{i}"),
                ]
            )
        return widgets

    def collect_settings(self) -> dict:
        return {
            "resource_address": self.resource_address_edit.text(),
            "active_smu_page": int(self.smu_selector.currentIndex()),
            "common": {
                "nplc": float(self.nplc_spin.value()),
                "src_meas_delay": float(self.src_meas_delay_spin.value()),
                "step_sweep_delay": float(self.step_sweep_delay_spin.value()),
                "pulse_sample_interval": float(self.pulse_sample_interval_spin.value()),
                "pulse_list_sweep_sampling": bool(
                    self.pulse_list_sweep_sampling_check.isChecked()
                ),
                "stepper": str(self.stepper_selector.currentText() or "").strip(),
                "sweep_points": int(self.sweep_points_spin.value()),
                "stepper_points": int(self.stepper_points_spin.value()),
                "repeat": int(self.repeat_spin.value()),
            },
            "smu1": self._collect_smu_settings(1),
            "smu2": self._collect_smu_settings(2),
        }

    def _collect_smu_settings(self, smu_index: int) -> dict:
        return {
            "enabled": bool(getattr(self, f"smu{smu_index}_enable_group").isChecked()),
            "function": str(getattr(self, f"function_combo_smu{smu_index}").currentText()),
            "mode": str(getattr(self, f"mode_combo_smu{smu_index}").currentText()),
            "dual": bool(getattr(self, f"dual_sweep_check_smu{smu_index}").isChecked()),
            "ramp_up": {
                "enabled": bool(getattr(self, f"ramp_up_check_smu{smu_index}").isChecked()),
                "step": float(getattr(self, f"ramp_up_step_smu{smu_index}").value()),
                "delay": float(getattr(self, f"ramp_up_delay_smu{smu_index}").value()),
            },
            "ramp_down": {
                "enabled": bool(getattr(self, f"ramp_down_check_smu{smu_index}").isChecked()),
                "step": float(getattr(self, f"ramp_down_step_smu{smu_index}").value()),
                "delay": float(getattr(self, f"ramp_down_delay_smu{smu_index}").value()),
            },
            "level": float(getattr(self, f"level_spin_smu{smu_index}").value()),
            "start": float(getattr(self, f"start_spin_smu{smu_index}").value()),
            "stop": float(getattr(self, f"stop_spin_smu{smu_index}").value()),
            "limit": float(getattr(self, f"limit_spin_smu{smu_index}").value()),
            "pulse": normalize_pulse_config(self._pulse_configs.get(smu_index)),
            "measure": {
                "items": list(
                    getattr(self, f"measure_combo_smu{smu_index}").selected_items()
                ),
                "range": str(
                    getattr(self, f"measure_range_combo_smu{smu_index}").currentText()
                ),
                "autozero": str(
                    getattr(self, f"measure_autozero_combo_smu{smu_index}").currentText()
                ),
            },
        }

    def apply_settings(self, settings: dict, *, emit_changed: bool = True) -> None:
        widgets = self._config_widgets()
        for widget in widgets:
            widget.blockSignals(True)

        try:
            self.resource_address_edit.setText(str(settings.get("resource_address", "")))

            for smu_index in (1, 2):
                smu_cfg = settings.get(f"smu{smu_index}", {})
                getattr(self, f"smu{smu_index}_enable_group").setChecked(
                    bool(smu_cfg.get("enabled", True))
                )
                getattr(self, f"function_combo_smu{smu_index}").setCurrentText(
                    str(smu_cfg.get("function", getattr(self, f"function_combo_smu{smu_index}").currentText()))
                )
                getattr(self, f"mode_combo_smu{smu_index}").setCurrentText(
                    str(smu_cfg.get("mode", getattr(self, f"mode_combo_smu{smu_index}").currentText()))
                )
                getattr(self, f"dual_sweep_check_smu{smu_index}").setChecked(
                    bool(smu_cfg.get("dual", False))
                )

                ramp_up_cfg = smu_cfg.get("ramp_up", {})
                getattr(self, f"ramp_up_check_smu{smu_index}").setChecked(
                    bool(ramp_up_cfg.get("enabled", False))
                )
                getattr(self, f"ramp_up_step_smu{smu_index}").setValue(
                    float(ramp_up_cfg.get("step", getattr(self, f"ramp_up_step_smu{smu_index}").value()))
                )
                getattr(self, f"ramp_up_delay_smu{smu_index}").setValue(
                    float(ramp_up_cfg.get("delay", getattr(self, f"ramp_up_delay_smu{smu_index}").value()))
                )

                ramp_down_cfg = smu_cfg.get("ramp_down", {})
                getattr(self, f"ramp_down_check_smu{smu_index}").setChecked(
                    bool(ramp_down_cfg.get("enabled", False))
                )
                getattr(self, f"ramp_down_step_smu{smu_index}").setValue(
                    float(ramp_down_cfg.get("step", getattr(self, f"ramp_down_step_smu{smu_index}").value()))
                )
                getattr(self, f"ramp_down_delay_smu{smu_index}").setValue(
                    float(ramp_down_cfg.get("delay", getattr(self, f"ramp_down_delay_smu{smu_index}").value()))
                )

                getattr(self, f"level_spin_smu{smu_index}").setValue(
                    float(smu_cfg.get("level", getattr(self, f"level_spin_smu{smu_index}").value()))
                )
                getattr(self, f"start_spin_smu{smu_index}").setValue(
                    float(smu_cfg.get("start", getattr(self, f"start_spin_smu{smu_index}").value()))
                )
                getattr(self, f"stop_spin_smu{smu_index}").setValue(
                    float(smu_cfg.get("stop", getattr(self, f"stop_spin_smu{smu_index}").value()))
                )
                getattr(self, f"limit_spin_smu{smu_index}").setValue(
                    float(smu_cfg.get("limit", getattr(self, f"limit_spin_smu{smu_index}").value()))
                )
                self._pulse_configs[smu_index] = normalize_pulse_config(
                    smu_cfg.get("pulse")
                )
                self._refresh_pulse_button_text(smu_index)
                measure_cfg = smu_cfg.get("measure", {})
                getattr(self, f"measure_combo_smu{smu_index}").set_selected_items(
                    list(measure_cfg.get("items", ["Voltage", "Current"]))
                )
                getattr(self, f"measure_range_combo_smu{smu_index}").setCurrentText(
                    str(
                        measure_cfg.get(
                            "range",
                            getattr(self, f"measure_range_combo_smu{smu_index}").currentText(),
                        )
                    )
                )
                getattr(self, f"measure_autozero_combo_smu{smu_index}").setCurrentText(
                    str(
                        measure_cfg.get(
                            "autozero",
                            getattr(self, f"measure_autozero_combo_smu{smu_index}").currentText(),
                        )
                    )
                )
                getattr(self, f"measure_popup_smu{smu_index}")._refresh_text()

            self._refresh_dynamic_ui_state()

            common_cfg = settings.get("common", {})
            self.nplc_spin.setValue(float(common_cfg.get("nplc", self.nplc_spin.value())))
            self.src_meas_delay_spin.setValue(
                float(common_cfg.get("src_meas_delay", self.src_meas_delay_spin.value()))
            )
            self.step_sweep_delay_spin.setValue(
                float(common_cfg.get("step_sweep_delay", self.step_sweep_delay_spin.value()))
            )
            self.pulse_sample_interval_spin.setValue(
                float(
                    common_cfg.get(
                        "pulse_sample_interval",
                        self.pulse_sample_interval_spin.value(),
                    )
                )
            )
            self.pulse_list_sweep_sampling_check.setChecked(
                bool(
                    common_cfg.get(
                        "pulse_list_sweep_sampling",
                        self.pulse_list_sweep_sampling_check.isChecked(),
                    )
                )
            )
            self.sweep_points_spin.setValue(
                int(common_cfg.get("sweep_points", self.sweep_points_spin.value()))
            )
            self.stepper_points_spin.setValue(
                int(common_cfg.get("stepper_points", self.stepper_points_spin.value()))
            )
            self.repeat_spin.setValue(int(common_cfg.get("repeat", self.repeat_spin.value())))

            stepper_text = str(common_cfg.get("stepper", self.stepper_selector.currentText()))
            if stepper_text in [
                self.stepper_selector.itemText(i)
                for i in range(self.stepper_selector.count())
            ]:
                self.stepper_selector.setCurrentText(stepper_text)

            self.smu_selector.setCurrentIndex(
                int(settings.get("active_smu_page", self.smu_selector.currentIndex()))
            )
            self._refresh_dynamic_ui_state()
        finally:
            for widget in widgets:
                widget.blockSignals(False)

        if emit_changed:
            self.emit_config_changed()

    def reset_settings(self) -> None:
        self.apply_settings(self._default_settings)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.instrument_model = "2636B"
        self._pulse_configs = {
            1: default_pulse_config(),
            2: default_pulse_config(),
        }
        self.setWindowTitle("Keithley 2636 SMU Control")
        self.setMinimumSize(1260, 740)
        self.resize(1280, 820)

        central = QWidget()
        self.setCentralWidget(central)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)

        self._refreshing_project_tree = False
        self._quick_config_buttons: dict[int, QPushButton] = {}
        self._quick_configs: list[object] = []
        self._build_project_toolbar(central_layout)
        self._loading_overlay = LoadingOverlay(central)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(6, 0, 6, 6)
        content_layout.setSpacing(6)
        self._build_project_sidebar(content_layout)

        self.tab_widget = QTabWidget()
        content_layout.addWidget(self.tab_widget, 1)
        central_layout.addLayout(content_layout, 1)

        # --- Tab 1: Settings ---
        self.settings_tab = QWidget()
        self._build_settings_tab()
        self.tab_widget.addTab(self.settings_tab, "Settings")

        # --- Tab 2: Graph ---
        self.graph_tab = QWidget()
        self._build_graph_tab()
        self.tab_widget.addTab(self.graph_tab, "Graph")

        # --- Tab 3: Table ---
        self.table_tab = QWidget()
        self._build_table_tab()
        self.tab_widget.addTab(self.table_tab, "Table")
        self._default_settings = self.collect_settings()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        overlay = getattr(self, "_loading_overlay", None)
        if overlay is not None and self.centralWidget() is not None:
            overlay.setGeometry(self.centralWidget().rect())

    def show_loading(self, message: str, maximum: int = 100) -> None:
        self._loading_overlay.show_loading(message, maximum)

    def update_loading(self, value: int, message: str | None = None) -> None:
        self._loading_overlay.update_loading(value, message)

    def hide_loading(self) -> None:
        self._loading_overlay.hide()

    def _build_project_toolbar(self, parent_layout: QVBoxLayout) -> None:
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 6, 8, 4)
        toolbar.setSpacing(6)

        self.workspace_name_label = QLabel("Untitled Workspace")
        self.workspace_name_label.setMinimumWidth(190)
        font = self.workspace_name_label.font()
        font.setBold(True)
        self.workspace_name_label.setFont(font)
        toolbar.addWidget(self.workspace_name_label)

        self.new_project_btn = QPushButton("New Workspace")
        self.open_project_btn = QPushButton("Open Workspace")
        self.save_project_btn = QPushButton("Save Workspace")
        self.save_project_as_btn = QPushButton("Save As")
        self.save_project_as_btn.hide()
        toolbar.addWidget(self.new_project_btn)
        toolbar.addWidget(self.open_project_btn)
        toolbar.addWidget(self.save_project_btn)

        self.connection_status_label = QLabel("Disconnected")
        self.connection_status_label.setStyleSheet("color: gray; font-weight: bold;")
        toolbar.addWidget(self.connection_status_label)

        self.save_quick_config_btn = QPushButton("Save Config")
        toolbar.addWidget(self.save_quick_config_btn)
        self.import_config_btn = QPushButton("Load Config")
        toolbar.addWidget(self.import_config_btn)

        self.quick_config_container = QWidget()
        self.quick_config_layout = QHBoxLayout(self.quick_config_container)
        self.quick_config_layout.setContentsMargins(0, 0, 0, 0)
        self.quick_config_layout.setSpacing(4)
        toolbar.addWidget(self.quick_config_container, 1)

        self.quick_config_menu = QMenu(self)
        self.quick_config_menu.setMinimumWidth(320)
        self.quick_config_menu.aboutToShow.connect(self._rebuild_quick_config_menu)
        self.quick_config_dropdown_btn = QToolButton()
        self.quick_config_dropdown_btn.setArrowType(Qt.DownArrow)
        self.quick_config_dropdown_btn.setToolTip("More configurations")
        self.quick_config_dropdown_btn.setPopupMode(QToolButton.InstantPopup)
        self.quick_config_dropdown_btn.setMenu(self.quick_config_menu)
        toolbar.addWidget(self.quick_config_dropdown_btn)

        self.resource_address_edit = QLineEdit()
        self.resource_address_edit.setPlaceholderText("Auto detected resource address")
        self.resource_address_edit.setText("TCPIP0::192.168.1.100::inst0::INSTR")
        self.resource_address_edit.hide()

        parent_layout.addLayout(toolbar)

    def _build_project_sidebar(self, parent_layout: QHBoxLayout) -> None:
        sidebar = QGroupBox("Projects")
        sidebar.setFixedWidth(280)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(6, 6, 6, 6)

        self.new_project_record_btn = QPushButton("New Project")
        sidebar_layout.addWidget(self.new_project_record_btn)

        self.project_tree = QTreeWidget()
        self.project_tree.setHeaderHidden(True)
        self.project_tree.setTextElideMode(Qt.ElideRight)
        self.project_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.project_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.project_tree.currentItemChanged.connect(self._handle_project_tree_selection_changed)
        self.project_tree.customContextMenuRequested.connect(self._handle_project_tree_context_menu)
        sidebar_layout.addWidget(self.project_tree, 1)
        parent_layout.addWidget(sidebar)

    def set_workspace_title(self, name: str, path: str | None, dirty: bool) -> None:
        suffix = "*" if dirty else ""
        label = f"{name or 'Untitled Workspace'}{suffix}"
        self.workspace_name_label.setText(label)
        self.workspace_name_label.setToolTip(path or "Unsaved workspace")

    def set_project_tree(
        self,
        projects: list[object],
        active_project_id: int | None,
        active_run_id: int | None,
    ) -> None:
        self._refreshing_project_tree = True
        try:
            self.project_tree.clear()
            active_item = None
            for project in projects:
                project_id = int(getattr(project, "id", 0))
                project_label = str(getattr(project, "name", "Project"))
                project_item = QTreeWidgetItem([project_label])
                project_item.setToolTip(0, project_label)
                project_item.setData(0, Qt.UserRole, ("project", project_id, None))
                self.project_tree.addTopLevelItem(project_item)
                if active_project_id is not None and project_id == int(active_project_id):
                    active_item = project_item

                for run in getattr(project, "runs", []):
                    run_id = int(getattr(run, "id", 0))
                    name = str(getattr(run, "name", "") or "")
                    status = str(getattr(run, "status", "ready") or "ready")
                    suffix = f"  {name}" if name else ""
                    status_suffix = "" if status == "completed" else f"  [{status}]"
                    run_label = f"RUN {run_id}{suffix}{status_suffix}"
                    run_item = QTreeWidgetItem([run_label])
                    run_item.setToolTip(0, run_label)
                    run_item.setData(0, Qt.UserRole, ("run", project_id, run_id))
                    project_item.addChild(run_item)
                    if (
                        active_project_id is not None
                        and active_run_id is not None
                        and project_id == int(active_project_id)
                        and run_id == int(active_run_id)
                    ):
                        active_item = run_item
                project_item.setExpanded(True)
            if active_item is not None:
                self.project_tree.setCurrentItem(active_item)
        finally:
            self._refreshing_project_tree = False

    def _summary_value_label(self, text: str = "-") -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        label.setMinimumWidth(0)
        label.setToolTip(text)
        return label

    def set_summary_label_text(self, label: QLabel, text: str) -> None:
        label.setText(text)
        label.setToolTip(text)

    def set_quick_configs(self, configs: list[object]) -> None:
        self._quick_configs = list(configs)
        while self.quick_config_layout.count():
            item = self.quick_config_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._quick_config_buttons.clear()

        quick_access_configs = [
            config for config in configs if bool(getattr(config, "quick_access", False))
        ][:3]
        for config in quick_access_configs:
            config_id = int(getattr(config, "id", 0))
            name = str(getattr(config, "name", "Config"))
            button = QPushButton(self._quick_config_button_text(name))
            button.setMinimumWidth(180)
            button.setMaximumWidth(220)
            button.setToolTip(name)
            button.setContextMenuPolicy(Qt.CustomContextMenu)
            button.clicked.connect(
                lambda checked=False, item_id=config_id: self.quick_config_clicked.emit(item_id)
            )
            button.customContextMenuRequested.connect(
                lambda pos, item_id=config_id, source=button: self._handle_quick_config_menu(
                    source,
                    pos,
                    item_id,
                )
            )
            self.quick_config_layout.addWidget(button)
            self._quick_config_buttons[config_id] = button
        self.quick_config_layout.addStretch(1)

    def _quick_config_button_text(self, name: str) -> str:
        return self.fontMetrics().elidedText(str(name), Qt.ElideRight, 190)

    def _quick_access_count(self) -> int:
        return sum(
            1 for config in self._quick_configs if bool(getattr(config, "quick_access", False))
        )

    def _rebuild_quick_config_menu(self) -> None:
        self.quick_config_menu.clear()

        notice = QLabel("Maximum 3 quick access configs.")
        notice.setStyleSheet("color: #8a5300; padding: 4px 8px;")
        notice.hide()
        notice_action = QWidgetAction(self.quick_config_menu)
        notice_action.setDefaultWidget(notice)
        self.quick_config_menu.addAction(notice_action)

        if not self._quick_configs:
            empty_action = self.quick_config_menu.addAction("No configurations saved")
            empty_action.setEnabled(False)
            return

        for config in self._quick_configs:
            config_id = int(getattr(config, "id", 0))
            name = str(getattr(config, "name", "Config"))
            is_quick_access = bool(getattr(config, "quick_access", False))

            row = QWidget(self.quick_config_menu)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 2, 6, 2)
            row_layout.setSpacing(6)

            name_button = QPushButton(
                self.fontMetrics().elidedText(name, Qt.ElideRight, 230)
            )
            name_button.setFlat(True)
            name_button.setMinimumWidth(230)
            name_button.setToolTip(name)
            name_button.setContextMenuPolicy(Qt.CustomContextMenu)
            name_button.clicked.connect(
                lambda checked=False, item_id=config_id: self._handle_quick_config_menu_click(
                    item_id
                )
            )
            name_button.customContextMenuRequested.connect(
                lambda pos, item_id=config_id, source=name_button: self._handle_quick_config_menu(
                    source,
                    pos,
                    item_id,
                )
            )

            star_button = QToolButton()
            star_button.setText("★" if is_quick_access else "☆")
            star_button.setToolTip(
                "Remove from quick access" if is_quick_access else "Add to quick access"
            )
            star_button.setAutoRaise(True)
            star_button.clicked.connect(
                lambda checked=False,
                item_id=config_id,
                enabled=not is_quick_access,
                notice_label=notice: self._handle_quick_access_star_click(
                    item_id,
                    enabled,
                    notice_label,
                )
            )

            row_layout.addWidget(name_button, 1)
            row_layout.addWidget(star_button)

            action = QWidgetAction(self.quick_config_menu)
            action.setDefaultWidget(row)
            self.quick_config_menu.addAction(action)

    def _handle_quick_config_menu_click(self, config_id: int) -> None:
        self.quick_config_menu.close()
        self.quick_config_clicked.emit(config_id)

    def _handle_quick_access_star_click(
        self,
        config_id: int,
        enabled: bool,
        notice_label: QLabel,
    ) -> None:
        if enabled and self._quick_access_count() >= 3:
            notice_label.show()
            self.quick_config_menu.adjustSize()
            QTimer.singleShot(1600, notice_label.hide)
            return
        self.quick_config_menu.close()
        self.quick_config_quick_access_toggled.emit(config_id, enabled)

    def selected_run_id(self) -> int | None:
        item = self.project_tree.currentItem()
        if item is None:
            return None
        value = item.data(0, Qt.UserRole)
        if not isinstance(value, tuple) or value[0] != "run":
            return None
        return int(value[2])

    def _handle_project_tree_selection_changed(self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None) -> None:
        if self._refreshing_project_tree or current is None:
            return
        value = current.data(0, Qt.UserRole)
        if not isinstance(value, tuple):
            return
        item_type, project_id, run_id = value
        if item_type == "project":
            self.project_selected.emit(int(project_id))
        elif item_type == "run":
            self.run_selected.emit(int(project_id), int(run_id))

    def _handle_project_tree_context_menu(self, pos) -> None:
        item = self.project_tree.itemAt(pos)
        if item is None:
            return
        value = item.data(0, Qt.UserRole)
        if not isinstance(value, tuple):
            return
        item_type, project_id, run_id = value
        menu = QMenu(self.project_tree)
        rename_action = menu.addAction("Rename")
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.project_tree.mapToGlobal(pos))
        if action == rename_action and item_type == "project":
            self.project_rename_requested.emit(int(project_id))
        elif action == delete_action and item_type == "project":
            self.project_delete_requested.emit(int(project_id))
        elif action == rename_action and item_type == "run":
            self.run_rename_requested.emit(int(project_id), int(run_id))
        elif action == delete_action and item_type == "run":
            self.run_delete_requested.emit(int(project_id), int(run_id))

    def _handle_quick_config_menu(self, button: QPushButton, pos, config_id: int) -> None:
        menu = QMenu(button)
        rename_action = menu.addAction("Rename")
        delete_action = menu.addAction("Delete")
        action = menu.exec(button.mapToGlobal(pos))
        if action == rename_action:
            self.quick_config_rename_requested.emit(config_id)
        elif action == delete_action:
            self.quick_config_delete_requested.emit(config_id)

    def _build_settings_tab(self) -> None:
        layout = QVBoxLayout(self.settings_tab)

        # Legacy controls kept hidden so older controller paths can still bind
        # while the visible project toolbar owns project and connection actions.
        self.scan_btn = QPushButton("Scan")
        self.connect_btn = QPushButton("Connect")
        self.export_config_btn = QPushButton("Export Config")
        self.reset_config_btn = QPushButton("Reset")
        for button in (
            self.scan_btn,
            self.connect_btn,
            self.export_config_btn,
            self.reset_config_btn,
        ):
            button.hide()

        # ---------- Upper: Output Preview ----------
        preview_group = QGroupBox("Output Preview")
        preview_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(6, 6, 6, 6)
        preview_layout.setSpacing(0)

        # Preview waveform (Settings tab)
        self.preview_plot_placeholder = PreviewGraphWidget()
        self.preview_plot_placeholder.setMinimumHeight(140)
        self.preview_plot_placeholder.setMaximumHeight(140)
        preview_layout.addWidget(self.preview_plot_placeholder)
        preview_group.setFixedHeight(preview_group.sizeHint().height())

        layout.addWidget(preview_group)

        # ---------- Lower: Three columns (Device Summary | Channel Settings | Common Settings) ----------
        lower_layout = QHBoxLayout()
        lower_layout.setSpacing(8)

        # --- Left: Device Summary ---
        summary_group = QGroupBox("Device Summary")
        summary_group.setFixedWidth(SETTINGS_SUMMARY_WIDTH)
        summary_layout = QVBoxLayout(summary_group)

        # SMU 1 status: Function, Mode, Source, Limit, Ramp
        smu1_group = DeviceSummaryGroupBox("SMU 1", 1)
        smu1_group.setCheckable(True)
        smu1_group.setChecked(True)
        smu1_group.setToolTip("Enable SMU 1 for the next run")
        smu1_group.clicked.connect(self._select_smu_settings)
        smu1_layout = QFormLayout(smu1_group)
        smu1_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.smu1_function_label = self._summary_value_label()
        self.smu1_mode_label = self._summary_value_label()
        self.smu1_source_label = self._summary_value_label()
        self.smu1_limit_label = self._summary_value_label()
        self.smu1_measure_label = self._summary_value_label()
        self.smu1_ramp_label = self._summary_value_label()
        smu1_layout.addRow("Function:", self.smu1_function_label)
        smu1_layout.addRow("Mode:", self.smu1_mode_label)
        smu1_layout.addRow("Source:", self.smu1_source_label)
        smu1_layout.addRow("Limit:", self.smu1_limit_label)
        smu1_layout.addRow("Measure:", self.smu1_measure_label)
        smu1_layout.addRow("Ramp:", self.smu1_ramp_label)
        summary_layout.addWidget(smu1_group)
        self.smu1_enable_group = smu1_group

        # SMU 2 status: Function, Mode, Source, Limit, Ramp
        smu2_group = DeviceSummaryGroupBox("SMU 2", 2)
        smu2_group.setCheckable(True)
        smu2_group.setChecked(True)
        smu2_group.setToolTip("Enable SMU 2 for the next run")
        smu2_group.clicked.connect(self._select_smu_settings)
        smu2_layout = QFormLayout(smu2_group)
        smu2_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.smu2_function_label = self._summary_value_label()
        self.smu2_mode_label = self._summary_value_label()
        self.smu2_source_label = self._summary_value_label()
        self.smu2_limit_label = self._summary_value_label()
        self.smu2_measure_label = self._summary_value_label()
        self.smu2_ramp_label = self._summary_value_label()
        smu2_layout.addRow("Function:", self.smu2_function_label)
        smu2_layout.addRow("Mode:", self.smu2_mode_label)
        smu2_layout.addRow("Source:", self.smu2_source_label)
        smu2_layout.addRow("Limit:", self.smu2_limit_label)
        smu2_layout.addRow("Measure:", self.smu2_measure_label)
        smu2_layout.addRow("Ramp:", self.smu2_ramp_label)
        summary_layout.addWidget(smu2_group)
        self.smu2_enable_group = smu2_group

        summary_layout.addStretch()
        lower_layout.addWidget(summary_group)

        # --- Middle: Channel Settings (StackedWidget: one form per SMU) ---
        channel_group = QGroupBox("Channel Settings")
        channel_group.setFixedWidth(SETTINGS_CHANNEL_WIDTH)
        channel_layout = QVBoxLayout(channel_group)

        self.smu_selector = QComboBox()
        self.smu_selector.addItems(["Configure SMU 1", "Configure SMU 2"])
        channel_layout.addWidget(self.smu_selector)

        self.channel_stacked = QStackedWidget()
        self.channel_stacked.addWidget(self._build_smu_form_page(1))
        self.channel_stacked.addWidget(self._build_smu_form_page(2))
        self.smu_selector.currentIndexChanged.connect(self.channel_stacked.setCurrentIndex)
        channel_layout.addWidget(self.channel_stacked)
        lower_layout.addWidget(channel_group)

        # --- Right: Common Settings ---
        common_group = QGroupBox("Common Settings")
        common_group.setFixedWidth(SETTINGS_COMMON_WIDTH)
        common_layout = QFormLayout(common_group)
        common_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.nplc_spin = QDoubleSpinBox()
        self.nplc_spin.setRange(0.01, 25.0)
        self.nplc_spin.setValue(1.0)
        self.nplc_spin.setDecimals(2)
        common_layout.addRow("NPLC:", self.nplc_spin)
        self.nplc_spin.valueChanged.connect(self.emit_config_changed)

        self.measure_window_label = QLabel("20.0 ms")
        self.measure_window_label.setMinimumWidth(0)
        common_layout.addRow("Measure Window:", self.measure_window_label)

        self.src_meas_delay_spin = AdaptiveDelaySpinBox()
        self.src_meas_delay_spin.setRange(0.0, KEITHLEY_DELAY_MAX_S)
        self.src_meas_delay_spin.setValue(0.0)
        self.src_meas_delay_spin.setSuffix(" s")
        self.src_meas_delay_spin.setDecimals(KEITHLEY_DELAY_DECIMALS)
        self.src_meas_delay_spin.setSingleStep(KEITHLEY_DELAY_STEP_S)
        common_layout.addRow("Src to Meas Delay:", self.src_meas_delay_spin)
        self.src_meas_delay_spin.valueChanged.connect(self.emit_config_changed)

        self.step_sweep_delay_spin = AdaptiveDelaySpinBox()
        self.step_sweep_delay_spin.setRange(0.0, KEITHLEY_DELAY_MAX_S)
        self.step_sweep_delay_spin.setValue(0.0)
        self.step_sweep_delay_spin.setSuffix(" s")
        self.step_sweep_delay_spin.setDecimals(KEITHLEY_DELAY_DECIMALS)
        self.step_sweep_delay_spin.setSingleStep(KEITHLEY_DELAY_STEP_S)
        common_layout.addRow("Step to Sweep Delay:", self.step_sweep_delay_spin)
        self.step_sweep_delay_spin.valueChanged.connect(self.emit_config_changed)

        self.pulse_sample_interval_spin = AdaptiveDelaySpinBox()
        self.pulse_sample_interval_spin.setRange(0.0, KEITHLEY_DELAY_MAX_S)
        self.pulse_sample_interval_spin.setValue(0.001)
        self.pulse_sample_interval_spin.setSuffix(" s")
        self.pulse_sample_interval_spin.setDecimals(KEITHLEY_DELAY_DECIMALS)
        self.pulse_sample_interval_spin.setSingleStep(0.0001)
        common_layout.addRow("Pulse Sample Interval:", self.pulse_sample_interval_spin)
        self.pulse_sample_interval_spin.valueChanged.connect(self.emit_config_changed)

        self.pulse_list_sweep_sampling_check = QCheckBox(
            "Use list sweep sampling (NPLC + delay)"
        )
        self.pulse_list_sweep_sampling_check.setChecked(False)
        pulse_list_sweep_tooltip = (
            "Pulse sample interval is ignored. Point spacing is estimated as NPLC "
            "integration window + source-to-measure delay."
        )
        self.pulse_list_sweep_sampling_check.setToolTip(pulse_list_sweep_tooltip)
        self.pulse_sample_interval_spin.setToolTip(pulse_list_sweep_tooltip)
        common_layout.addRow("", self.pulse_list_sweep_sampling_check)
        self.pulse_list_sweep_sampling_check.stateChanged.connect(
            self.emit_config_changed
        )
        self.pulse_list_sweep_sampling_check.stateChanged.connect(
            self._refresh_dynamic_ui_state
        )

        self.stepper_selector = QComboBox()
        self.stepper_selector.addItems(["None", "SMU 2", "SMU 1"])
        common_layout.addRow("Stepper:", self.stepper_selector)
        self.stepper_selector.currentIndexChanged.connect(self.emit_config_changed)

        self.sweep_points_spin = QSpinBox()
        self.sweep_points_spin.setRange(2, 10000)
        self.sweep_points_spin.setValue(51)
        common_layout.addRow("Sweep Points:", self.sweep_points_spin)
        self.sweep_points_spin.valueChanged.connect(self.emit_config_changed)

        self.stepper_points_spin = QSpinBox()
        self.stepper_points_spin.setRange(1, 100)
        self.stepper_points_spin.setValue(5)
        common_layout.addRow("Stepper Points:", self.stepper_points_spin)
        self.stepper_points_spin.valueChanged.connect(self.emit_config_changed)

        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 1000)
        self.repeat_spin.setValue(1)
        common_layout.addRow("Repeat:", self.repeat_spin)
        self.repeat_spin.valueChanged.connect(self.emit_config_changed)

        self.calculated_points_label = QLabel("0")
        self.calculated_points_label.setWordWrap(True)
        self.calculated_points_label.setMinimumWidth(0)
        font = self.calculated_points_label.font()
        font.setBold(True)
        self.calculated_points_label.setFont(font)
        common_layout.addRow("Calculated Points:", self.calculated_points_label)

        common_layout.addRow(QLabel(""))  # spacer
        self.run_btn = QPushButton("Run")
        self.run_btn.setMinimumHeight(48)
        font = self.run_btn.font()
        font.setPointSize(12)
        self.run_btn.setFont(font)
        self.run_btn.setStyleSheet(
            "QPushButton { background-color: #2d7d2d; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #3d9d3d; }"
            "QPushButton:pressed { background-color: #1d5d1d; }"
            "QPushButton:disabled { background-color: #808080; color: #d0d0d0; }"
        )
        self.run_btn.setEnabled(False)
        common_layout.addRow(self.run_btn)

        self.abort_btn = QPushButton("Abort")
        self.abort_btn.setMinimumHeight(48)
        self.abort_btn.setFont(font)
        self.abort_btn.setStyleSheet(
            "QPushButton { background-color: #b71c1c; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #d32f2f; }"
            "QPushButton:pressed { background-color: #8b0000; }"
            "QPushButton:disabled { background-color: #808080; color: #d0d0d0; }"
        )
        self.abort_btn.setEnabled(False)
        common_layout.addRow(self.abort_btn)

        lower_layout.addWidget(common_group)
        lower_layout.addStretch(1)

        layout.addLayout(lower_layout)
        layout.addStretch(1)
        self._connect_smu_form_signals()

    def _build_smu_form_page(self, smu_index: int) -> QWidget:
        """Build one Channel Settings form page for the given SMU (1 or 2)."""
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        function_combo = QComboBox()
        function_combo.addItems(["Voltage", "Current"])
        form.addRow("Function:", function_combo)

        mode_combo = QComboBox()
        mode_combo.addItems(["Fixed", "Sweep", "Pulse"])
        form.addRow("Mode:", mode_combo)

        dual_sweep_check = QCheckBox("Dual Sweep")
        form.addRow("", dual_sweep_check)

        # Ramp Up / Ramp Down (visible only when Function is Voltage)
        ramp_container = QWidget()
        ramp_container_layout = QVBoxLayout(ramp_container)
        ramp_container_layout.setContentsMargins(0, 0, 0, 0)

        ramp_up_check = QCheckBox("Ramp Up from 0V")
        ramp_container_layout.addWidget(ramp_up_check)
        ramp_up_step = ScientificDoubleSpinBox()
        ramp_up_step.setRange(0.01, 1000.0)
        ramp_up_step.setValue(0.5)
        ramp_up_step.setSuffix(" V")
        ramp_up_step.setDecimals(9)
        ramp_up_delay = AdaptiveDelaySpinBox()
        ramp_up_delay.setRange(0.0, 3600.0)
        ramp_up_delay.setValue(1.0)
        ramp_up_delay.setSuffix(" s")
        ramp_up_delay.setDecimals(9)
        ramp_up_row = QWidget()
        ramp_up_hbox = QHBoxLayout(ramp_up_row)
        ramp_up_hbox.setContentsMargins(0, 0, 0, 0)
        ramp_up_hbox.addWidget(ramp_up_step)
        ramp_up_hbox.addWidget(ramp_up_delay)
        ramp_container_layout.addWidget(ramp_up_row)

        ramp_down_check = QCheckBox("Ramp Down to 0V")
        ramp_container_layout.addWidget(ramp_down_check)
        ramp_down_step = ScientificDoubleSpinBox()
        ramp_down_step.setRange(0.01, 1000.0)
        ramp_down_step.setValue(0.5)
        ramp_down_step.setSuffix(" V")
        ramp_down_step.setDecimals(9)
        ramp_down_delay = AdaptiveDelaySpinBox()
        ramp_down_delay.setRange(0.0, 3600.0)
        ramp_down_delay.setValue(1.0)
        ramp_down_delay.setSuffix(" s")
        ramp_down_delay.setDecimals(9)
        ramp_down_row = QWidget()
        ramp_down_hbox = QHBoxLayout(ramp_down_row)
        ramp_down_hbox.setContentsMargins(0, 0, 0, 0)
        ramp_down_hbox.addWidget(ramp_down_step)
        ramp_down_hbox.addWidget(ramp_down_delay)
        ramp_container_layout.addWidget(ramp_down_row)

        form.addRow("Ramp:", ramp_container)

        level_spin = ScientificDoubleSpinBox()
        level_spin.setRange(-1e3, 1e3)
        level_spin.setDecimals(9)
        form.addRow("Bias Level:", level_spin)

        start_spin = ScientificDoubleSpinBox()
        start_spin.setRange(-1e3, 1e3)
        start_spin.setDecimals(9)
        form.addRow("Start:", start_spin)

        stop_spin = ScientificDoubleSpinBox()
        stop_spin.setRange(-1e3, 1e3)
        stop_spin.setDecimals(9)
        form.addRow("Stop:", stop_spin)

        step_display = ScientificDoubleSpinBox()
        step_display.setRange(0.0, 2000.0)
        step_display.setDecimals(9)
        step_display.setSingleStep(0.1)
        step_display.setSuffix(" V")
        step_display.setKeyboardTracking(False)
        form.addRow("Step:", step_display)

        pulse_config_btn = QPushButton("Configure Pulse...")
        pulse_config_btn.setVisible(False)
        form.addRow("", pulse_config_btn)

        limit_spin = ScientificDoubleSpinBox()
        limit_spin.setRange(1e-9, 1.515)
        limit_spin.setDecimals(9)
        limit_spin.setSingleStep(1e-3)
        limit_spin.setSuffix(" A")
        form.addRow("Limit:", limit_spin)

        measure_popup = MeasurePopupButton()
        form.addRow("Measure:", measure_popup)

        setattr(self, f"function_combo_smu{smu_index}", function_combo)
        setattr(self, f"mode_combo_smu{smu_index}", mode_combo)
        setattr(self, f"dual_sweep_check_smu{smu_index}", dual_sweep_check)
        setattr(self, f"ramp_container_smu{smu_index}", ramp_container)
        setattr(self, f"ramp_up_check_smu{smu_index}", ramp_up_check)
        setattr(self, f"ramp_up_step_smu{smu_index}", ramp_up_step)
        setattr(self, f"ramp_up_delay_smu{smu_index}", ramp_up_delay)
        setattr(self, f"ramp_up_row_smu{smu_index}", ramp_up_row)
        setattr(self, f"ramp_down_check_smu{smu_index}", ramp_down_check)
        setattr(self, f"ramp_down_step_smu{smu_index}", ramp_down_step)
        setattr(self, f"ramp_down_delay_smu{smu_index}", ramp_down_delay)
        setattr(self, f"ramp_down_row_smu{smu_index}", ramp_down_row)
        setattr(self, f"level_spin_smu{smu_index}", level_spin)
        setattr(self, f"start_spin_smu{smu_index}", start_spin)
        setattr(self, f"stop_spin_smu{smu_index}", stop_spin)
        setattr(self, f"step_display_smu{smu_index}", step_display)
        setattr(self, f"pulse_config_btn_smu{smu_index}", pulse_config_btn)
        setattr(self, f"limit_spin_smu{smu_index}", limit_spin)
        setattr(self, f"measure_popup_smu{smu_index}", measure_popup)
        setattr(self, f"measure_combo_smu{smu_index}", measure_popup.measure_combo)
        setattr(self, f"measure_range_combo_smu{smu_index}", measure_popup.measure_range_combo)
        setattr(self, f"measure_autozero_combo_smu{smu_index}", measure_popup.measure_autozero_combo)
        return page

    def _connect_smu_form_signals(self) -> None:
        """Connect all Channel Settings form widgets to emit_config_changed (after both SMU pages exist)."""
        for i in (1, 2):
            getattr(self, f"function_combo_smu{i}").currentTextChanged.connect(self.emit_config_changed)
            getattr(self, f"function_combo_smu{i}").currentIndexChanged.connect(self.emit_config_changed)
            getattr(self, f"mode_combo_smu{i}").currentTextChanged.connect(self.emit_config_changed)
            getattr(self, f"mode_combo_smu{i}").currentIndexChanged.connect(self.emit_config_changed)
            getattr(self, f"dual_sweep_check_smu{i}").stateChanged.connect(self.emit_config_changed)
            getattr(self, f"ramp_up_check_smu{i}").stateChanged.connect(self.emit_config_changed)
            getattr(self, f"ramp_up_step_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"ramp_up_delay_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"ramp_down_check_smu{i}").stateChanged.connect(self.emit_config_changed)
            getattr(self, f"ramp_down_step_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"ramp_down_delay_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"level_spin_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"start_spin_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"stop_spin_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"pulse_config_btn_smu{i}").clicked.connect(
                lambda checked=False, smu_index=i: self.open_pulse_config_dialog(smu_index)
            )
            getattr(self, f"limit_spin_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"measure_popup_smu{i}").selection_changed.connect(self.emit_config_changed)
            getattr(self, f"function_combo_smu{i}").currentTextChanged.connect(self._refresh_dynamic_ui_state)
            getattr(self, f"mode_combo_smu{i}").currentTextChanged.connect(self._refresh_dynamic_ui_state)
            getattr(self, f"ramp_up_check_smu{i}").stateChanged.connect(self._refresh_dynamic_ui_state)
            getattr(self, f"ramp_down_check_smu{i}").stateChanged.connect(self._refresh_dynamic_ui_state)

        self.smu1_enable_group.toggled.connect(self.emit_config_changed)
        self.smu2_enable_group.toggled.connect(self.emit_config_changed)
        self.smu1_enable_group.toggled.connect(self._refresh_dynamic_ui_state)
        self.smu2_enable_group.toggled.connect(self._refresh_dynamic_ui_state)
        self.stepper_selector.currentTextChanged.connect(self._refresh_dynamic_ui_state)
        self._refresh_dynamic_ui_state()

    def _refresh_pulse_button_text(self, smu_index: int) -> None:
        button = getattr(self, f"pulse_config_btn_smu{smu_index}", None)
        if button is None:
            return
        config = normalize_pulse_config(self._pulse_configs.get(smu_index))
        count = len(config["combinations"])
        suffix = "combination" if count == 1 else "combinations"
        text = f"Configure Pulse... ({count} {suffix})"
        button.setText(text)
        button.setToolTip(text)

    def open_pulse_config_dialog(self, smu_index: int) -> None:
        current = normalize_pulse_config(self._pulse_configs.get(smu_index))
        dialog = PulseConfigDialog(
            current,
            parent=self,
            title=f"SMU {smu_index} Pulse Configuration",
        )
        if dialog.exec() != PulseConfigDialog.Accepted:
            return

        self._pulse_configs[smu_index] = normalize_pulse_config(dialog.config())
        self._refresh_pulse_button_text(smu_index)
        self.emit_config_changed()

    def _build_graph_tab(self) -> None:
        layout = QVBoxLayout(self.graph_tab)

        # Toolbar
        graph_toolbar = QHBoxLayout()
        self.clear_plot_btn = QPushButton("Clear Plot")
        self.autoscale_btn = QPushButton("Autoscale")
        self.export_image_btn = QPushButton("Export to Image")
        self.x_axis_combo = QComboBox()
        self.y_axis_combo = QComboBox()
        axis_options = [
            "Time",
            "SMU1 Voltage",
            "SMU1 Current",
            "SMU1 Resistance",
            "SMU2 Voltage",
            "SMU2 Current",
            "SMU2 Resistance",
        ]
        self.x_axis_combo.addItems(axis_options)
        self.y_axis_combo.addItems(axis_options)
        self.x_axis_combo.setCurrentText("SMU1 Voltage")
        self.y_axis_combo.setCurrentText("SMU1 Current")
        self.graph_linear_btn = QPushButton("Linear")
        self.graph_log_btn = QPushButton("Log |I|")
        self.graph_linear_btn.setCheckable(True)
        self.graph_log_btn.setCheckable(True)
        self.graph_linear_btn.setChecked(True)
        self.graph_show_ramping_check = QCheckBox("Show ramping")
        self.graph_show_ramping_check.setChecked(True)
        self.graph_show_ramping_check.setVisible(False)
        graph_toolbar.addWidget(self.clear_plot_btn)
        graph_toolbar.addWidget(self.autoscale_btn)
        graph_toolbar.addWidget(self.export_image_btn)
        graph_toolbar.addWidget(QLabel("X Axis:"))
        graph_toolbar.addWidget(self.x_axis_combo)
        graph_toolbar.addWidget(QLabel("Y Axis:"))
        graph_toolbar.addWidget(self.y_axis_combo)
        graph_toolbar.addWidget(self.graph_linear_btn)
        graph_toolbar.addWidget(self.graph_log_btn)
        graph_toolbar.addWidget(self.graph_show_ramping_check)
        graph_toolbar.addStretch()
        layout.addLayout(graph_toolbar)

        # I-V measurement plot (Graph tab)
        self.graph_plot_placeholder = MeasurementGraphWidget()
        self.graph_plot_placeholder.setMinimumHeight(400)
        layout.addWidget(self.graph_plot_placeholder)

    def _build_table_tab(self) -> None:
        layout = QVBoxLayout(self.table_tab)

        self.data_table = QTableWidget()
        self.data_table.setColumnCount(7)
        self.data_table.setHorizontalHeaderLabels([
            "Time (s)",
            "SMU 1 V",
            "SMU 1 I",
            "SMU 1 R",
            "SMU 2 V",
            "SMU 2 I",
            "SMU 2 R",
        ])
        self.data_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.data_table)

        self.csv_show_ramping_check = QCheckBox("Show/export ramping")
        self.csv_show_ramping_check.setChecked(True)
        self.csv_show_ramping_check.setVisible(False)
        layout.addWidget(self.csv_show_ramping_check)

        self.export_csv_btn = QPushButton("Export to CSV")
        self.export_csv_btn.setMinimumHeight(36)
        layout.addWidget(self.export_csv_btn)
