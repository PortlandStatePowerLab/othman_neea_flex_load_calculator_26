# -*- coding: utf-8 -*-
"""
Created on Wed Sep  3 17:39:26 2025
Modified on Nov 19 2025
Modified on Jun 17 2026
Modified on Aug 21 2026

@author: danap
@edited by: jdinsmor
@edited by: t-metzler
@edited by: o-murad
"""

import os
import shutil
import datetime as dt
import threading
import pandas as pd
from ochre import Dwelling
from ochre.utils.schedule import ALL_SCHEDULE_NAMES
import concurrent.futures
from pathlib import Path
import ochre
import random

# OCHRE seeds the *global* NumPy RNG inside Dwelling.__init__ (see
# ochre.Simulator.__init__ -> np.random.seed(seed)) and then immediately
# draws from it while generating stochastic equipment schedules. Since
# homes run concurrently across ThreadPoolExecutor worker threads, two
# Dwelling() constructions overlapping in time would reset/consume that
# shared global state out of order, corrupting each other's draws and
# defeating the point of a per-home seed. Serializing construction (only,
# not the much more expensive per-timestep simulation loop that follows)
# avoids that race.
_dwelling_init_lock = threading.Lock()

#########################################
# USER SETTINGS
#########################################

#Gallons, MLU, MLU duration, Shed duration, ELU, ELU duration, Shed duration, Offset sheds
# Distinct name for this run: isolates the effect of release WINDOW length
# alone (300 min) vs. the original 90-min-window baseline -- everything
# else (SHED event, deadbands, step-change release, dispatch stagger,
# RNG seeding, EVENT_DATE fix) is back to the original/validated values,
# no ramp.
filename = '2025_ReleaseWindow300_StepChange'

#"HPWH 50 Input Files", "HPWH 66 Input Files/bldg", "HPWH 80 Input Files", "HPWH All Input Files/bldg"
Input_folder = "HPWH 50 Input Files"

# Original OCHRE defaults folder
ochre_dir = Path(ochre.__file__).resolve().parent
DEFAULT_INPUT = ochre_dir / "defaults" / "Input Files"
print("OCHRE installed at:", ochre_dir)
print(DEFAULT_INPUT)

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
t_res = 1  # minutes

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
# RESERVE EVENT SETTINGS
#########################################

RESERVE_COMMAND = 'SHED'

reserve_event = {
    'dispatch_time': '15:00',
    'duration': 1.5   # 1.5 hr = 90 min event
}

# The single calendar day the reserve event happens on -- the LAST day of
# the simulation. remove_first_day() strips every earlier day before
# results are saved, so this is also the only day that survives into the
# saved CSVs/plots. determine_reserve_control() and simulate_home_phase1()
# both anchor to this FIXED date (instead of each sim_time's own date) so
# the event fires exactly once, on the day that actually gets analyzed --
# not on every day of the simulation, and not on an earlier warm-up day
# whose temp_at_event_end would then rank units by the wrong event.
EVENT_DATE = (Start + dt.timedelta(days=Duration - 1)).date()

#########################################
# RELEASE (POST-EVENT) SETTINGS  -- NEW
#########################################
# After event_end, instead of every unit snapping back to baseline
# simultaneously (which causes the cold-load-pickup spike), each unit's
# return to baseline is staggered across this window. Units are ranked by
# their own tank temperature at event_end: the warmest units (least
# recovery time needed) are released first, the coldest units (longest
# recovery time) are held longest, so recovery load spreads out instead
# of overlapping. Adjust this single value to try different window
# lengths (e.g. 5-30 minutes).
RELEASE_WINDOW_MINUTES = 300

#########################################
# Random dispatch time
#########################################

def get_unit_delay_minutes(home_path=None):
    return random.uniform(5, 30)  # random delay between 5 and 30 minutes

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
# RESERVE CONTROL FUNCTION  -- MODIFIED
#########################################

