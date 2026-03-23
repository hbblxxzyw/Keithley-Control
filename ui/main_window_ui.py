"""
Main window UI with top-level 3-tab architecture: Settings, Graph, Table.

Industrial-style layout: configuration and preview in Settings; full-screen
Graph and Table tabs for measurement data.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QValidator
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
    QToolButton,
    QMenu,
    QWidgetAction,
    QSizePolicy,
)

from ui.graph_widget import PreviewGraphWidget, MeasurementGraphWidget


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


class ScientificDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that accepts scientific notation and displays tiny values as 1E-7."""

    scientific_threshold = 1e-5

    def textFromValue(self, value: float) -> str:
        abs_value = abs(float(value))
        if abs_value != 0.0 and abs_value < self.scientific_threshold:
            return f"{value:.0E}"
        decimals = max(0, min(self.decimals(), 9))
        text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
        return text or "0"

    def valueFromText(self, text: str) -> float:
        cleaned = text.replace(self.suffix(), "").strip()
        if not cleaned:
            return self.minimum()
        return float(cleaned)

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:
        cleaned = text.replace(self.suffix(), "").strip()
        if not cleaned or cleaned in {"-", "+", ".", "-.", "+.", "E", "e"}:
            return (QValidator.State.Intermediate, text, pos)
        try:
            float(cleaned)
        except ValueError:
            partial_markers = ("e", "e+", "e-", "E", "E+", "E-")
            if cleaned.endswith(partial_markers):
                return (QValidator.State.Intermediate, text, pos)
            return (QValidator.State.Invalid, text, pos)
        return (QValidator.State.Acceptable, text, pos)


