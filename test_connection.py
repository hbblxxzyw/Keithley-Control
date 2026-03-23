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

    print("\n========== 2. Test Basic Single Point Output (SMU B) ==========")
    smu.set_voltage_source("smub", 1.0, 0.01)  # 1V, 10mA limit
    smu.set_output("smub", True)
    current = smu.measure_current("smub")
    print(f"[Result] SMU B Current = {current} A")
    smu.set_output("smub", False)

    print("\n========== 3. Test High-Speed Hardware Buffer Sweep (SMU A) ==========")
    # 这里的调用将触发我们刚才重构的 TSP 块状脚本下发
    print("Executing Sweep: 0V to 5V, 51 points, 0.01s delay, 1.0 NPLC...")
    
    # run_iv_sweep 返回生成器，按块迭代合并得到完整 (voltages, currents, measured_voltages)
    voltages: list[float] = []
    currents: list[float] = []
    measured_voltages: list[float] = []
    for v_chunk, i_chunk, measured_v_chunk in smu.run_iv_sweep(
        smu_channel="smua",
        start_v=0.0,
        stop_v=5.0,
        points=51,
        delay=0.01,
        nplc=1.0,
    ):
        voltages.extend(v_chunk)
        currents.extend(i_chunk)
        if measured_v_chunk is not None:
            measured_voltages.extend(measured_v_chunk)

    print(f"\n[Result] Sweep finished. Received {len(voltages)} voltage points and {len(currents)} current points.")

    print("\n========== 4. Disconnect ==========")
    smu.disconnect()
    print("Test done.")

if __name__ == "__main__":
    main()
