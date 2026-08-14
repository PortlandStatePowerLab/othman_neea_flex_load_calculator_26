import os
import datetime as dt
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

#from ochre import Dwelling
from ochre.utils import default_input_path  # for using sample files
from ochre import HeatPumpWaterHeater

# --- Set up output folder relative to this script's location ---
script_dir = os.path.dirname(os.path.abspath(__file__))
output_dir = os.path.join(script_dir, "outputs")
os.makedirs(output_dir, exist_ok=True)

# Define equipment and simulation parameters
setpoint_default = 51  # in C
deadband_default = 5.56  # in C
equipment_args = {
    "start_time": dt.datetime(2018, 1, 1, 0, 0),
    "time_res": dt.timedelta(minutes=1),
    "duration": dt.timedelta(days=1),
    "verbosity": 7,
    "save_results": False,
    "Setpoint Temperature (C)": setpoint_default,
    "Tank Volume (L)": 250,
    "Tank Height (m)": 1.22,
    "UA (W/K)": 2.17,
    "HPWH COP (-)": 4.5,
}

# Create water draw schedule
times = pd.date_range(
    equipment_args["start_time"],
    equipment_args["start_time"] + equipment_args["duration"],
    freq=equipment_args["time_res"],
    inclusive="left",
)
water_draw_magnitude = 12  # L/min
withdraw_rate = np.random.choice([0, water_draw_magnitude], p=[0.99, 0.01], size=len(times))
schedule = pd.DataFrame(
    {
        "Water Heating (L/min)": withdraw_rate,
        "Water Heating Setpoint (C)": setpoint_default,
        "Water Heating Deadband (C)": deadband_default,
        "Zone Temperature (C)": 20,
        "Zone Wet Bulb Temperature (C)": 15,
        "Mains Temperature (C)": 7,
    },
    index=times,
)

# Initialize equipment
hpwh = HeatPumpWaterHeater(schedule=schedule, **equipment_args)

# Simulate
control_signal = {}
for t in hpwh.sim_times:
    if t.hour in [7, 16]:
        control_signal = {"Deadband": deadband_default - 2.78}
    elif t.hour in [8, 17]:
        control_signal = {
            "Setpoint": setpoint_default - 5.56,
            "Deadband": deadband_default - 2.78,
        }
    else:
        control_signal = {}

    _ = hpwh.update(control_signal=control_signal)

df = hpwh.finalize()

# --- Save full results as CSV ---
csv_path = os.path.join(output_dir, "hpwh_results.csv")
df.to_csv(csv_path)
print(f"Results saved to: {csv_path}")

# --- Plot and save figure ---
cols_to_plot = [
    "Hot Water Outlet Temperature (C)",
    "Hot Water Average Temperature (C)",
    "Water Heating Deadband Upper Limit (C)",
    "Water Heating Deadband Lower Limit (C)",
    "Water Heating Electric Power (kW)",
    "Hot Water Unmet Demand (kW)",
    "Hot Water Delivered (L/min)",
]
ax = df.loc[:, cols_to_plot].plot(figsize=(12, 6))
plt.tight_layout()

plot_path = os.path.join(output_dir, "hpwh_results_plot.png")
plt.savefig(plot_path, dpi=150)
print(f"Plot saved to: {plot_path}")

plt.show()