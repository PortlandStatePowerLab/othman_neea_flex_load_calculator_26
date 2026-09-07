# -*- coding: utf-8 -*-
"""
Created on Wed Sep  3 17:39:26 2025
Modified on Nov 19 2025
Modified on Jun 17 2026
Modified on Aug 31 2026
Forked to V3 on Aug 31 2026 -- power-capped admission control
Copied into this V2 file and modified on Sep 7 2026 -- CAP_KW/
ADMISSIONS_PER_TIMESTEP replaced with CAP_PCT/ADMISSION_RATE_PCT
(percentage-based, self-scaling to fleet size). V3.py itself was left
untouched -- this file is a separate fork of it, not a shared module.

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

# Distinct name per test run -- change before each run so results don't
# overwrite each other.
filename = 'HPWH_AdmissionControl_PctCapRate_n95test'

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

# Per-test output folder -- keeps each run's aggregated CSVs alongside the
# C1/C2/C3 analysis outputs they feed, instead of piling up at WORKING_DIR.
RESULTS_DIR = os.path.join(WORKING_DIR, "Ready_data", filename)

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
    'duration': 1.5   # 1.5 hr = 90 min event -- unchanged from the validated baseline
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
# POWER-CAPPED ADMISSION CONTROL
#########################################
# Units are ranked warmest-first from event_end (priority order), but WHEN
# a unit actually gets released depends on real-time fleet conditions, not
# a delay decided in advance. At each timestep after event_end, the next-
# highest-priority unit still waiting is admitted (switched to baseline
# setpoint/deadband) only if the fleet's current aggregate Water Heating
# Electric Power is already below the cap; otherwise it waits one more
# timestep and is reconsidered. Once admitted, a unit stays admitted -- the
# cap only gates new admissions, not units already recovering.
#
# CAP_PCT / ADMISSION_RATE_PCT -- percentage-based versions of the fixed
# CAP_KW=8.0 / ADMISSIONS_PER_TIMESTEP=8 values validated at n=95 (0%
# never-admitted). Fixed kW/count values don't generalize to a different
# fleet size or composition -- a fleet's own baseline power doesn't scale
# linearly with n (this is exactly what broke the n=498 admission-rate/cap
# test: 15.0 kW and "1/timestep", both validated at n=95, silently failed
# at n=498 until re-measured from n=498's own data). Expressing both as
# percentages of a quantity MEASURED FROM THIS RUN'S OWN FLEET, instead of
# a stored constant, is what actually makes them portable across fleet
# size:
#
#   CAP_PCT            -- % of this fleet's own measured baseline (no-
#                          dispatch) daily peak Water Heating power. This
#                          requires an extra "Phase 0" pass (see
#                          measure_fleet_baseline_peak_kw() below) that
#                          runs every home's baseline case BEFORE the
#                          admission loop, purely to measure this number --
#                          adds runtime, but replaces a hardcoded CAP_KW
#                          with one automatically re-derived for whatever
#                          fleet is actually being simulated.
#   ADMISSION_RATE_PCT  -- % of the fleet size n (n itself already comes
#                          from scanning Input_folder, not a constant).
#
# 53% / 8% are the percentages that reproduce the n=95-validated CAP_KW=8.0
# / ADMISSIONS_PER_TIMESTEP=8 (measured baseline peak was 15.054 kW; 8/95 =
# 8.42%, rounds to 8%). Re-validate before trusting these at a different n
# or Input_folder -- the whole point of this change is that they get
# RE-MEASURED per run, not assumed constant, but the DECISION of which
# specific percentages are safe is only proven at n=95 so far.
CAP_PCT = 53.0

ADMISSION_RATE_PCT = 8.0

# Computed at runtime (Phase 0, in __main__) from CAP_PCT and this fleet's
# own measured baseline peak power -- left as None here so it's obvious
# this is a derived value, not a constant to edit directly.
CAP_KW = None

#########################################
# Random dispatch time
#########################################

def get_unit_delay_minutes(home_path=None):
    # Seeded per-home (like OCHRE's own seed=home_path) so the same home
    # always gets the same dispatch stagger delay across runs -- makes
    # repeat runs and cap/rate sweeps comparable to each other instead of
    # confounded by fresh random staggering every run. A local
    # random.Random(...) instance (not the shared `random` module) avoids
    # the same cross-thread RNG race _dwelling_init_lock exists to
    # prevent for OCHRE's construction -- Phase 1 calls this concurrently
    # across threads.
    return random.Random(home_path).uniform(5, 30)

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
# RESERVE CONTROL FUNCTION -- used by Phase 1 only
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
    setpoint/deadband. Only ever called by Phase 1 (with
    release_delay_minutes held far in the future) and by the baseline-peak
    probe (which never sets this signal at all) -- the post-event
    admission decision is made dynamically by run_admission_controlled_recovery(),
    not by a precomputed release_time.
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
# TANK TEMPERATURE READ
#########################################

def get_tank_temperature_c(hpwh_unit):
    """
    Returns the HPWH's lower-node tank temperature (deg C) -- the same
    node OCHRE's own internal control logic partially weights (see
    WaterHeater.py: t_control = 0.75*upper + 0.25*lower). Read directly
    from the live model state so it's available at any point during the
    simulation, matching what temp_at_event_end has always used for
    ranking.
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
# PHASE 0 -- measure this fleet's own baseline peak power, to size
# CAP_KW from CAP_PCT
#########################################