def determine_reserve_control(sim_time, event_cfg, unit_delay_minutes=0,
                               release_delay_minutes=0, **kwargs):
    """
    Applies the reserve command (SHED or LOAD) from this unit's own
    (staggered) dispatch time until its own (staggered) release time, on
    the single fixed EVENT_DATE (not sim_time's own date -- otherwise this
    would retrigger on every simulated day):

        event_start = EVENT_DATE + event_cfg['dispatch_time'] + unit_delay_minutes
        event_end   = event_start + event_cfg['duration']
        release_time = event_end + release_delay_minutes

    Outside [event_start, release_time), the unit runs at baseline
    setpoint/deadband. release_delay_minutes is 0 for a unit released
    immediately at event_end, and up to RELEASE_WINDOW_MINUTES for the
    last unit released. Release is an instant step change back to
    baseline setpoint/deadband -- no gradual ramp.
    """
    ctrl_signal = {
        'Water Heating': {
            'Setpoint': TbaselineC,
            'Deadband': TdeadbandC,
            'Load Fraction': 1,
        }
    }

    event_start = pd.to_datetime(f"{EVENT_DATE} {event_cfg['dispatch_time']}")
    event_start = event_start + dt.timedelta(minutes=unit_delay_minutes)
    event_end = event_start + pd.Timedelta(hours=event_cfg['duration'])
    release_time = event_end + dt.timedelta(minutes=release_delay_minutes)

    if event_start <= sim_time < release_time:
        if RESERVE_COMMAND == 'SHED':
            ctrl_signal['Water Heating'].update({
                'Setpoint': Tcontrol_SHEDC,
                'Deadband': Tcontrol_deadbandC
            })
        elif RESERVE_COMMAND == 'LOAD':
            ctrl_signal['Water Heating'].update({
                'Setpoint': Tcontrol_LOADC,
                'Deadband': Tcontrol_LOADdeadbandC
            })

    return ctrl_signal

#########################################
# TANK TEMPERATURE READ  -- NEW
#########################################

def get_tank_temperature_c(hpwh_unit):
    """
    Returns the HPWH's lower-node tank temperature (deg C) -- the same
    node OCHRE's own internal control logic uses to decide heat pump
    on/off (see WaterHeater.py: self.model.states[self.t_lower_idx]).
    Read directly from the live model state, not from a results/output
    column, so it's available at any point during the simulation
    (unlike hpwh_unit.results, which OCHRE clears periodically and which
    never held equipment-level history to begin with in this setup).
    """
    return hpwh_unit.model.states[hpwh_unit.t_lower_idx]

#########################################
# SCHEDULE FILTERING
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

#########################################
# DWELLING ARGS HELPER
#########################################

