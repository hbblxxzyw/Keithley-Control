"""
GUI application entry point for Keithley 2636 SMU Control.
"""

import os
import sys
from PySide6.QtWidgets import QApplication
from ui.main_window_ui import MainWindowUI
from core.dummy_instrument import DummyKeithley2636
from controllers.main_controller import MainController
from core.real_instrument import RealKeithley2636


def create_instrument():
    """
    Select the real instrument when VISA is available, otherwise fall back to
    the dummy driver so the GUI remains usable on development machines.
    """
    mode = os.environ.get("KEITHLEY_INSTRUMENT", "auto").strip().lower()
    if mode in {"dummy", "sim", "simulator"}:
        print("Using dummy Keithley instrument.")
        return DummyKeithley2636()

    if mode in {"debug", "dry-run", "dryrun"}:
        print("Using RealKeithley2636 debug dry-run mode.")
        return RealKeithley2636(debug=True)

    if mode == "real":
        return RealKeithley2636(debug=False)

    visa_ok, visa_error = RealKeithley2636.visa_available()
    if visa_ok:
        instrument = RealKeithley2636(debug=False)
        try:
            resource_str = instrument.find_resource_address(preferred_serial="4399155")
        except Exception as exc:
            resource_str = None
            visa_error = str(exc)
        if resource_str:
            return instrument

        resource_manager = getattr(instrument, "_rm", None)
        close = getattr(resource_manager, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        print("No VISA instrument found; using dummy Keithley instrument.")
        if visa_error:
            print(f"VISA scan error: {visa_error}")
        return DummyKeithley2636()

    print("VISA backend unavailable; using dummy Keithley instrument.")
    print(f"VISA error: {visa_error}")
    return DummyKeithley2636()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    instrument = create_instrument()
    window = MainWindowUI()
    controller = MainController(ui=window, instrument=instrument)
    window.show()
    sys.exit(app.exec())