def simulate_home_baseline_only(home_path, weather_file_path):
    """
    Runs just the undispatched baseline case for one home (no reserve
    event at all), for the full Start->Start+Duration window. Used only to
    measure the fleet's own baseline peak power -- a second, separate
    baseline run happens later in finalize_and_save_home() for the actual
    saved baseline CSV. Duplicated work, but keeps this probe independent
    of admission-control state and simple to reason about; not worth
    threading the two together for the run frequency this script sees.
    """
    filtered_sched_file = os.path.join(home_path, 'filtered_schedules.csv')
    hpxml_file = os.path.join(home_path, XML_ADDRESS)
    dwelling_args_local = build_dwelling_args(hpxml_file, filtered_sched_file, weather_file_path, home_path)
    with _dwelling_init_lock:
        base_dwelling = Dwelling(name="HPWH Baseline Peak Probe", **dwelling_args_local)
    for t_base in base_dwelling.sim_times:
        base_ctrl = {"Water Heating": {"Setpoint": TbaselineC, "Deadband": TdeadbandC, "Load Fraction": 1}}
        base_dwelling.update(control_signal=base_ctrl)
    df_base, _, _ = base_dwelling.finalize()
    df_base = remove_first_day(df_base, Start)
    return df_base[["Time", "Water Heating Electric Power (kW)"]]

def measure_fleet_baseline_peak_kw(homes, weather_file_path):
    """
    Runs simulate_home_baseline_only() for every home in parallel, sums
    their Water Heating power at each timestep, and returns the fleet-
    aggregate peak -- the "measure it, don't assume a ratio" quantity
    CAP_KW is derived from.
    """
    dfs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(simulate_home_baseline_only, home, weather_file_path): home
            for home in homes
        }
        for f in concurrent.futures.as_completed(futures):
            try:
                dfs.append(f.result())
            except Exception as e:
                print(f"Baseline-peak probe failed for {futures[f]}: {e}")

    fleet_power = pd.concat(dfs).groupby("Time")["Water Heating Electric Power (kW)"].sum()
    return fleet_power.max()

#########################################
# PHASE 1 -- run each home up through event_end, record temp (UNCHANGED)
#########################################

def simulate_home_phase1(home_path, weather_file_path, event_cfg):
    filtered_sched_file = filter_schedules(home_path)
    hpxml_file = os.path.join(home_path, XML_ADDRESS)

    unit_delay_minutes = get_unit_delay_minutes(home_path)

    dwelling_args_local = build_dwelling_args(hpxml_file, filtered_sched_file, weather_file_path, home_path)

    with _dwelling_init_lock:
        sim_dwelling = Dwelling(name="HPWH Controlled", **dwelling_args_local)
    hpwh_unit = sim_dwelling.get_equipment_by_end_use('Water Heating')

    event_start = pd.to_datetime(f"{EVENT_DATE} {event_cfg['dispatch_time']}")
    event_start = event_start + dt.timedelta(minutes=unit_delay_minutes)
    event_end = event_start + pd.Timedelta(hours=event_cfg['duration'])

    sim_times = list(sim_dwelling.sim_times)
    resume_idx = None
    temp_at_event_end = None
    tank_temp_log = []

    for i, sim_time in enumerate(sim_times):
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
            break  # pause here; the admission-controlled phase resumes from resume_idx

    return {
        "home_path": home_path,
        "sim_dwelling": sim_dwelling,
        "sim_times": sim_times,
        "tank_temp_log": tank_temp_log,
        "resume_idx": resume_idx,
        "unit_delay_minutes": unit_delay_minutes,
        "event_end": event_end,
        "temp_at_event_end": temp_at_event_end,
    }

#########################################
# PRIORITY ORDER
#########################################

