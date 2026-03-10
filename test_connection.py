"""
Connection and TSP command test script when no instrument is connected.

Runs RealKeithley2636 with debug=True to verify that TSP commands are
generated and "sent" as expected (terminal prints [DEBUG SEND] prefixed
low-level commands in order).
"""

from core.real_instrument import RealKeithley2636


def main() -> None:
    # Use a dummy address; debug=True does not require a physical connection
    smu = RealKeithley2636(debug=True)

    print("========== 1. Connect ==========")
    smu.connect("TCPIP0::192.168.1.100::inst0::INSTR")

    print("\n========== 2. Set smua voltage source: 5 V, 10 mA limit ==========")
    smu.set_voltage_source("smua", 5.0, 0.01)  # 10 mA = 0.01 A

    print("\n========== 3. Enable smua output ==========")
    smu.set_output("smua", True)

    print("\n========== 4. Measure smua current ==========")
    current = smu.measure_current("smua")
    print(f"[Result] Current = {current} A")

    print("\n========== 5. Disable smua output ==========")
    smu.set_output("smua", False)

    print("\n========== 6. Disconnect ==========")
    smu.disconnect()
    print("Test done.")


if __name__ == "__main__":
    main()