class MainWindowUI(QMainWindow):
    """
    Main application window with a top-level QTabWidget:
    Tab 1 Settings (config + preview), Tab 2 Graph (I-V curves), Tab 3 Table (data).
    """

    # Emitted when any Channel Settings form widget (SMU 1 or SMU 2) changes
    channel_config_changed = Signal()
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
        is_sweep = self._mode_text(smu_index) == "sweep"
        getattr(self, f"level_spin_smu{smu_index}").setEnabled(not is_sweep)
        getattr(self, f"start_spin_smu{smu_index}").setEnabled(is_sweep)
        getattr(self, f"stop_spin_smu{smu_index}").setEnabled(is_sweep)
        getattr(self, f"step_display_smu{smu_index}").setEnabled(is_sweep)
        dual_check = getattr(self, f"dual_sweep_check_smu{smu_index}")
        dual_check.setEnabled(is_sweep)
        dual_check.setVisible(is_sweep)

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
        sweep_modes = [idx for idx, mode in ((1, mode1), (2, mode2)) if mode == "sweep"]

        self.sweep_points_spin.setEnabled(bool(sweep_modes))

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

    def _refresh_dynamic_ui_state(self) -> None:
        self._apply_channel_mode_state(1)
        self._apply_channel_mode_state(2)
        self._apply_ramp_visibility(1)
        self._apply_ramp_visibility(2)
        self._apply_common_settings_state()
        self._apply_limit_spin_config(1)
        self._apply_limit_spin_config(2)

    def _config_widgets(self) -> list[QWidget]:
        widgets: list[QWidget] = [
            self.resource_address_edit,
            self.nplc_spin,
            self.src_meas_delay_spin,
            self.step_sweep_delay_spin,
            self.stepper_selector,
            self.sweep_points_spin,
            self.stepper_points_spin,
            self.repeat_spin,
            self.smu_selector,
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

    def apply_settings(self, settings: dict) -> None:
        widgets = self._config_widgets()
        for widget in widgets:
            widget.blockSignals(True)

        try:
            self.resource_address_edit.setText(str(settings.get("resource_address", "")))

            for smu_index in (1, 2):
                smu_cfg = settings.get(f"smu{smu_index}", {})
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

        self.emit_config_changed()

    def reset_settings(self) -> None:
        self.apply_settings(self._default_settings)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.instrument_model = "2636B"
        self.setWindowTitle("Keithley 2636 SMU Control")
        self.setMinimumSize(1000, 700)
        self.resize(1200, 800)

        central = QWidget()
        self.setCentralWidget(central)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)

        self.tab_widget = QTabWidget()
        central_layout.addWidget(self.tab_widget)

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

    def _build_settings_tab(self) -> None:
        layout = QVBoxLayout(self.settings_tab)

        # ---------- Connection row ----------
        conn_layout = QHBoxLayout()
        conn_layout.addWidget(QLabel("Address:"))
        self.resource_address_edit = QLineEdit()
        self.resource_address_edit.setPlaceholderText("e.g. TCPIP0::192.168.1.100::inst0::INSTR")
        self.resource_address_edit.setText("TCPIP0::192.168.1.100::inst0::INSTR")
        conn_layout.addWidget(self.resource_address_edit)
        self.scan_btn = QPushButton("Scan")
        conn_layout.addWidget(self.scan_btn)
        self.connect_btn = QPushButton("Connect")
        conn_layout.addWidget(self.connect_btn)
        self.import_config_btn = QPushButton("Import Config")
        conn_layout.addWidget(self.import_config_btn)
        self.export_config_btn = QPushButton("Export Config")
        conn_layout.addWidget(self.export_config_btn)
        self.reset_config_btn = QPushButton("Reset")
        conn_layout.addWidget(self.reset_config_btn)
        self.connection_status_label = QLabel("Disconnected")
        self.connection_status_label.setStyleSheet("color: gray; font-weight: bold;")
        conn_layout.addWidget(self.connection_status_label)
        conn_layout.addStretch()
        layout.addLayout(conn_layout)

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

        # --- Left: Device Summary ---
        summary_group = QGroupBox("Device Summary")
        summary_layout = QVBoxLayout(summary_group)

        # SMU 1 status: Function, Mode, Source, Limit, Ramp
        smu1_group = QGroupBox("SMU 1")
        smu1_layout = QFormLayout(smu1_group)
        self.smu1_function_label = QLabel("—")
        self.smu1_mode_label = QLabel("—")
        self.smu1_source_label = QLabel("—")
        self.smu1_limit_label = QLabel("—")
        self.smu1_measure_label = QLabel("—")
        self.smu1_ramp_label = QLabel("—")
        smu1_layout.addRow("Function:", self.smu1_function_label)
        smu1_layout.addRow("Mode:", self.smu1_mode_label)
        smu1_layout.addRow("Source:", self.smu1_source_label)
        smu1_layout.addRow("Limit:", self.smu1_limit_label)
        smu1_layout.addRow("Measure:", self.smu1_measure_label)
        smu1_layout.addRow("Ramp:", self.smu1_ramp_label)
        summary_layout.addWidget(smu1_group)

        # SMU 2 status: Function, Mode, Source, Limit, Ramp
        smu2_group = QGroupBox("SMU 2")
        smu2_layout = QFormLayout(smu2_group)
        self.smu2_function_label = QLabel("—")
        self.smu2_mode_label = QLabel("—")
        self.smu2_source_label = QLabel("—")
        self.smu2_limit_label = QLabel("—")
        self.smu2_measure_label = QLabel("—")
        self.smu2_ramp_label = QLabel("—")
        smu2_layout.addRow("Function:", self.smu2_function_label)
        smu2_layout.addRow("Mode:", self.smu2_mode_label)
        smu2_layout.addRow("Source:", self.smu2_source_label)
        smu2_layout.addRow("Limit:", self.smu2_limit_label)
        smu2_layout.addRow("Measure:", self.smu2_measure_label)
        smu2_layout.addRow("Ramp:", self.smu2_ramp_label)
        summary_layout.addWidget(smu2_group)

        summary_layout.addStretch()
        lower_layout.addWidget(summary_group, 1)

        # --- Middle: Channel Settings (StackedWidget: one form per SMU) ---
        channel_group = QGroupBox("Channel Settings")
        channel_layout = QVBoxLayout(channel_group)

        self.smu_selector = QComboBox()
        self.smu_selector.addItems(["Configure SMU 1", "Configure SMU 2"])
        channel_layout.addWidget(self.smu_selector)

        self.channel_stacked = QStackedWidget()
        self.channel_stacked.addWidget(self._build_smu_form_page(1))
        self.channel_stacked.addWidget(self._build_smu_form_page(2))
        self.smu_selector.currentIndexChanged.connect(self.channel_stacked.setCurrentIndex)
        channel_layout.addWidget(self.channel_stacked)
        lower_layout.addWidget(channel_group, 1)

        # --- Right: Common Settings ---
        common_group = QGroupBox("Common Settings")
        common_layout = QFormLayout(common_group)

        self.nplc_spin = QDoubleSpinBox()
        self.nplc_spin.setRange(0.01, 25.0)
        self.nplc_spin.setValue(1.0)
        self.nplc_spin.setDecimals(2)
        common_layout.addRow("NPLC:", self.nplc_spin)
        self.nplc_spin.valueChanged.connect(self.emit_config_changed)

        self.measure_window_label = QLabel("20.0 ms")
        common_layout.addRow("Measure Window:", self.measure_window_label)

        self.src_meas_delay_spin = QDoubleSpinBox()
        self.src_meas_delay_spin.setRange(0.0, 3600.0)
        self.src_meas_delay_spin.setValue(0.0)
        self.src_meas_delay_spin.setSuffix(" s")
        self.src_meas_delay_spin.setDecimals(2)
        self.src_meas_delay_spin.setSingleStep(0.01)
        common_layout.addRow("Src to Meas Delay:", self.src_meas_delay_spin)
        self.src_meas_delay_spin.valueChanged.connect(self.emit_config_changed)

        self.step_sweep_delay_spin = QDoubleSpinBox()
        self.step_sweep_delay_spin.setRange(0.0, 3600.0)
        self.step_sweep_delay_spin.setValue(0.0)
        self.step_sweep_delay_spin.setSuffix(" s")
        self.step_sweep_delay_spin.setDecimals(2)
        self.step_sweep_delay_spin.setSingleStep(0.01)
        common_layout.addRow("Step to Sweep Delay:", self.step_sweep_delay_spin)
        self.step_sweep_delay_spin.valueChanged.connect(self.emit_config_changed)

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

        lower_layout.addWidget(common_group, 1)

        layout.addLayout(lower_layout)
        layout.addStretch(1)
        self._connect_smu_form_signals()

    def _build_smu_form_page(self, smu_index: int) -> QWidget:
        """Build one Channel Settings form page for the given SMU (1 or 2)."""
        page = QWidget()
        form = QFormLayout(page)

        function_combo = QComboBox()
        function_combo.addItems(["Voltage", "Current"])
        form.addRow("Function:", function_combo)

        mode_combo = QComboBox()
        mode_combo.addItems(["Fixed", "Sweep"])
        form.addRow("Mode:", mode_combo)

        dual_sweep_check = QCheckBox("Dual Sweep")
        form.addRow("", dual_sweep_check)

        # Ramp Up / Ramp Down (visible only when Function is Voltage)
        ramp_container = QWidget()
        ramp_container_layout = QVBoxLayout(ramp_container)
        ramp_container_layout.setContentsMargins(0, 0, 0, 0)

        ramp_up_check = QCheckBox("Ramp Up from 0V")
        ramp_container_layout.addWidget(ramp_up_check)
        ramp_up_step = QDoubleSpinBox()
        ramp_up_step.setRange(0.01, 1000.0)
        ramp_up_step.setValue(0.5)
        ramp_up_step.setSuffix(" V")
        ramp_up_step.setDecimals(3)
        ramp_up_delay = QDoubleSpinBox()
        ramp_up_delay.setRange(0.0, 3600.0)
        ramp_up_delay.setValue(1.0)
        ramp_up_delay.setSuffix(" s")
        ramp_up_delay.setDecimals(2)
        ramp_up_row = QWidget()
        ramp_up_hbox = QHBoxLayout(ramp_up_row)
        ramp_up_hbox.setContentsMargins(0, 0, 0, 0)
        ramp_up_hbox.addWidget(ramp_up_step)
        ramp_up_hbox.addWidget(ramp_up_delay)
        ramp_container_layout.addWidget(ramp_up_row)

        ramp_down_check = QCheckBox("Ramp Down to 0V")
        ramp_container_layout.addWidget(ramp_down_check)
        ramp_down_step = QDoubleSpinBox()
        ramp_down_step.setRange(0.01, 1000.0)
        ramp_down_step.setValue(0.5)
        ramp_down_step.setSuffix(" V")
        ramp_down_step.setDecimals(3)
        ramp_down_delay = QDoubleSpinBox()
        ramp_down_delay.setRange(0.0, 3600.0)
        ramp_down_delay.setValue(1.0)
        ramp_down_delay.setSuffix(" s")
        ramp_down_delay.setDecimals(2)
        ramp_down_row = QWidget()
        ramp_down_hbox = QHBoxLayout(ramp_down_row)
        ramp_down_hbox.setContentsMargins(0, 0, 0, 0)
        ramp_down_hbox.addWidget(ramp_down_step)
        ramp_down_hbox.addWidget(ramp_down_delay)
        ramp_container_layout.addWidget(ramp_down_row)

        form.addRow("Ramp:", ramp_container)

        level_spin = QDoubleSpinBox()
        level_spin.setRange(-1e3, 1e3)
        level_spin.setDecimals(4)
        form.addRow("Bias Level:", level_spin)

        start_spin = QDoubleSpinBox()
        start_spin.setRange(-1e3, 1e3)
        start_spin.setDecimals(4)
        form.addRow("Start:", start_spin)

        stop_spin = QDoubleSpinBox()
        stop_spin.setRange(-1e3, 1e3)
        stop_spin.setDecimals(4)
        form.addRow("Stop:", stop_spin)

        step_display = QLabel("0.000 V")
        step_display.setStyleSheet("color: white; padding: 2px;")
        form.addRow("Step:", step_display)

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
            getattr(self, f"limit_spin_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"measure_popup_smu{i}").selection_changed.connect(self.emit_config_changed)
            getattr(self, f"function_combo_smu{i}").currentTextChanged.connect(self._refresh_dynamic_ui_state)
            getattr(self, f"mode_combo_smu{i}").currentTextChanged.connect(self._refresh_dynamic_ui_state)
            getattr(self, f"ramp_up_check_smu{i}").stateChanged.connect(self._refresh_dynamic_ui_state)
            getattr(self, f"ramp_down_check_smu{i}").stateChanged.connect(self._refresh_dynamic_ui_state)

        self.stepper_selector.currentTextChanged.connect(self._refresh_dynamic_ui_state)
        self._refresh_dynamic_ui_state()

    def _build_graph_tab(self) -> None:
        layout = QVBoxLayout(self.graph_tab)

        # Toolbar
        graph_toolbar = QHBoxLayout()
        self.clear_plot_btn = QPushButton("Clear Plot")
        self.autoscale_btn = QPushButton("Autoscale")
        self.export_image_btn = QPushButton("Export to Image")
        self.graph_linear_btn = QPushButton("Linear")
        self.graph_log_btn = QPushButton("Log |I|")
        self.graph_linear_btn.setCheckable(True)
        self.graph_log_btn.setCheckable(True)
        self.graph_linear_btn.setChecked(True)
        graph_toolbar.addWidget(self.clear_plot_btn)
        graph_toolbar.addWidget(self.autoscale_btn)
        graph_toolbar.addWidget(self.export_image_btn)
        graph_toolbar.addWidget(self.graph_linear_btn)
        graph_toolbar.addWidget(self.graph_log_btn)
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

        self.export_csv_btn = QPushButton("Export to CSV")
        self.export_csv_btn.setMinimumHeight(36)
        layout.addWidget(self.export_csv_btn)
