"""
Pulse configuration dialog.

The dialog stores a sequence of pulse combinations. Each combination owns a
sequence of pulses; pulse train items own a nested sequence of single/paired
pulses.
"""

from copy import deepcopy
from typing import Any, Callable

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.graph_widget import PlainAxisItem
from ui.numeric_spinbox import AdaptiveDelaySpinBox, ScientificDoubleSpinBox


PULSE_TYPE_LABELS = {
    "single": "Single",
    "paired": "Paired",
    "train": "Pulse Train",
}
PULSE_LABEL_TYPES = {value: key for key, value in PULSE_TYPE_LABELS.items()}
SEQUENCE_BUTTON_STYLE = """
QPushButton {
    padding: 4px 10px;
}
QPushButton:checked {
    background-color: #2f5f8f;
    color: white;
    border: 1px solid #24486d;
    font-weight: 600;
}
"""
SEQUENCE_LIST_STYLE = """
QListWidget {
    border: 1px solid #d0d7de;
    background: #ffffff;
}
QListWidget::item {
    padding: 3px 6px;
    margin: 2px;
    border: 1px solid #c9d1d9;
    background: #f6f8fa;
}
QListWidget::item:selected {
    background-color: #2f5f8f;
    color: white;
    border: 1px solid #24486d;
    font-weight: 600;
}
"""


def _install_enter_commit_filter(owner: QWidget, widgets: list[QWidget]) -> None:
    """Let Enter commit field edits without accepting the parent dialog."""
    commit_widgets = getattr(owner, "_enter_commit_widgets", None)
    if commit_widgets is None:
        commit_widgets = {}
        setattr(owner, "_enter_commit_widgets", commit_widgets)
    for widget in widgets:
        widget.installEventFilter(owner)
        commit_widgets[widget] = widget
        if isinstance(widget, QAbstractSpinBox):
            line_edit = widget.lineEdit()
            line_edit.installEventFilter(owner)
            commit_widgets[line_edit] = widget


def _handle_enter_commit(owner: QWidget, watched: object, event: QEvent) -> bool:
    if event.type() != QEvent.KeyPress:
        return False
    if event.key() not in (Qt.Key_Return, Qt.Key_Enter):  # type: ignore[attr-defined]
        return False

    commit_widget = getattr(owner, "_enter_commit_widgets", {}).get(watched)
    if commit_widget is None:
        return False
    if isinstance(commit_widget, QAbstractSpinBox):
        commit_widget.interpretText()
    commit_widget.clearFocus()
    return True


def default_pulse_config() -> dict[str, Any]:
    return {"combinations": [], "selected_index": -1}


def default_single_pulse() -> dict[str, Any]:
    return {
        "type": "single",
        "magnitude": 0.0,
        "duration_s": 0.001,
        "interval_after_s": 0.0,
    }


def default_paired_pulse() -> dict[str, Any]:
    return {
        "type": "paired",
        "magnitude": 0.0,
        "duration_s": 0.001,
        "interval_s": 0.001,
        "interval_after_s": 0.0,
    }


def default_pulse_train() -> dict[str, Any]:
    return {
        "type": "train",
        "items": [],
        "selected_index": -1,
        "interval_after_s": 0.0,
    }