def compute_priority_order(phase1_results):
    """
    Ranks units by tank temperature at event_end, warmest first. This is
    PRIORITY ORDER for admission, not a precomputed delay -- the
    admission-control loop below decides actual release timing
    dynamically from real-time fleet power, not from this ranking alone.
    """
    return sorted(phase1_results, key=lambda r: r["temp_at_event_end"], reverse=True)

#########################################
# POWER-CAPPED ADMISSION CONTROL LOOP
#########################################

def run_admission_controlled_recovery(phase1_results, event_cfg):
    """
    Single synchronized lockstep loop across every home's already-
    constructed Dwelling.

    Why single-threaded: deciding whether to admit the next unit requires
    the TRUE fleet-wide aggregate Water Heating Electric Power at THIS
    instant, which only exists if every home is stepped forward in
    lockstep (one timestep at a time, synchronized), not run independently
    to completion in parallel threads. OCHRE's per-timestep compute itself
    is cheap -- Dwelling construction/schedule-loading (Phase 1, already
    parallelized) is what dominated runtime before, not the update() calls
    this loop makes -- so the sequential cost here is modest (~a few
    minutes for a 95-home fleet).

    Mechanics: units are ranked warmest-first (priority order). Once a
    unit reaches its OWN event_end it's eligible and waits in SHED. At
    each timestep, up to max_admissions_per_timestep new units -- the
    next-highest-priority eligible ones still waiting -- are admitted
    (switched to baseline setpoint/deadband, an instant step change,
    staying admitted from then on), gated on the fleet's aggregate WH
    power as of the PREVIOUS completed timestep being below the CURRENT
    cap, CAP_KW (computed in __main__ from CAP_PCT before this is called).
    Otherwise no new unit is admitted this timestep, and the same
    candidate is reconsidered next timestep.

    max_admissions_per_timestep is derived from ADMISSION_RATE_PCT * n,
    not a flat count: however generous the power cap, it still takes at
    least n / rate minutes to clear a fleet of n waiting units -- for
    n=498 at a flat 1/timestep that floor alone (~8.3 hr) was most of the
    remaining simulation window, which is what produced the 20.3%
    never-admitted result even with a correctly fleet-scaled power cap.
    Expressing the rate as a percentage of n (rather than a flat count)
    keeps that floor proportional to fleet size automatically.
    """
    priority_order = compute_priority_order(phase1_results)
    n = len(priority_order)
    max_admissions_per_timestep = max(1, round(ADMISSION_RATE_PCT / 100 * n))

    sim_times = priority_order[0]["sim_times"]
    resume_idx_by_key = {id(r): r["resume_idx"] for r in priority_order}
    global_start_idx = min(resume_idx_by_key.values())

    hpwh_unit_by_key = {id(r): r["sim_dwelling"].get_equipment_by_end_use('Water Heating') for r in priority_order}

    released = {id(r): False for r in priority_order}
    admitted_time = {id(r): None for r in priority_order}
    next_candidate_idx = 0

    aggregate_power_prev = 0.0
    fleet_power_log = []

    for i in range(global_start_idx, len(sim_times)):
        sim_time = sim_times[i]
        current_cap_kw = CAP_KW

        # ---- Admission decision, using the previous timestep's known aggregate ----
        # Up to max_admissions_per_timestep candidates, all gated on the
        # SAME aggregate_power_prev snapshot (we don't have finer-grained
        # feedback within a single timestep -- each admitted unit's actual
        # draw only shows up once its own update() runs, below).
        if aggregate_power_prev < current_cap_kw:
            admitted_this_step = 0
            while admitted_this_step < max_admissions_per_timestep and next_candidate_idx < n:
                candidate = priority_order[next_candidate_idx]
                if i < resume_idx_by_key[id(candidate)]:
                    # This candidate's own SHED period (from its staggered
                    # dispatch) hasn't ended yet -- it isn't eligible yet,
                    # so no further admissions happen this timestep even
                    # though there's headroom.
                    break
                released[id(candidate)] = True
                admitted_time[id(candidate)] = sim_time
                next_candidate_idx += 1
                admitted_this_step += 1

        # ---- Step every home that has reached this instant forward one timestep ----
        for r in priority_order:
            key = id(r)
            if i < resume_idx_by_key[key]:
                # Phase 1 already advanced this home past this instant
                # (its own event_end came later than the fleet minimum).
                continue

            if released[key]:
                control_cmd = {"Water Heating": {"Setpoint": TbaselineC, "Deadband": TdeadbandC, "Load Fraction": 1}}
            else:
                control_cmd = {"Water Heating": {"Setpoint": Tcontrol_SHEDC, "Deadband": Tcontrol_deadbandC, "Load Fraction": 1}}

            r["sim_dwelling"].update(control_signal=control_cmd)
            r["tank_temp_log"].append((sim_time, get_tank_temperature_c(hpwh_unit_by_key[key])))

        # Best-known aggregate as of this instant: every home's most
        # recent electric_kw reading, whether freshly updated this
        # iteration or carried over (only relevant for the first ~25 min
        # of this loop, while later-staggered homes are still catching up).
        aggregate_power_prev = sum(u.electric_kw for u in hpwh_unit_by_key.values())
        fleet_power_log.append((sim_time, aggregate_power_prev, current_cap_kw))

    never_admitted = [os.path.basename(r["home_path"]) for r in priority_order if not released[id(r)]]
    if never_admitted:
        print(f"WARNING: {len(never_admitted)} units never admitted within the simulated window "
              f"(cap schedule too restrictive to clear the fleet in time): {never_admitted}")

    for rank, r in enumerate(priority_order):
        r["priority_rank"] = rank
        r["admitted_time"] = admitted_time[id(r)]

    return priority_order, fleet_power_log

