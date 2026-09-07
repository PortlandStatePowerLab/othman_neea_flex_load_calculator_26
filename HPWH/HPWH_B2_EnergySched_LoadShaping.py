"""
Author: Thomas Metzler
Created: 8/13/26
Adjusts load up and shed commands to keep power consumption at a constant level.
"""

import os
import shutil
import datetime as dt
import pandas as pd
from ochre import Dwelling
from ochre.utils.schedule import ALL_SCHEDULE_NAMES
import concurrent.futures
from pathlib import Path
import ochre
import random

#########################################
# USER SETTINGS
#########################################

filename = 'HPWH_Test_Loadshape_9'
Input_folder = "HPWH All Input Files 2"

# Original OCHRE defaults folder
ochre_dir = Path(ochre.__file__).resolve().parent
DEFAULT_INPUT = ochre_dir / "defaults" / "Input Files"
DEFAULT_WEATHER = ochre_dir / "defaults" / "Weather" / "USA_OR_Portland.Intl.AP.726980_TMY3.epw"

# Safe working folder (writable)
WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(WORKING_DIR, Input_folder, "bldg")
WEATHER_DIR = os.path.join(WORKING_DIR, "Weather")
WEATHER_FILE = os.path.join(WEATHER_DIR, "USA_OR_Portland.Intl.AP.726980_TMY3.epw")
XML_ADDRESS = "home.xml"
CSV_ADDRESS = "in.schedules.csv"

# Simulation parameters
Start = dt.datetime(2018, 1, 11, 0, 0)
Duration = 2  # days
t_res = 15  # minutes

# --- GLOBAL VPP EVENT SETTINGS ---
# Define the time window for active load shaping
VPP_START_TIME = dt.time(12, 0)
VPP_END_TIME = dt.time(23, 0)

# Fleet-agnostic average power targets
AVERAGE_SETPOINT_KW = 1.9     # Target average power PER HOME during VPP event
AVERAGE_DEADBAND_KW = 0.01     # Tolerance PER HOME to prevent constant toggling
ESTIMATED_LOAD_KW = 0.5       # Est. power ADDED when forcing a unit ON (LOAD) or lost when restored to NORMAL
ESTIMATED_SHED_KW = 0.3       # Est. power DROPPED when allowing a unit to SHED or gained when restored to NORMAL

# --- PID CONTROLLER GAINS ---
# Tune these parameters to adjust responsiveness and damp oscillations
KP = 2.0                      # Proportional gain
KI = 0.1                      # Integral gain
KD = 0.1                      # Derivative gain

# HPWH control parameters (°F)
Tcontrol_SHEDF = 126
Tcontrol_deadbandF = 10
Tcontrol_LOADF = 130
Tcontrol_LOADdeadbandF = 2
TbaselineF = 130
TdeadbandF = 7
Tinit = 128
count = 0

#########################################
# TEMPERATURE CONVERSIONS F to C
#########################################

def f_to_c(temp_f): 
    return (temp_f - 32) * 5/9

def f_to_c_DB(temp_f):
    return 5/9 * temp_f

Tcontrol_SHEDC = f_to_c(Tcontrol_SHEDF)
Tcontrol_deadbandC = f_to_c_DB(Tcontrol_deadbandF)
Tcontrol_LOADC = f_to_c(Tcontrol_LOADF)
Tcontrol_LOADdeadbandC = f_to_c_DB(Tcontrol_LOADdeadbandF)
TbaselineC = f_to_c(TbaselineF)
TdeadbandC = f_to_c_DB(TdeadbandF)
TinitC = f_to_c(Tinit)

#########################################
# HELPER FUNCTIONS
#########################################

def filter_schedules(home_path):
    orig_sched_file = os.path.join(home_path, CSV_ADDRESS)
    filtered_sched_file = os.path.join(home_path, 'filtered_schedules.csv')

    df_sched = pd.read_csv(orig_sched_file)
    valid_schedule_names = set(ALL_SCHEDULE_NAMES.keys())
    filtered_columns = [col for col in df_sched.columns if col in valid_schedule_names]
    dropped_columns = [col for col in df_sched.columns if col not in filtered_columns]
    
    if dropped_columns:
        print(f"Dropped invalid schedules for {home_path}: {dropped_columns}")

    df_sched_filtered = df_sched[filtered_columns]
    df_sched_filtered.to_csv(filtered_sched_file, index=False)
    return filtered_sched_file