def normalize_pulse_item(
    item: dict[str, Any] | None,
    *,
    allow_train: bool = True,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        return default_single_pulse()

    pulse_type = str(item.get("type", "single")).strip().lower()
    if pulse_type == "paired":
        normalized = default_paired_pulse()
        normalized["magnitude"] = float(item.get("magnitude", 0.0))
        normalized["duration_s"] = float(item.get("duration_s", 0.001))
        normalized["interval_s"] = float(item.get("interval_s", 0.001))
        normalized["interval_after_s"] = float(item.get("interval_after_s", 0.0))
        return normalized

    if pulse_type == "train" and allow_train:
        nested_items = [
            normalize_pulse_item(child, allow_train=False)
            for child in item.get("items", [])
            if isinstance(child, dict)
        ]
        selected_index = int(item.get("selected_index", -1))
        if not nested_items:
            selected_index = -1
        else:
            selected_index = max(0, min(selected_index, len(nested_items) - 1))
        return {
            "type": "train",
            "items": nested_items,
            "selected_index": selected_index,
            "interval_after_s": float(item.get("interval_after_s", 0.0)),
        }

    normalized = default_single_pulse()
    normalized["magnitude"] = float(item.get("magnitude", 0.0))
    normalized["duration_s"] = float(item.get("duration_s", 0.001))
    normalized["interval_after_s"] = float(item.get("interval_after_s", 0.0))
    return normalized


def normalize_pulse_items(
    items: list[Any] | None,
    *,
    allow_train: bool = True,
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [
        normalize_pulse_item(item, allow_train=allow_train)
        for item in items
        if isinstance(item, dict)
    ]


def normalize_pulse_config(config: dict[str, Any] | None) -> dict[str, Any]:
    normalized = default_pulse_config()
    if not isinstance(config, dict):
        return normalized

    combinations: list[dict[str, Any]] = []
    for index, item in enumerate(config.get("combinations", [])):
        if not isinstance(item, dict):
            continue
        combinations.append(
            {
                "name": str(item.get("name") or f"Combination {index + 1}"),
                "interval_after_s": float(item.get("interval_after_s", 0.0)),
                "repeat": max(1, int(item.get("repeat", 1))),
                "items": normalize_pulse_items(item.get("items"), allow_train=True),
            }
        )

    selected_index = int(config.get("selected_index", -1))
    if not combinations:
        selected_index = -1
    else:
        selected_index = max(0, min(selected_index, len(combinations) - 1))

    normalized["combinations"] = combinations
    normalized["selected_index"] = selected_index
    return normalized


def _append_level_segment(
    times: list[float],
    values: list[float],
    current_time: float,
    level: float,
    duration_s: float,
) -> float:
    duration_s = max(0.0, float(duration_s))
    if not times:
        times.append(current_time)
        values.append(0.0)
    if values[-1] != level:
        times.append(current_time)
        values.append(level)
    current_time += duration_s
    times.append(current_time)
    values.append(level)
    return current_time


def build_pulse_waveform(items: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    times: list[float] = [0.0]
    values: list[float] = [0.0]

    def append_items(pulse_items: list[dict[str, Any]], start_time: float) -> float:
        current_time = start_time
        for pulse in normalize_pulse_items(pulse_items, allow_train=True):
            pulse_type = str(pulse.get("type", "single")).lower()
            if pulse_type == "train":
                current_time = append_items(
                    normalize_pulse_items(pulse.get("items", []), allow_train=False),
                    current_time,
                )
                current_time = _append_level_segment(
                    times,
                    values,
                    current_time,
                    0.0,
                    float(pulse.get("interval_after_s", 0.0)),
                )
                continue

            magnitude = float(pulse.get("magnitude", 0.0))
            duration_s = float(pulse.get("duration_s", 0.0))
            current_time = _append_level_segment(
                times, values, current_time, magnitude, duration_s
            )
            if values[-1] != 0.0:
                times.append(current_time)
                values.append(0.0)

            if pulse_type == "paired":
                interval_s = float(pulse.get("interval_s", 0.0))
                current_time = _append_level_segment(
                    times, values, current_time, 0.0, interval_s
                )
                current_time = _append_level_segment(
                    times, values, current_time, magnitude, duration_s
                )
                if values[-1] != 0.0:
                    times.append(current_time)
                    values.append(0.0)

            current_time = _append_level_segment(
                times,
                values,
                current_time,
                0.0,
                float(pulse.get("interval_after_s", 0.0)),
            )

        return current_time

    append_items(items, 0.0)
    if len(times) == 1:
        times.append(1.0)
        values.append(0.0)
    return times, values


def build_combination_waveform(
    combination: dict[str, Any] | None,
) -> tuple[list[float], list[float]]:
    if not isinstance(combination, dict):
        return [0.0, 1.0], [0.0, 0.0]

    times: list[float] = [0.0]
    values: list[float] = [0.0]
    repeat = max(1, int(combination.get("repeat", 1)))
    interval_after_s = max(0.0, float(combination.get("interval_after_s", 0.0)))
    current_time = 0.0

    for _ in range(repeat):
        normalized_items = normalize_pulse_items(
            combination.get("items", []),
            allow_train=True,
        )
        if normalized_items:
            item_times, item_values = build_pulse_waveform(normalized_items)
            if item_times and item_times[-1] > 0.0:
                for time_value, level_value in zip(item_times[1:], item_values[1:]):
                    times.append(current_time + float(time_value))
                    values.append(float(level_value))
                current_time += float(item_times[-1])
        current_time = _append_level_segment(
            times,
            values,
            current_time,
            0.0,
            interval_after_s,
        )

    if current_time <= 0.0:
        times.append(1.0)
        values.append(0.0)
    return times, values


def combination_duration_s(combination: dict[str, Any] | None) -> float:
    if not isinstance(combination, dict):
        return 0.0
    times, _ = build_combination_waveform(combination)
    if not times or times == [0.0, 1.0]:
        return 0.0
    return max(times)


class PulseWaveformPreview(pg.PlotWidget):
    """Single-combination pulse waveform preview."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            parent=parent,
            axisItems={
                "bottom": PlainAxisItem("bottom", suffix="s"),
                "left": PlainAxisItem("left"),
            },
        )
        self.setBackground("w")
        self.setMinimumHeight(140)
        self.setMaximumHeight(180)
        self.showGrid(x=True, y=True, alpha=0.12)
        self.showAxis("top", False)
        self.showAxis("right", False)
        self.getAxis("left").setTextPen("#5f6368")
        self.getAxis("bottom").setTextPen("#5f6368")
        self.getAxis("left").setPen("#d0d7de")
        self.getAxis("bottom").setPen("#d0d7de")
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=False, y=False)
        self.getViewBox().setDefaultPadding(0.04)
        self.line = self.plot(pen=pg.mkPen("#c0392b", width=2))
        self.update_waveform([])

    def update_waveform(self, combination: dict[str, Any] | None) -> None:
        times, values = build_combination_waveform(combination)
        self.line.setData(times, values)
        max_time = max(times) if times else 1.0
        if max_time <= 0.0:
            max_time = 1.0
        self.setXRange(0.0, max_time, padding=0.02)
        min_value = min(values) if values else 0.0
        max_value = max(values) if values else 0.0
        if min_value == max_value:
            pad = max(1.0, abs(max_value) * 0.2)
            min_value -= pad
            max_value += pad
        else:
            pad = (max_value - min_value) * 0.15
            min_value -= pad
            max_value += pad
        self.setYRange(min_value, max_value, padding=0.0)


class PulseSequenceEditor(QWidget):
    """Horizontal sequence editor for pulses or pulse-train children."""

    def __init__(
        self,
        items: list[Any] | None = None,
        *,
        allow_train: bool = True,
        title: str = "Pulses",
        on_changed: Callable[[list[dict[str, Any]]], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.allow_train = allow_train
        self._items = normalize_pulse_items(items, allow_train=allow_train)
        self._selected_index = 0 if self._items else -1
        self._buttons: list[QPushButton] = []
        self._syncing_editor = False
        self._syncing_sequence = False
        self._reorder_update_pending = False
        self._on_changed = on_changed
        self._train_editor: PulseSequenceEditor | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        sequence_group = QGroupBox(title)
        sequence_group.setFixedHeight(112)
        sequence_layout = QHBoxLayout(sequence_group)

        self.sequence_list = QListWidget()
        self.sequence_list.setViewMode(QListView.ListMode)
        self.sequence_list.setFlow(QListView.LeftToRight)
        self.sequence_list.setWrapping(True)
        self.sequence_list.setResizeMode(QListView.Adjust)
        self.sequence_list.setMovement(QListView.Static)
        self.sequence_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.sequence_list.setDragDropOverwriteMode(False)
        self.sequence_list.setDropIndicatorShown(True)
        self.sequence_list.setDefaultDropAction(Qt.MoveAction)
        self.sequence_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.sequence_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sequence_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.sequence_list.setUniformItemSizes(True)
        self.sequence_list.setGridSize(QSize(86 if allow_train else 74, 30))
        self.sequence_list.setFixedHeight(72)
        self.sequence_list.setStyleSheet(SEQUENCE_LIST_STYLE)
        self.sequence_list.currentRowChanged.connect(self._select_pulse)
        self.sequence_list.model().rowsMoved.connect(self._schedule_sequence_reorder)
        self.sequence_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.sequence_list.customContextMenuRequested.connect(
            self._show_pulse_context_menu
        )
        sequence_layout.addWidget(self.sequence_list, 1)

        self.add_button = QPushButton("+")
        self.add_button.setFixedWidth(44)
        self.add_button.setFixedHeight(72)
        self.add_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.add_button.clicked.connect(self._add_pulse)
        sequence_layout.addWidget(self.add_button)
        root.addWidget(sequence_group)

        self.editor_group = QGroupBox("Pulse")
        editor_layout = QVBoxLayout(self.editor_group)
        form = QFormLayout()

        self.type_combo = QComboBox()
        labels = ["Single", "Paired"]
        if allow_train:
            labels.append("Pulse Train")
        self.type_combo.addItems(labels)
        self.type_combo.currentTextChanged.connect(self._handle_type_changed)
        form.addRow("Type:", self.type_combo)

        self.magnitude_label = QLabel("Magnitude:")
        self.magnitude_spin = ScientificDoubleSpinBox()
        self.magnitude_spin.setRange(-1e6, 1e6)
        self.magnitude_spin.setDecimals(9)
        self.magnitude_spin.setSingleStep(0.1)
        self.magnitude_spin.valueChanged.connect(self._handle_magnitude_changed)
        form.addRow(self.magnitude_label, self.magnitude_spin)

        self.duration_label = QLabel("Duration:")
        self.duration_spin = AdaptiveDelaySpinBox()
        self.duration_spin.setRange(0.0, 3600.0)
        self.duration_spin.setDecimals(9)
        self.duration_spin.setSingleStep(0.001)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.valueChanged.connect(self._handle_duration_changed)
        form.addRow(self.duration_label, self.duration_spin)

        self.interval_label = QLabel("Interval:")
        self.interval_spin = AdaptiveDelaySpinBox()
        self.interval_spin.setRange(0.0, 3600.0)
        self.interval_spin.setDecimals(9)
        self.interval_spin.setSingleStep(0.001)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.valueChanged.connect(self._handle_interval_changed)
        form.addRow(self.interval_label, self.interval_spin)

        self.interval_after_label = QLabel("Interval After:")
        self.interval_after_spin = AdaptiveDelaySpinBox()
        self.interval_after_spin.setRange(0.0, 3600.0)
        self.interval_after_spin.setDecimals(9)
        self.interval_after_spin.setSingleStep(0.001)
        self.interval_after_spin.setSuffix(" s")
        self.interval_after_spin.valueChanged.connect(
            self._handle_interval_after_changed
        )
        form.addRow(self.interval_after_label, self.interval_after_spin)

        editor_layout.addLayout(form)
        self.train_container = QWidget()
        self.train_layout = QVBoxLayout(self.train_container)
        self.train_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addWidget(self.train_container)
        root.addWidget(self.editor_group)

        self._rebuild_sequence()
        self._load_selected_pulse()
        _install_enter_commit_filter(
            self,
            [
                self.magnitude_spin,
                self.duration_spin,
                self.interval_spin,
                self.interval_after_spin,
            ],
        )

    def items(self) -> list[dict[str, Any]]:
        return normalize_pulse_items(deepcopy(self._items), allow_train=self.allow_train)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if _handle_enter_commit(self, watched, event):
            return True
        return super().eventFilter(watched, event)

    def set_items(self, items: list[Any] | None) -> None:
        self._items = normalize_pulse_items(items, allow_train=self.allow_train)
        self._selected_index = 0 if self._items else -1
        self._rebuild_sequence()
        self._load_selected_pulse()

    def _selected_pulse(self) -> dict[str, Any] | None:
        if self._selected_index < 0 or self._selected_index >= len(self._items):
            return None
        return self._items[self._selected_index]

    def _notify_changed(self) -> None:
        if self._on_changed is not None:
            self._on_changed(self.items())

    def _add_pulse(self) -> None:
        self._items.append(default_single_pulse())
        self._selected_index = len(self._items) - 1
        self._rebuild_sequence()
        self._load_selected_pulse()
        self._notify_changed()

    def _select_pulse(self, index: int) -> None:
        if self._syncing_sequence:
            return
        if index < 0 or index >= len(self._items):
            return
        self._selected_index = index
        self._load_selected_pulse()

    def _pulse_button_text(self, index: int, pulse: dict[str, Any]) -> str:
        pulse_type = str(pulse.get("type", "single")).lower()
        label = {
            "single": "Single",
            "paired": "Pair",
            "train": "Train",
        }.get(pulse_type, PULSE_TYPE_LABELS.get(pulse_type, "Single"))
        return f"{index + 1}. {label}"

    def _rebuild_sequence(self) -> None:
        self._syncing_sequence = True
        try:
            self.sequence_list.clear()
            for index, pulse in enumerate(self._items):
                item = QListWidgetItem(self._pulse_button_text(index, pulse))
                item.setData(Qt.UserRole, id(pulse))
                item.setSizeHint(QSize(86 if self.allow_train else 74, 30))
                self.sequence_list.addItem(item)
            if 0 <= self._selected_index < len(self._items):
                self.sequence_list.setCurrentRow(self._selected_index)
        finally:
            self._syncing_sequence = False

    def _show_pulse_context_menu(self, position) -> None:
        item = self.sequence_list.itemAt(position)
        if item is None:
            return
        index = self.sequence_list.row(item)
        menu = QMenu(self)
        duplicate_action = menu.addAction("Duplicate")
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.sequence_list.mapToGlobal(position))
        if action == duplicate_action:
            self._duplicate_pulse(index)
        elif action == delete_action:
            self._delete_pulse(index)

    def _duplicate_pulse(self, index: int) -> None:
        if index < 0 or index >= len(self._items):
            return
        self._items.insert(index + 1, deepcopy(self._items[index]))
        self._selected_index = index + 1
        self._rebuild_sequence()
        self._load_selected_pulse()
        self._notify_changed()

    def _delete_pulse(self, index: int) -> None:
        if index < 0 or index >= len(self._items):
            return
        del self._items[index]
        if not self._items:
            self._selected_index = -1
        else:
            self._selected_index = min(index, len(self._items) - 1)
        self._rebuild_sequence()
        self._load_selected_pulse()
        self._notify_changed()

    def _schedule_sequence_reorder(self, *args) -> None:
        if self._syncing_sequence or self._reorder_update_pending:
            return
        self._reorder_update_pending = True
        QTimer.singleShot(0, self._handle_sequence_reordered)

    def _handle_sequence_reordered(self) -> None:
        self._reorder_update_pending = False
        if self._syncing_sequence:
            return
        selected_uid = None
        current_item = self.sequence_list.currentItem()
        if current_item is not None:
            selected_uid = current_item.data(Qt.UserRole)
        items_by_uid = {id(item): item for item in self._items}
        reordered: list[dict[str, Any]] = []
        for row in range(self.sequence_list.count()):
            item_uid = self.sequence_list.item(row).data(Qt.UserRole)
            if isinstance(item_uid, int) and item_uid in items_by_uid:
                reordered.append(items_by_uid[item_uid])
        if len(reordered) != len(self._items):
            self._rebuild_sequence()
            return
        self._items = reordered
        if isinstance(selected_uid, int):
            self._selected_index = next(
                (
                    index
                    for index, item in enumerate(self._items)
                    if id(item) == selected_uid
                ),
                min(max(self.sequence_list.currentRow(), 0), len(self._items) - 1),
            )
        else:
            self._selected_index = min(max(self.sequence_list.currentRow(), 0), len(self._items) - 1)
        self._rebuild_sequence()
        self._load_selected_pulse()
        self._notify_changed()

    def _clear_train_editor(self) -> None:
        if self._train_editor is None:
            return
        self.train_layout.removeWidget(self._train_editor)
        self._train_editor.deleteLater()
        self._train_editor = None

    def _set_param_rows_visible(
        self,
        visible: bool,
        *,
        show_interval: bool,
        show_interval_after: bool = True,
    ) -> None:
        for widget in (
            self.magnitude_label,
            self.magnitude_spin,
            self.duration_label,
            self.duration_spin,
        ):
            widget.setVisible(visible)
        self.interval_label.setVisible(visible and show_interval)
        self.interval_spin.setVisible(visible and show_interval)
        self.interval_after_label.setVisible(show_interval_after)
        self.interval_after_spin.setVisible(show_interval_after)

    def _load_selected_pulse(self) -> None:
        pulse = self._selected_pulse()
        enabled = pulse is not None
        self.editor_group.setEnabled(enabled)
        self._syncing_editor = True
        try:
            if pulse is None:
                self.type_combo.setCurrentText("Single")
                self.magnitude_spin.setValue(0.0)
                self.duration_spin.setValue(0.001)
                self.interval_spin.setValue(0.001)
                self.interval_after_spin.setValue(0.0)
                self._set_param_rows_visible(
                    True,
                    show_interval=False,
                    show_interval_after=True,
                )
                self.train_container.setVisible(False)
                self._clear_train_editor()
                return

            pulse_type = str(pulse.get("type", "single")).lower()
            self.type_combo.setCurrentText(PULSE_TYPE_LABELS.get(pulse_type, "Single"))

            if pulse_type == "train" and self.allow_train:
                self._set_param_rows_visible(
                    False,
                    show_interval=False,
                    show_interval_after=True,
                )
                self.interval_after_spin.setValue(
                    float(pulse.get("interval_after_s", 0.0))
                )
                self.train_container.setVisible(True)
                self._clear_train_editor()
                self._train_editor = PulseSequenceEditor(
                    pulse.get("items", []),
                    allow_train=False,
                    title="Pulse Train",
                    on_changed=self._handle_train_items_changed,
                )
                self.train_layout.addWidget(self._train_editor)
            else:
                self.train_container.setVisible(False)
                self._clear_train_editor()
                self._set_param_rows_visible(
                    True,
                    show_interval=pulse_type == "paired",
                    show_interval_after=True,
                )
                self.magnitude_spin.setValue(float(pulse.get("magnitude", 0.0)))
                self.duration_spin.setValue(float(pulse.get("duration_s", 0.001)))
                self.interval_spin.setValue(float(pulse.get("interval_s", 0.001)))
                self.interval_after_spin.setValue(
                    float(pulse.get("interval_after_s", 0.0))
                )
        finally:
            self._syncing_editor = False

    def _handle_type_changed(self, label: str) -> None:
        if self._syncing_editor:
            return
        pulse = self._selected_pulse()
        if pulse is None:
            return

        pulse_type = PULSE_LABEL_TYPES.get(label, "single")
        magnitude = float(pulse.get("magnitude", self.magnitude_spin.value()))
        duration = float(pulse.get("duration_s", self.duration_spin.value()))
        interval = float(pulse.get("interval_s", self.interval_spin.value()))
        interval_after = float(
            pulse.get("interval_after_s", self.interval_after_spin.value())
        )

        if pulse_type == "paired":
            replacement = default_paired_pulse()
            replacement.update(
                {
                    "magnitude": magnitude,
                    "duration_s": duration,
                    "interval_s": interval,
                    "interval_after_s": interval_after,
                }
            )
        elif pulse_type == "train" and self.allow_train:
            replacement = default_pulse_train()
            replacement["interval_after_s"] = interval_after
            if str(pulse.get("type", "")).lower() == "train":
                replacement["items"] = normalize_pulse_items(
                    pulse.get("items"), allow_train=False
                )
        else:
            replacement = default_single_pulse()
            replacement.update(
                {
                    "magnitude": magnitude,
                    "duration_s": duration,
                    "interval_after_s": interval_after,
                }
            )

        self._items[self._selected_index] = replacement
        self._rebuild_sequence()
        self._load_selected_pulse()
        self._notify_changed()

    def _handle_magnitude_changed(self, value: float) -> None:
        if self._syncing_editor:
            return
        pulse = self._selected_pulse()
        if pulse is None:
            return
        pulse["magnitude"] = float(value)
        self._notify_changed()

    def _handle_duration_changed(self, value: float) -> None:
        if self._syncing_editor:
            return
        pulse = self._selected_pulse()
        if pulse is None:
            return
        pulse["duration_s"] = float(value)
        self._notify_changed()

    def _handle_interval_changed(self, value: float) -> None:
        if self._syncing_editor:
            return
        pulse = self._selected_pulse()
        if pulse is None:
            return
        pulse["interval_s"] = float(value)
        self._notify_changed()

    def _handle_interval_after_changed(self, value: float) -> None:
        if self._syncing_editor:
            return
        pulse = self._selected_pulse()
        if pulse is None:
            return
        pulse["interval_after_s"] = float(value)
        self._notify_changed()

    def _handle_train_items_changed(self, items: list[dict[str, Any]]) -> None:
        pulse = self._selected_pulse()
        if pulse is None or str(pulse.get("type", "")).lower() != "train":
            return
        pulse["items"] = normalize_pulse_items(items, allow_train=False)
        self._notify_changed()


class PulseConfigDialog(QDialog):
    """Edit a channel's pulse combination sequence."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        parent: QWidget | None = None,
        title: str = "Pulse Configuration",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 620)
        self._config = normalize_pulse_config(deepcopy(config))
        self._buttons: list[QPushButton] = []
        self._syncing_editor = False
        self._syncing_sequence = False
        self._reorder_update_pending = False

        root = QVBoxLayout(self)

        sequence_group = QGroupBox("Pulse Combinations")
        sequence_group.setFixedHeight(104)
        sequence_layout = QHBoxLayout(sequence_group)

        self.sequence_list = QListWidget()
        self.sequence_list.setViewMode(QListView.ListMode)
        self.sequence_list.setFlow(QListView.LeftToRight)
        self.sequence_list.setWrapping(False)
        self.sequence_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.sequence_list.setDefaultDropAction(Qt.MoveAction)
        self.sequence_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.sequence_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.sequence_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sequence_list.setFixedHeight(64)
        self.sequence_list.setStyleSheet(SEQUENCE_LIST_STYLE)
        self.sequence_list.currentRowChanged.connect(self._select_combination)
        self.sequence_list.model().rowsMoved.connect(
            self._schedule_combination_reorder
        )
        self.sequence_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.sequence_list.customContextMenuRequested.connect(
            self._show_combination_context_menu
        )
        sequence_layout.addWidget(self.sequence_list, 1)

        self.add_button = QPushButton("+")
        self.add_button.setFixedWidth(44)
        self.add_button.setFixedHeight(64)
        self.add_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.add_button.clicked.connect(self._add_combination)
        sequence_layout.addWidget(self.add_button)
        root.addWidget(sequence_group)

        self.editor_group = QGroupBox("Combination")
        editor_layout = QVBoxLayout(self.editor_group)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._handle_name_changed)
        form.addRow("Name:", self.name_edit)

        interval_repeat_row = QWidget()
        interval_repeat_layout = QHBoxLayout(interval_repeat_row)
        interval_repeat_layout.setContentsMargins(0, 0, 0, 0)
        self.interval_spin = AdaptiveDelaySpinBox()
        self.interval_spin.setRange(0.0, 3600.0)
        self.interval_spin.setDecimals(9)
        self.interval_spin.setSingleStep(0.001)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.valueChanged.connect(self._handle_interval_changed)
        interval_repeat_layout.addWidget(self.interval_spin, 1)
        interval_repeat_layout.addWidget(QLabel("Repeat:"))
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 1000000)
        self.repeat_spin.setValue(1)
        self.repeat_spin.valueChanged.connect(self._handle_repeat_changed)
        interval_repeat_layout.addWidget(self.repeat_spin)
        form.addRow("Interval After:", interval_repeat_row)
        editor_layout.addLayout(form)

        self.pulse_editor = PulseSequenceEditor(
            [],
            allow_train=True,
            title="Pulses",
            on_changed=self._handle_pulses_changed,
        )
        editor_layout.addWidget(self.pulse_editor, 1)

        preview_group = QGroupBox("Waveform Preview")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(6, 6, 6, 6)
        self.waveform_preview = PulseWaveformPreview()
        preview_layout.addWidget(self.waveform_preview)
        self.waveform_stats_label = QLabel("Total duration: 0 s")
        preview_layout.addWidget(self.waveform_stats_label)
        editor_layout.addWidget(preview_group)
        root.addWidget(self.editor_group, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._rebuild_sequence()
        self._load_selected_combination()
        _install_enter_commit_filter(
            self,
            [
                self.name_edit,
                self.interval_spin,
                self.repeat_spin,
            ],
        )

    def config(self) -> dict[str, Any]:
        return normalize_pulse_config(deepcopy(self._config))

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if _handle_enter_commit(self, watched, event):
            return True
        return super().eventFilter(watched, event)

    def _selected_index(self) -> int:
        return int(self._config.get("selected_index", -1))

    def _selected_combination(self) -> dict[str, Any] | None:
        index = self._selected_index()
        combinations = self._config["combinations"]
        if index < 0 or index >= len(combinations):
            return None
        return combinations[index]

    def _add_combination(self) -> None:
        combinations = self._config["combinations"]
        combinations.append(
            {
                "name": f"Combination {len(combinations) + 1}",
                "interval_after_s": 0.0,
                "repeat": 1,
                "items": [],
            }
        )
        self._config["selected_index"] = len(combinations) - 1
        self._rebuild_sequence()
        self._load_selected_combination()

    def _select_combination(self, index: int) -> None:
        if self._syncing_sequence:
            return
        if index < 0 or index >= len(self._config["combinations"]):
            return
        self._config["selected_index"] = index
        self._load_selected_combination()

    def _rebuild_sequence(self) -> None:
        self._syncing_sequence = True
        try:
            self.sequence_list.clear()
            selected_index = self._selected_index()
            for index, combo in enumerate(self._config["combinations"]):
                item = QListWidgetItem(self._combination_label(index, combo))
                item.setData(Qt.UserRole, id(combo))
                self.sequence_list.addItem(item)
            if 0 <= selected_index < len(self._config["combinations"]):
                self.sequence_list.setCurrentRow(selected_index)
        finally:
            self._syncing_sequence = False

    def _combination_label(self, index: int, combination: dict[str, Any]) -> str:
        name = str(combination.get("name") or f"Combination {index + 1}")
        repeat = max(1, int(combination.get("repeat", 1)))
        return name if repeat == 1 else f"{name} x{repeat}"

    def _show_combination_context_menu(self, position) -> None:
        item = self.sequence_list.itemAt(position)
        if item is None:
            return
        index = self.sequence_list.row(item)
        menu = QMenu(self)
        duplicate_action = menu.addAction("Duplicate")
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.sequence_list.mapToGlobal(position))
        if action == duplicate_action:
            self._duplicate_combination(index)
        elif action == delete_action:
            self._delete_combination(index)

    def _duplicate_combination(self, index: int) -> None:
        combinations = self._config["combinations"]
        if index < 0 or index >= len(combinations):
            return
        duplicated = deepcopy(combinations[index])
        duplicated["name"] = f"{duplicated.get('name') or f'Combination {index + 1}'} Copy"
        combinations.insert(index + 1, duplicated)
        self._config["selected_index"] = index + 1
        self._rebuild_sequence()
        self._load_selected_combination()

    def _delete_combination(self, index: int) -> None:
        combinations = self._config["combinations"]
        if index < 0 or index >= len(combinations):
            return
        del combinations[index]
        if not combinations:
            self._config["selected_index"] = -1
        else:
            self._config["selected_index"] = min(index, len(combinations) - 1)
        self._rebuild_sequence()
        self._load_selected_combination()

    def _schedule_combination_reorder(self, *args) -> None:
        if self._syncing_sequence or self._reorder_update_pending:
            return
        self._reorder_update_pending = True
        QTimer.singleShot(0, self._handle_combination_reordered)

    def _handle_combination_reordered(self) -> None:
        self._reorder_update_pending = False
        if self._syncing_sequence:
            return
        combinations = self._config["combinations"]
        selected_uid = None
        current_item = self.sequence_list.currentItem()
        if current_item is not None:
            selected_uid = current_item.data(Qt.UserRole)
        combinations_by_uid = {id(combination): combination for combination in combinations}
        reordered: list[dict[str, Any]] = []
        for row in range(self.sequence_list.count()):
            combination_uid = self.sequence_list.item(row).data(Qt.UserRole)
            if isinstance(combination_uid, int) and combination_uid in combinations_by_uid:
                reordered.append(combinations_by_uid[combination_uid])
        if len(reordered) != len(combinations):
            self._rebuild_sequence()
            return
        self._config["combinations"] = reordered
        if isinstance(selected_uid, int):
            self._config["selected_index"] = next(
                (
                    index
                    for index, combination in enumerate(reordered)
                    if id(combination) == selected_uid
                ),
                min(max(self.sequence_list.currentRow(), 0), len(reordered) - 1),
            )
        else:
            self._config["selected_index"] = min(
                max(self.sequence_list.currentRow(), 0),
                len(reordered) - 1,
            )
        self._rebuild_sequence()
        self._load_selected_combination()

    def _load_selected_combination(self) -> None:
        combination = self._selected_combination()
        enabled = combination is not None
        self.editor_group.setEnabled(enabled)
        self._syncing_editor = True
        try:
            if combination is None:
                self.name_edit.clear()
                self.interval_spin.setValue(0.0)
                self.repeat_spin.setValue(1)
                self.pulse_editor.set_items([])
                self.waveform_preview.update_waveform(None)
                self._refresh_waveform_stats(None)
            else:
                self.name_edit.setText(str(combination.get("name") or ""))
                self.interval_spin.setValue(float(combination.get("interval_after_s", 0.0)))
                self.repeat_spin.setValue(max(1, int(combination.get("repeat", 1))))
                self.pulse_editor.set_items(combination.get("items", []))
                self.waveform_preview.update_waveform(combination)
                self._refresh_waveform_stats(combination)
        finally:
            self._syncing_editor = False

    def _refresh_selected_combination_label(self) -> None:
        index = self._selected_index()
        combination = self._selected_combination()
        if combination is None or index < 0 or index >= self.sequence_list.count():
            return
        self.sequence_list.item(index).setText(self._combination_label(index, combination))

    def _refresh_waveform_stats(self, combination: dict[str, Any] | None) -> None:
        duration_s = combination_duration_s(combination)
        self.waveform_stats_label.setText(f"Total duration: {duration_s:.9g} s")

    def _handle_name_changed(self, text: str) -> None:
        if self._syncing_editor:
            return
        combination = self._selected_combination()
        if combination is None:
            return
        combination["name"] = text
        self._refresh_selected_combination_label()

    def _handle_interval_changed(self, value: float) -> None:
        if self._syncing_editor:
            return
        combination = self._selected_combination()
        if combination is None:
            return
        combination["interval_after_s"] = float(value)
        self.waveform_preview.update_waveform(combination)
        self._refresh_waveform_stats(combination)

    def _handle_repeat_changed(self, value: int) -> None:
        if self._syncing_editor:
            return
        combination = self._selected_combination()
        if combination is None:
            return
        combination["repeat"] = max(1, int(value))
        self._refresh_selected_combination_label()
        self.waveform_preview.update_waveform(combination)
        self._refresh_waveform_stats(combination)

    def _handle_pulses_changed(self, items: list[dict[str, Any]]) -> None:
        if self._syncing_editor:
            return
        combination = self._selected_combination()
        if combination is None:
            return
        combination["items"] = normalize_pulse_items(items, allow_train=True)
        self.waveform_preview.update_waveform(combination)
        self._refresh_waveform_stats(combination)
