"""
GUI application entry point for Keithley 2636 SMU Control.
"""

import sys
from PySide6.QtWidgets import QApplication
from ui.main_window_ui import MainWindowUI
from core.dummy_instrument import DummyKeithley2636
from controllers.main_controller import MainController
from core.real_instrument import RealKeithley2636


if __name__ == "__main__":
    app = QApplication(sys.argv)
    #instrument = DummyKeithley2636()
    instrument = RealKeithley2636(debug=False)
    window = MainWindowUI()
    controller = MainController(ui=window, instrument=instrument)
    window.show()
    sys.exit(app.exec())