def find_all_homes(base_dir):
    images = []
    homes = []
    for item in os.listdir(base_dir):
        home_path = os.path.join(base_dir, item)
        if os.path.isdir(home_path):
            if os.path.isfile(os.path.join(home_path, XML_ADDRESS)) and \
               os.path.isfile(os.path.join(home_path, CSV_ADDRESS)):
                homes.append(home_path)
    return homes

def remove_first_day(df, start_date):
    if 'Time' not in df.columns:
        df = df.reset_index()
        if 'index' in df.columns:
            df.rename(columns={'index': 'Time'}, inplace=True)
    df['Time'] = pd.to_datetime(df['Time'], errors='coerce')
    first_day_end = start_date + pd.Timedelta(days=1)
    return df[df['Time'] >= first_day_end].copy()

def aggregate_results(homes, work_dir):
    all_ctrl, all_base = [], []
    for home in homes:
        results_dir = os.path.join(home, "Results")
        ctrl_file = os.path.join(results_dir, "home_controlled.csv")
        base_file = os.path.join(results_dir, "home_baseline.csv")
        
        if os.path.exists(ctrl_file):
            df_ctrl = pd.read_csv(ctrl_file)
            df_ctrl["Home"] = os.path.basename(home)
            all_ctrl.append(df_ctrl)

        if os.path.exists(base_file):
            df_base = pd.read_csv(base_file)
            df_base["Home"] = os.path.basename(home)
            all_base.append(df_base)

    if all_ctrl:
        df_ctrl_all = pd.concat(all_ctrl, ignore_index=True)
        df_ctrl_all.to_csv(os.path.join(work_dir, filename + "_controlled.csv"), index=False)

    if all_base:
        df_base_all = pd.concat(all_base, ignore_index=True)
        df_base_all.to_csv(os.path.join(work_dir, filename + "_baseline.csv"), index=False)
    print(f"Aggregated CSVs written!")

#########################################
# HPWH / HVAC CONTROL & INITIALIZATION
#########################################

def determine_hpwh_control(global_mode="NORMAL"):
    """
    Highly simplified controller. 
    It purely reacts to the assigned global VPP mode.
    """
    ctrl_signal = {
        'Water Heating': {
            'Setpoint': TbaselineC,
            'Deadband': TdeadbandC,
            'Load Fraction': 1,
        }
    }

    if global_mode == "SHED":
        ctrl_signal['Water Heating'].update({
            'Setpoint': Tcontrol_SHEDC,
            'Deadband': Tcontrol_deadbandC
        })
    elif global_mode == "LOAD":
        ctrl_signal['Water Heating'].update({
            'Setpoint': Tcontrol_LOADC,
            'Deadband': Tcontrol_LOADdeadbandC
        })

    return ctrl_signal

def initialize_home(home_path, weather_file_path):
    filtered_sched_file = filter_schedules(home_path)
    hpxml_file = os.path.join(home_path, XML_ADDRESS)
    
    dwelling_args_local = {
        "start_time": Start,
        "time_res": dt.timedelta(minutes=t_res),
        "duration": dt.timedelta(days=Duration),
        "hpxml_file": hpxml_file,
        "hpxml_schedule_file": filtered_sched_file,
        "weather_file": weather_file_path,
        "verbosity": 7,
        "Equipment": {
            "Water Heating": {
                "Initial Temperature (C)": TinitC, 
                "hp_only_mode": True,
                "Max Tank Temperature": 70,
                "Upper Node": 3,
                "Lower Node": 10,
                "Upper Node Weight": 0.75,
            },
        }
    }

    base_dwelling = Dwelling(name=f"Base_{os.path.basename(home_path)}", **dwelling_args_local)
    sim_dwelling = Dwelling(name=f"Ctrl_{os.path.basename(home_path)}", **dwelling_args_local)
    return base_dwelling, sim_dwelling

def init_fleet_worker(home):
    """Worker function to initialize dwellings in parallel"""
    base_dw, sim_dw = initialize_home(home, WEATHER_FILE)
    return {
        "base": base_dw, 
        "sim": sim_dw, 
        "path": home,
        "override": "NORMAL"  # VPP command tracking state
    }

#########################################
# MAIN EXECUTION
#########################################