#########################################
# FINALIZE + BASELINE + SAVE -- per home, parallel again
#########################################

def finalize_and_save_home(phase1_result):
    home_path = phase1_result["home_path"]
    sim_dwelling = phase1_result["sim_dwelling"]
    tank_temp_log = phase1_result["tank_temp_log"]

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
# MAIN EXECUTION  -- Phase 0 (baseline peak measurement) -> Phase 1
# (parallel) -> admission control (sequential) -> finalize (parallel)
#########################################

if __name__ == "__main__":
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(WEATHER_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
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

    # ---- Phase 0: measure this fleet's own baseline peak WH power, so
    # CAP_KW can be derived from CAP_PCT instead of hardcoded ----
    print("Measuring fleet's own baseline daily peak WH power (Phase 0)...")
    BASELINE_PEAK_KW = measure_fleet_baseline_peak_kw(homes, WEATHER_FILE)
    CAP_KW = CAP_PCT / 100 * BASELINE_PEAK_KW
    print(f"Measured baseline peak: {BASELINE_PEAK_KW:.3f} kW -> CAP_KW = {CAP_KW:.3f} kW ({CAP_PCT}% of baseline peak)")
    print(f"Admission rate: {ADMISSION_RATE_PCT}% of fleet size")

    # ---- Phase 1: run every home through the reserve event up to
    # event_end, recording each unit's tank temperature at that moment
    # (parallel -- no aggregate visibility needed) ----
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

    print(f"Phase 1 complete for {len(phase1_results)} homes. Starting admission-controlled recovery...")

    # ---- Power-capped admission control, single synchronized lockstep
    # loop across every home ----
    phase1_results, fleet_power_log = run_admission_controlled_recovery(
        phase1_results, reserve_event
    )

    print("Admission-controlled recovery complete. Finalizing homes...")

    # ---- Persist the per-home assignment / outcome summary ----
    release_summary = pd.DataFrame([
        {
            "Home": os.path.basename(r["home_path"]),
            "unit_delay_minutes": r["unit_delay_minutes"],
            "temp_at_event_end_C": r["temp_at_event_end"],
            "temp_at_event_end_F": r["temp_at_event_end"] * 9 / 5 + 32,
            "priority_rank": r["priority_rank"],
            "event_end": r["event_end"],
            "admitted_time": r["admitted_time"],
            "effective_release_delay_minutes": (
                (r["admitted_time"] - r["event_end"]).total_seconds() / 60
                if r["admitted_time"] is not None else None
            ),
        }
        for r in phase1_results
    ])
    release_summary.to_csv(os.path.join(RESULTS_DIR, filename + "_release_summary.csv"), index=False)
    print(f"Release summary written for {len(release_summary)} homes")

    fleet_power_df = pd.DataFrame(fleet_power_log, columns=["Time", "Aggregate_WH_Power_kW", "Cap_kW"])
    fleet_power_df.to_csv(os.path.join(RESULTS_DIR, filename + "_admission_fleet_power.csv"), index=False)
    print(f"Admission-loop fleet power log written ({len(fleet_power_df)} timesteps)")

    # ---- Finalize each home, run its baseline case, and save results
    # (independent per home again -- parallel) ----
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(finalize_and_save_home, r): r["home_path"]
            for r in phase1_results
        }
        for f in concurrent.futures.as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"Finalize/save failed for {futures[f]}: {e}")

    print("All simulations complete!")

    aggregate_results(homes, RESULTS_DIR)
