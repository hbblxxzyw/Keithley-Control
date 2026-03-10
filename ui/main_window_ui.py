"""
Main window UI with top-level 3-tab architecture: Settings, Graph, Table.

Industrial-style layout: configuration and preview in Settings; full-screen
Graph and Table tabs for measurement data.
"""

from PySide6.QtCore import Signal
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
)
from PySide6.QtGui import QFont

from ui.graph_widget import PreviewGraphWidget, MeasurementGraphWidget


class MainWindowUI(QMainWindow):
    """
    Main application window with a top-level QTabWidget:
    Tab 1 Settings (config + preview), Tab 2 Graph (I-V curves), Tab 3 Table (data).
    """

    # Emitted when any Channel Settings form widget (SMU 1 or SMU 2) changes
    channel_config_changed = Signal()

    def emit_config_changed(self, *args: object) -> None:
        """统一的槽函数，用于转发所有表单变动"""
        self.channel_config_changed.emit()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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

    def _build_settings_tab(self) -> None:
        layout = QVBoxLayout(self.settings_tab)

        # ---------- Connection row ----------
        conn_layout = QHBoxLayout()
        conn_layout.addWidget(QLabel("Address:"))
        self.resource_address_edit = QLineEdit()
        self.resource_address_edit.setPlaceholderText("e.g. TCPIP0::192.168.1.100::inst0::INSTR")
        self.resource_address_edit.setText("TCPIP0::192.168.1.100::inst0::INSTR")
        conn_layout.addWidget(self.resource_address_edit)
        self.connect_btn = QPushButton("Connect")
        conn_layout.addWidget(self.connect_btn)
        self.connection_status_label = QLabel("Disconnected")
        self.connection_status_label.setStyleSheet("color: gray; font-weight: bold;")
        conn_layout.addWidget(self.connection_status_label)
        conn_layout.addStretch()
        layout.addLayout(conn_layout)

        # ---------- Upper: Output Preview ----------
        preview_group = QGroupBox("Output Preview")
        preview_layout = QVBoxLayout(preview_group)

        # Toolbar: Time Resolution
        preview_toolbar = QHBoxLayout()
        preview_toolbar.addWidget(QLabel("Time Resolution:"))
        self.time_resolution_spin = QDoubleSpinBox()
        self.time_resolution_spin.setRange(0.001, 1000.0)
        self.time_resolution_spin.setValue(0.01)
        self.time_resolution_spin.setSuffix(" s")
        self.time_resolution_spin.setDecimals(4)
        preview_toolbar.addWidget(self.time_resolution_spin)
        self.time_resolution_spin.valueChanged.connect(self.emit_config_changed)
        preview_toolbar.addStretch()
        preview_layout.addLayout(preview_toolbar)

        # Preview waveform (Settings tab)
        self.preview_plot_placeholder = PreviewGraphWidget()
        self.preview_plot_placeholder.setMinimumHeight(200)
        preview_layout.addWidget(self.preview_plot_placeholder)

        layout.addWidget(preview_group)

        # ---------- Lower: Three columns (Device Summary | Channel Settings | Common Settings) ----------
        lower_layout = QHBoxLayout()

        # --- Left: Device Summary ---
        summary_group = QGroupBox("Device Summary")
        summary_layout = QVBoxLayout(summary_group)

        # SMU 1 status: Mode, Source, Limit
        smu1_group = QGroupBox("SMU 1")
        smu1_layout = QFormLayout(smu1_group)
        self.smu1_mode_label = QLabel("—")
        self.smu1_source_label = QLabel("—")
        self.smu1_points_label = QLabel("—")
        self.smu1_limit_label = QLabel("—")
        smu1_layout.addRow("Mode:", self.smu1_mode_label)
        smu1_layout.addRow("", self.smu1_source_label)
        smu1_layout.addRow("Points:", self.smu1_points_label)
        smu1_layout.addRow("Limit:", self.smu1_limit_label)
        summary_layout.addWidget(smu1_group)

        # SMU 2 status: Mode, Source, Limit
        smu2_group = QGroupBox("SMU 2")
        smu2_layout = QFormLayout(smu2_group)
        self.smu2_mode_label = QLabel("—")
        self.smu2_source_label = QLabel("—")
        self.smu2_points_label = QLabel("—")
        self.smu2_limit_label = QLabel("—")
        smu2_layout.addRow("Mode:", self.smu2_mode_label)
        smu2_layout.addRow("", self.smu2_source_label)
        smu2_layout.addRow("Points:", self.smu2_points_label)
        smu2_layout.addRow("Limit:", self.smu2_limit_label)
        summary_layout.addWidget(smu2_group)

        summary_layout.addStretch()
        lower_layout.addWidget(summary_group)

        # --- Middle: Channel Settings (StackedWidget: one form per SMU) ---
        channel_group = QGroupBox("Channel Settings")
        channel_group.setMaximumWidth(320)
        channel_layout = QVBoxLayout(channel_group)

        self.smu_selector = QComboBox()
        self.smu_selector.addItems(["Configure SMU 1", "Configure SMU 2"])
        channel_layout.addWidget(self.smu_selector)

        self.channel_stacked = QStackedWidget()
        self.channel_stacked.addWidget(self._build_smu_form_page(1))
        self.channel_stacked.addWidget(self._build_smu_form_page(2))
        self.smu_selector.currentIndexChanged.connect(self.channel_stacked.setCurrentIndex)
        self._connect_smu_form_signals()
        channel_layout.addWidget(self.channel_stacked)
        lower_layout.addWidget(channel_group)

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
        self.stepper_selector.addItems(["SMU 2", "SMU 1"])
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
        )
        common_layout.addRow(self.run_btn)

        self.abort_btn = QPushButton("Abort")
        self.abort_btn.setMinimumHeight(48)
        self.abort_btn.setFont(font)
        self.abort_btn.setStyleSheet(
            "QPushButton { background-color: #b71c1c; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #d32f2f; }"
            "QPushButton:pressed { background-color: #8b0000; }"
        )
        common_layout.addRow(self.abort_btn)

        lower_layout.addWidget(common_group)

        layout.addLayout(lower_layout)

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
        step_display.setStyleSheet("background-color: #e0e0e0; color: #333; padding: 2px;")
        form.addRow("Step:", step_display)

        limit_spin = QDoubleSpinBox()
        limit_spin.setRange(1e-9, 10.0)
        limit_spin.setDecimals(4)
        limit_spin.setSuffix(" A")
        form.addRow("Limit:", limit_spin)

        setattr(self, f"function_combo_smu{smu_index}", function_combo)
        setattr(self, f"mode_combo_smu{smu_index}", mode_combo)
        setattr(self, f"dual_sweep_check_smu{smu_index}", dual_sweep_check)
        setattr(self, f"level_spin_smu{smu_index}", level_spin)
        setattr(self, f"start_spin_smu{smu_index}", start_spin)
        setattr(self, f"stop_spin_smu{smu_index}", stop_spin)
        setattr(self, f"step_display_smu{smu_index}", step_display)
        setattr(self, f"limit_spin_smu{smu_index}", limit_spin)
        return page

    def _connect_smu_form_signals(self) -> None:
        """Connect all Channel Settings form widgets to emit_config_changed (after both SMU pages exist)."""
        for i in (1, 2):
            getattr(self, f"function_combo_smu{i}").currentTextChanged.connect(self.emit_config_changed)
            getattr(self, f"function_combo_smu{i}").currentIndexChanged.connect(self.emit_config_changed)
            getattr(self, f"mode_combo_smu{i}").currentTextChanged.connect(self.emit_config_changed)
            getattr(self, f"mode_combo_smu{i}").currentIndexChanged.connect(self.emit_config_changed)
            getattr(self, f"dual_sweep_check_smu{i}").stateChanged.connect(self.emit_config_changed)
            getattr(self, f"level_spin_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"start_spin_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"stop_spin_smu{i}").valueChanged.connect(self.emit_config_changed)
            getattr(self, f"limit_spin_smu{i}").valueChanged.connect(self.emit_config_changed)

    def _build_graph_tab(self) -> None:
        layout = QVBoxLayout(self.graph_tab)

        # Toolbar
        graph_toolbar = QHBoxLayout()
        self.clear_plot_btn = QPushButton("Clear Plot")
        self.autoscale_btn = QPushButton("Autoscale")
        self.export_image_btn = QPushButton("Export to Image")
        graph_toolbar.addWidget(self.clear_plot_btn)
        graph_toolbar.addWidget(self.autoscale_btn)
        graph_toolbar.addWidget(self.export_image_btn)
        graph_toolbar.addStretch()
        layout.addLayout(graph_toolbar)

        # I-V measurement plot (Graph tab)
        self.graph_plot_placeholder = MeasurementGraphWidget()
        self.graph_plot_placeholder.setMinimumHeight(400)
        layout.addWidget(self.graph_plot_placeholder)

    def _build_table_tab(self) -> None:
        layout = QVBoxLayout(self.table_tab)

        self.data_table = QTableWidget()
        self.data_table.setColumnCount(5)
        self.data_table.setHorizontalHeaderLabels([
            "Time (s)",
            "SMU 1 V",
            "SMU 1 I",
            "SMU 2 V",
            "SMU 2 I",
        ])
        self.data_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.data_table)

        self.export_csv_btn = QPushButton("Export to CSV")
        self.export_csv_btn.setMinimumHeight(36)
        layout.addWidget(self.export_csv_btn)