def build_dwelling_args(hpxml_file, filtered_sched_file, weather_file_path, home_path):
    # OCHRE only seeds its (global) RNG if given a `seed` or `output_path`
    # (see Simulator.__init__); without one, every Dwelling() draws from
    # whatever the global NumPy RNG state happens to be, so stochastic
    # equipment (e.g. EventBasedLoad appliances) gets a DIFFERENT random
    # schedule each time -- including between a home's controlled and
    # baseline runs, which then diverge even before the event, for reasons
    # unrelated to the reserve dispatch. Seeding both from the same
    # home_path makes them share the exact same stochastic schedule, so
    # the only difference between them is the reserve event itself.
    return {
        "start_time": Start,
        "time_res": dt.timedelta(minutes=t_res),
        "duration": dt.timedelta(days=Duration),
        "hpxml_file": hpxml_file,
        "hpxml_schedule_file": filtered_sched_file,
        "weather_file": weather_file_path,
        "verbosity": 7,
        "seed": home_path,
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

#########################################
# PHASE 1 -- run each home up through event_end, record temp  -- NEW
#########################################

def simulate_home_phase1(home_path, weather_file_path, event_cfg):
    filtered_sched_file = filter_schedules(home_path)
    hpxml_file = os.path.join(home_path, XML_ADDRESS)

    unit_delay_minutes = get_unit_delay_minutes(home_path)

    dwelling_args_local = build_dwelling_args(hpxml_file, filtered_sched_file, weather_file_path, home_path)

    with _dwelling_init_lock:
        sim_dwelling = Dwelling(name="HPWH Controlled", **dwelling_args_local)
    hpwh_unit = sim_dwelling.get_equipment_by_end_use('Water Heating')

    # Anchored to the same fixed EVENT_DATE determine_reserve_control()
    # uses, so this loop breaks exactly at the one real event's end (the
    # day that survives remove_first_day()), not an earlier warm-up day.
    event_start = pd.to_datetime(f"{EVENT_DATE} {event_cfg['dispatch_time']}")
    event_start = event_start + dt.timedelta(minutes=unit_delay_minutes)
    event_end = event_start + pd.Timedelta(hours=event_cfg['duration'])

    sim_times = list(sim_dwelling.sim_times)
    resume_idx = None
    temp_at_event_end = None
    # Tracks the exact same tank node get_tank_temperature_c() reads, at
    # every timestep, so recovery-to-baseline can be measured downstream
    # from real data instead of an OCHRE output column that may not
    # correspond to the control node (e.g. "Minimum Temperature" picks up
    # the coldest node, which can be a mains-fed dead zone, not the
    # control node used for HP on/off decisions).
    tank_temp_log = []

    for i, sim_time in enumerate(sim_times):
        # Held in the reserve command through this phase; the real,
        # staggered release_delay_minutes gets applied in Phase 2 once
        # every unit's event_end temperature is known.
        control_cmd = determine_reserve_control(
            sim_time=sim_time,
            event_cfg=event_cfg,
            unit_delay_minutes=unit_delay_minutes,
            release_delay_minutes=10**6,
        )
        sim_dwelling.update(control_signal=control_cmd)
        tank_temp_log.append((sim_time, get_tank_temperature_c(hpwh_unit)))

        if sim_time >= event_end and temp_at_event_end is None:
            temp_at_event_end = get_tank_temperature_c(hpwh_unit)
            resume_idx = i + 1
            break  # pause here; Phase 2 resumes from resume_idx

    return {
        "home_path": home_path,
        "sim_dwelling": sim_dwelling,
        "sim_times": sim_times,
        "tank_temp_log": tank_temp_log,
        "resume_idx": resume_idx,
        "unit_delay_minutes": unit_delay_minutes,
        "temp_at_event_end": temp_at_event_end,
    }

#########################################
# ASSIGN RELEASE DELAYS -- NEW
#########################################

def compute_release_delays(phase1_results, window_minutes):
    """
    Ranks units by tank temperature at event_end and spreads their
    release delays across [0, window_minutes]. Warmest unit -> 0 min
    delay (released first, shortest recovery needed). Coldest unit ->
    window_minutes delay (released last, longest recovery needed).
    """
    n = len(phase1_results)
    if n <= 1:
        for r in phase1_results:
            r["release_delay_minutes"] = 0.0
        return phase1_results

    ranked = sorted(phase1_results, key=lambda r: r["temp_at_event_end"], reverse=True)

    for rank, r in enumerate(ranked):
        r["release_delay_minutes"] = window_minutes * rank / (n - 1)

    return ranked

#########################################
# PHASE 2 -- resume with assigned release delay, run baseline, save  -- NEW
#########################################

def simulate_home_phase2(phase1_result, event_cfg):
    home_path = phase1_result["home_path"]
    sim_dwelling = phase1_result["sim_dwelling"]
    sim_times = phase1_result["sim_times"]
    resume_idx = phase1_result["resume_idx"]
    unit_delay_minutes = phase1_result["unit_delay_minutes"]
    release_delay_minutes = phase1_result["release_delay_minutes"]
    hpwh_unit = sim_dwelling.get_equipment_by_end_use('Water Heating')
    tank_temp_log = list(phase1_result["tank_temp_log"])

    for sim_time in sim_times[resume_idx:]:
        control_cmd = determine_reserve_control(
            sim_time=sim_time,
            event_cfg=event_cfg,
            unit_delay_minutes=unit_delay_minutes,
            release_delay_minutes=release_delay_minutes,
        )
        sim_dwelling.update(control_signal=control_cmd)
        tank_temp_log.append((sim_time, get_tank_temperature_c(hpwh_unit)))

    df_ctrl, _, _ = sim_dwelling.finalize()

    tank_temp_df = pd.DataFrame(tank_temp_log, columns=["Time", "Tank Control Node Temperature (C)"])
    tank_temp_df["Time"] = pd.to_datetime(tank_temp_df["Time"])

    # Baseline run (independent of the reserve event / release logic)
    filtered_sched_file = os.path.join(home_path, 'filtered_schedules.csv')
    hpxml_file = os.path.join(home_path, XML_ADDRESS)
    dwelling_args_local = build_dwelling_args(hpxml_file, filtered_sched_file, phase1_result.get("weather_file_path"), home_path)
    with _dwelling_init_lock:
        base_dwelling = Dwelling(name="HPWH Baseline", **dwelling_args_local)
    for t_base in base_dwelling.sim_times:
        base_ctrl = {"Water Heating": {"Setpoint": TbaselineC, "Deadband": TdeadbandC, "Load Fraction": 1}}
        base_dwelling.update(control_signal=base_ctrl)
    df_base, _, _ = base_dwelling.finalize()

    df_ctrl = remove_first_day(df_ctrl, Start)
    df_base = remove_first_day(df_base, Start)

    df_ctrl = df_ctrl.merge(tank_temp_df, on="Time", how="left")

    CTRL_COLS = ["Time", "Total Electric Power (kW)",
                 "Total Electric Energy (kWh)",
                 "Water Heating Electric Power (kW)",
                 "Water Heating COP (-)",
                 "Water Heating Deadband Upper Limit (C)",
                 "Water Heating Deadband Lower Limit (C)",
                 "Water Heating Heat Pump COP (-)",
                 "Tank Control Node Temperature (C)",
                 "Hot Water Minimum Temperature (C)",
                 "Hot Water Average Temperature (C)",
                 "Hot Water Maximum Temperature (C)",
                 "Hot Water Outlet Temperature (C)",
                 "Temperature - Indoor (C)"]
    BASE_COLS = CTRL_COLS

    df_ctrl = df_ctrl[[c for c in CTRL_COLS if c in df_ctrl.columns]]
    df_base = df_base[[c for c in BASE_COLS if c in df_base.columns]]

    results_dir = os.path.join(home_path, "Results")
    os.makedirs(results_dir, exist_ok=True)
    df_ctrl.to_csv(os.path.join(results_dir, 'hpwh_controlled.csv'), index=False)
    df_base.to_csv(os.path.join(results_dir, 'hpwh_baseline.csv'), index=False)

    return df_ctrl, df_base

#########################################
# FIND ALL HOMES
#########################################

def find_all_homes(base_dir):
    homes = []
    for item in os.listdir(base_dir):
        home_path = os.path.join(base_dir, item)
        if os.path.isdir(home_path):
            if os.path.isfile(os.path.join(home_path, XML_ADDRESS)) and \
               os.path.isfile(os.path.join(home_path, CSV_ADDRESS)):
                homes.append(home_path)
    return homes

#########################################
# DELETE FIRST DAY ONLY
#########################################

def remove_first_day(df, start_date):
    if 'Time' not in df.columns:
        df = df.reset_index()
        if 'index' in df.columns:
            df.rename(columns={'index': 'Time'}, inplace=True)

    df['Time'] = pd.to_datetime(df['Time'], errors='coerce')
    first_day_end = start_date + pd.Timedelta(days=1)
    return df[df['Time'] >= first_day_end].copy()

#########################################
# AGGREGATE RESULTS
#########################################

def aggregate_results(homes, work_dir):
    all_ctrl, all_base = [], []

    for home in homes:
        results_dir = os.path.join(home, "Results")
        ctrl_file = os.path.join(results_dir, "hpwh_controlled.csv")
        base_file = os.path.join(results_dir, "hpwh_baseline.csv")

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

    print(f"Aggregated CSVs written! {count}")

#########################################
# MAIN EXECUTION  -- MODIFIED: two-phase run
#########################################

if __name__ == "__main__":
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(WEATHER_DIR, exist_ok=True)
    try:
        weather_path = Path(WEATHER_DIR)
        weather_path.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] Weather directory ready: {weather_path}")
    except Exception as e:
        print(f"[ERROR] Failed to create directory {weather_path}: {e}")

    count2 = 0

    for item in os.listdir(DEFAULT_INPUT):
        count2 += 1
        src = os.path.join(DEFAULT_INPUT, item)
        dst = os.path.join(INPUT_DIR, item)
        if os.path.isdir(src) and not os.path.exists(dst):
            shutil.copytree(src, dst)
            count += 1
        count += 1

    if not os.path.exists(WEATHER_FILE):
        shutil.copy(DEFAULT_WEATHER, WEATHER_FILE)
        count += 1

    homes = find_all_homes(INPUT_DIR)
    print(f"homes: ", INPUT_DIR)
    print(f"Found {len(homes)} homes")

    # ---- Phase 1: run every home through the reserve event up to
    # event_end, recording each unit's tank temperature at that moment ----
    phase1_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(simulate_home_phase1, home, WEATHER_FILE, reserve_event): home
            for home in homes
        }
        for f in concurrent.futures.as_completed(futures):
            try:
                result = f.result()
                result["weather_file_path"] = WEATHER_FILE
                phase1_results.append(result)
            except Exception as e:
                print(f"Phase 1 simulation failed for {futures[f]}: {e}")

    # ---- Rank units by event-end temperature and assign staggered
    # release delays (warmest released first) ----
    phase1_results = compute_release_delays(phase1_results, RELEASE_WINDOW_MINUTES)

    # ---- Persist the per-home delay/temperature assignments -- NEW ----
    # (release_delay_minutes and temp_at_event_end only ever existed in
    # memory otherwise, with no way to inspect the assigned distribution
    # after the run completes)
    release_summary = pd.DataFrame([
        {
            "Home": os.path.basename(r["home_path"]),
            "unit_delay_minutes": r["unit_delay_minutes"],
            "temp_at_event_end_C": r["temp_at_event_end"],
            "temp_at_event_end_F": r["temp_at_event_end"] * 9 / 5 + 32,
            "release_delay_minutes": r["release_delay_minutes"],
        }
        for r in phase1_results
    ])
    release_summary.to_csv(os.path.join(WORKING_DIR, filename + "_release_summary.csv"), index=False)
    print(f"Release summary written for {len(release_summary)} homes")

    # ---- Phase 2: resume each home with its assigned release delay,
    # run its baseline case, and save results ----
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(simulate_home_phase2, r, reserve_event): r["home_path"]
            for r in phase1_results
        }
        for f in concurrent.futures.as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"Phase 2 simulation failed for {futures[f]}: {e}")

    print("All simulations complete!")

    aggregate_results(homes, WORKING_DIR)