if __name__ == "__main__":
    # --- Directory Setup ---
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(WEATHER_DIR, exist_ok=True)
    
    # Copy homes from defaults
    count2 = 0
    for item in os.listdir(DEFAULT_INPUT):
        count2 += 1
        src = os.path.join(DEFAULT_INPUT, item)
        dst = os.path.join(INPUT_DIR, item)
        if os.path.isdir(src) and not os.path.exists(dst):
            shutil.copytree(src, dst)
            count += 1
        count += 1
        
    # Copy weather file
    if not os.path.exists(WEATHER_FILE):
        shutil.copy(DEFAULT_WEATHER, WEATHER_FILE)
        count += 1

    homes = find_all_homes(INPUT_DIR)
    print(f"Found {len(homes)} homes")

    # --- 1. Parallel Fleet Initialization ---
    fleet_data = []
    print("Initializing dwellings (in parallel)...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(init_fleet_worker, home) for home in homes]
        for f in concurrent.futures.as_completed(futures):
            try:
                fleet_data.append(f.result())
            except Exception as e:
                print("Initialization failed:", e)

    if not fleet_data:
        print("No dwellings were initialized. Exiting.")
        exit()

    num_homes = len(fleet_data)
    
    # --- 2. Co-Simulation Time Loop Setup ---
    sim_times = fleet_data[0]["base"].sim_times
    average_power_kw = 0.0

    vpp_state_log = [] # Add this line to initialize the log

    # PID State tracking variables
    integral_error = 0.0
    previous_error = 0.0

    print("Starting Co-Simulation Time Loop...")
    for sim_time in sim_times:
        current_time_of_day = sim_time.time()
        
        # Check if we are inside the VPP event window
        is_vpp_active = VPP_START_TIME <= current_time_of_day < VPP_END_TIME

        if is_vpp_active:
            # --- Active Load Shaping Dispatch Logic (Bidirectional & Asymmetrical) ---
            # PID error calculation: Error = Setpoint - Actual
            error = AVERAGE_SETPOINT_KW - average_power_kw
            
            # Discrete-time tracking transformations
            integral_error += error
            
            # --- ANTI-WINDUP CLAMPING ---
            # Prevent the integral from building up a massive "memory" when error stays positive or negative for hours
            MAX_INTEGRAL = 0.2
            MIN_INTEGRAL = -0.2
            integral_error = max(min(integral_error, MAX_INTEGRAL), MIN_INTEGRAL)
            
            derivative_error = error - previous_error
            previous_error = error
            
            # Compute PID output value
            pid_output = (KP * error) + (KI * integral_error) + (KD * derivative_error)
            
            if pid_output < -AVERAGE_DEADBAND_KW:
                # OVER setpoint -> Need to DROP load
                total_kw_to_drop = abs(pid_output) * num_homes
                
                # 1. Turn off active LOAD commands first (High impact)
                load_homes = [h for h in fleet_data if h["override"] == "LOAD"]
                random.shuffle(load_homes)
                
                units_to_drop_from_load = int(total_kw_to_drop / ESTIMATED_LOAD_KW)
                dropped_from_load = min(units_to_drop_from_load, len(load_homes))
                
                for h in load_homes[:dropped_from_load]:
                    h["override"] = "NORMAL"
                    
                # Subtract the power we just accounted for
                total_kw_to_drop -= (dropped_from_load * ESTIMATED_LOAD_KW)
                
                # 2. If we still need to drop power, issue SHED commands (Low impact)
                if total_kw_to_drop > 0:
                    normal_homes = [h for h in fleet_data if h["override"] == "NORMAL"]
                    random.shuffle(normal_homes)
                    
                    units_to_shed = int(total_kw_to_drop / ESTIMATED_SHED_KW)
                    shed_applied = min(units_to_shed, len(normal_homes))
                    
                    for h in normal_homes[:shed_applied]:
                        h["override"] = "SHED"
                        
            elif pid_output > AVERAGE_DEADBAND_KW:
                # UNDER setpoint -> Need to ADD load
                total_kw_to_add = pid_output * num_homes
                
                # 1. Turn off active SHED commands first (Low impact)
                shed_homes = [h for h in fleet_data if h["override"] == "SHED"]
                random.shuffle(shed_homes)
                
                units_to_restore_from_shed = int(total_kw_to_add / ESTIMATED_SHED_KW)
                restored_from_shed = min(units_to_restore_from_shed, len(shed_homes))
                
                for h in shed_homes[:restored_from_shed]:
                    h["override"] = "NORMAL"
                    
                # Subtract the power we just accounted for
                total_kw_to_add -= (restored_from_shed * ESTIMATED_SHED_KW)
                
                # 2. If we still need to add power, issue LOAD commands (High impact)
                if total_kw_to_add > 0:
                    normal_homes = [h for h in fleet_data if h["override"] == "NORMAL"]
                    random.shuffle(normal_homes)
                    
                    units_to_load = int(total_kw_to_add / ESTIMATED_LOAD_KW)
                    load_applied = min(units_to_load, len(normal_homes))
                    
                    for h in normal_homes[:load_applied]:
                        h["override"] = "LOAD"
        else:
            # --- VPP is OFF. Force all homes back to normal ---
            for h in fleet_data:
                h["override"] = "NORMAL"
            
            # Reset PID memory tracking to prevent baseline distortion at next event start
            integral_error = 0.0
            previous_error = 0.0

        #Initialize 
        current_step_aggregate_power = 0.0
        
        for home_data in fleet_data:
            base_dw = home_data["base"]
            sim_dw = home_data["sim"]
            
            # 1. Baseline Update
            base_ctrl = {"Water Heating": {"Setpoint": TbaselineC, "Deadband": TdeadbandC, "Load Fraction": 1}}
            base_dw.update(control_signal=base_ctrl)
            
           # 2. Controlled Update (driven purely by the VPP state now)
            control_cmd = determine_hpwh_control(global_mode=home_data["override"])
            
            # The update() method usually returns a dictionary of the current timestep's metrics
            metrics = sim_dw.update(control_signal=control_cmd)
            
            # 3. Read back real-time power
            # We will try the returned metrics first, then fall back to the standard current_results attribute
            if isinstance(metrics, dict) and "Total Electric Power (kW)" in metrics:
                home_power = metrics["Total Electric Power (kW)"]
            elif hasattr(sim_dw, 'current_results'):
                home_power = sim_dw.current_results.get("Total Electric Power (kW)", 0.0)
            else:
                # Failsafe so the code doesn't crash, though it means our VPP controller will read 0
                home_power = 0.0 
                
            current_step_aggregate_power += home_power
            
        # Recalculate average fleet power for the next time step's logic
        aggregate_power_kw = current_step_aggregate_power
        average_power_kw = aggregate_power_kw / num_homes

        # --- NEW: Log Fleet States for this Timestep ---
        shed_count = sum(1 for h in fleet_data if h["override"] == "SHED")
        load_count = sum(1 for h in fleet_data if h["override"] == "LOAD")
        normal_count = sum(1 for h in fleet_data if h["override"] == "NORMAL")
        
        vpp_state_log.append({
            "Time": sim_time,
            "Target Average Power (kW)": AVERAGE_SETPOINT_KW if is_vpp_active else "OFF",
            "Actual Average Power (kW)": average_power_kw,
            "Aggregate Power (kW)": aggregate_power_kw,
            "Units in NORMAL": normal_count,
            "Units in SHED": shed_count,
            "Units in LOAD": load_count
        })

    # --- 3. Finalize and Output Data ---
    print("Simulation complete! Finalizing results...")
    
    CTRL_COLS = ["Time", "Total Electric Power (kW)",
                 "Total Electric Energy (kWh)",
                 "Water Heating Electric Power (kW)",
                 "Water Heating COP (-)",
                 "Water Heating Deadband Upper Limit (C)",
                 "Water Heating Deadband Lower Limit (C)",
                 "Water Heating Heat Pump COP (-)",
                 "Water Heating Control Temperature (C)",
                 "Hot Water Outlet Temperature (C)",
                 "Temperature - Indoor (C)"]
    
    for home_data in fleet_data:
        home_path = home_data["path"]
        results_dir = os.path.join(home_path, "Results")
        os.makedirs(results_dir, exist_ok=True)
        
        df_base, _, _ = home_data["base"].finalize()
        df_ctrl, _, _ = home_data["sim"].finalize()
        
        df_base = remove_first_day(df_base, Start)
        df_ctrl = remove_first_day(df_ctrl, Start)
        
        df_ctrl = df_ctrl[[c for c in CTRL_COLS if c in df_ctrl.columns]]
        df_base = df_base[[c for c in CTRL_COLS if c in df_base.columns]]
        
        df_ctrl.to_csv(os.path.join(results_dir, 'home_controlled.csv'), index=False)
        df_base.to_csv(os.path.join(results_dir, 'home_baseline.csv'), index=False)

    # --- 4. Aggregate ---
    aggregate_results(homes, WORKING_DIR)

    # --- 5. Export VPP State Log ---
    print("Saving VPP state log...")
    df_vpp_log = pd.DataFrame(vpp_state_log)
    vpp_log_path = os.path.join(WORKING_DIR, filename + "_VPP_Fleet_States.csv")
    df_vpp_log.to_csv(vpp_log_path, index=False)
    print(f"VPP State Log saved to: {vpp_log_path}")