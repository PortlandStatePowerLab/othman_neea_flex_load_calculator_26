"""
#Author: Othman
#Modified for Reserve Service calculations

#Calculates aggregated fleet power, Coincidence Factor (CF), and per-unit
#response time classification for a Reserve Service dispatch event.
#Reads the aggregated baseline/controlled CSVs produced by the Reserve
#Service simulation script (aggregate_results output).
"""

import pandas as pd
import os
import json
import datetime as dt
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import os
from run_context import load_filename


#########################################
# PATH SETUP (same convention as plotting script)
#########################################

working_dir = os.path.dirname(os.path.abspath(__file__))

# OCHRE_FILENAME overrides this when set (used by excel_ochre.py); unset,
# inherit whichever B3 version last ran (see run_context.py) so this
# doesn't need its own name kept in sync by hand -- falling back to the
# hardcoded default only if no B3 run has ever recorded one yet.
input_file_root = os.environ.get('OCHRE_FILENAME') or load_filename('HPWH_AdmissionControl_n600_testx8_cap8_rerankLive')

input_file_name1 = input_file_root + "_baseline"
input_file_name2 = input_file_root + "_controlled"

# Simulation script (B3) now writes its aggregated CSVs directly into this
# per-test folder instead of working_dir, so read from the same place.
results_dir = os.path.join(working_dir, "Ready_data", input_file_root)
input_file_1 = os.path.join(results_dir, input_file_name1 + ".csv")
input_file_2 = os.path.join(results_dir, input_file_name2 + ".csv")

#########################################
# RESERVE EVENT SETTINGS
#########################################
# NOTE: these must match the values used in the simulation script that
# produced input_file_1 / input_file_2 (reserve_event['dispatch_time'],
# reserve_event['duration']). They are kept separate here since this is
# a standalone analysis script, not shared state with the simulation run.

DISPATCH_DATE = '2018-01-12'   # date portion for dispatch_t (Start + 1 day, since day 1 is removed)
# OCHRE_DISPATCH_TIME / OCHRE_DURATION_HR override these when set (see
# excel_ochre.py) -- unset, manual runs use the same defaults as before.
# These MUST match whatever B3 actually used for this run's data.

DISPATCH_TIME = os.environ.get('OCHRE_DISPATCH_TIME', '15:00')
EVENT_DURATION_HR = float(os.environ.get('OCHRE_DURATION_HR', 1.5))

# DISPATCH_TIME = reserve_event['dispatch_time']
# EVENT_DURATION_HR = reserve_event['duration']


dispatch_t = pd.to_datetime(f"{DISPATCH_DATE} {DISPATCH_TIME}")
event_end = dispatch_t + pd.Timedelta(hours=EVENT_DURATION_HR)


#########################################
# FLEET / CF SETTINGS
#########################################

# Uniform Pmax across the homogeneous fleet (kW), per simplicity principle.
# Adjust to match the nameplate/rated power of the HPWH units simulated.
# PMAX_KW = 5.5
PMAX_KW = None  # to be set from observed max, see below

# Power column used for aggregation and CF calculation
# POWER_COL = "Total Electric Power (kW)"
POWER_COL = "Water Heating Electric Power (kW)"

# Water heater power column used for response detection / online state
# WH_POWER_COL = "Water Heating Electric Power (kW)"
WH_POWER_COL = "Water Heating Electric Power (kW)"
#########################################
# RESPONSE DETECTION SETTINGS
#########################################

# Minimum |controlled - baseline| power divergence (kW) to count as a
# unit having "responded" to the dispatch signal.
RESPONSE_THRESHOLD_KW = 0.1

# Minimum baseline WH power (kW) at dispatch_t to consider a unit
# "Online" (actively heating) at the moment of dispatch, vs "Offline" (idle).
ONLINE_THRESHOLD_KW = 0.1

# Response time bins (minutes), per Reserve tier windows
# RESPONSE_BINS = [(0, 5), (6, 10), (11, 30)]
RESPONSE_BINS = [(0, 10), (11, 30)]

#########################################
# LOAD DATA
#########################################

def load_data():
    df_base = pd.read_csv(input_file_1)
    df_ctrl = pd.read_csv(input_file_2)

    df_base['Time'] = pd.to_datetime(df_base['Time'], errors='coerce')
    df_ctrl['Time'] = pd.to_datetime(df_ctrl['Time'], errors='coerce')

    return df_base, df_ctrl

#########################################
# AGGREGATED FLEET POWER + CF
#########################################

def compute_fleet_power_and_cf(df_ctrl):
    """
    Computes fleet-aggregated power P_fleet(t) and CF(t) over the
    dispatch event window, per CF(t) = sum(individual demand(t)) / (Pmax * n).
    """
    n_units = df_ctrl['Home'].nunique()

    event_df = df_ctrl[(df_ctrl['Time'] >= dispatch_t) & (df_ctrl['Time'] < event_end)]

    fleet_power = event_df.groupby('Time')[POWER_COL].sum().rename('P_fleet_kW')
    cf_series = fleet_power / (PMAX_KW * n_units)
    cf_series = cf_series.rename('CF_event')

    avg_fleet_power = fleet_power.mean() if not fleet_power.empty else float('nan')
    avg_cf = cf_series.mean() if not cf_series.empty else float('nan')

    return n_units, fleet_power, cf_series, avg_fleet_power, avg_cf

#########################################
# PER-UNIT RESPONSE TIME + STATE AT DISPATCH
#########################################

def compute_response_metrics(df_base, df_ctrl):
    """
    For each home:
      - state_at_dispatch: Online/Offline, from baseline WH power at dispatch_t
      - response_time_i: dispatch_signal_i - dispatch_t (minutes), where
        dispatch_signal_i is the first timestamp >= dispatch_t where
        |controlled - baseline| WH power exceeds RESPONSE_THRESHOLD_KW
        Units that never diverge within the event window are marked
        as "No Response".
    """
    results = []

    for home_id, ctrl_group in df_ctrl.groupby('Home'):
        base_group = df_base[df_base['Home'] == home_id]

        # --- State at dispatch (from baseline) ---
        base_at_dispatch = base_group[base_group['Time'] <= dispatch_t]
        if base_at_dispatch.empty:
            state = "Unknown"
        else:
            last_row = base_at_dispatch.sort_values('Time').iloc[-1]
            base_power_at_dispatch = last_row[WH_POWER_COL]
            state = "Online" if base_power_at_dispatch > ONLINE_THRESHOLD_KW else "Offline"

        # --- Response time detection ---
        merged = pd.merge(
            ctrl_group[['Time', WH_POWER_COL]],
            base_group[['Time', WH_POWER_COL]],
            on='Time', suffixes=('_ctrl', '_base')
        )
        merged = merged[(merged['Time'] >= dispatch_t) & (merged['Time'] < event_end)]
        merged['power_diff'] = (merged[f"{WH_POWER_COL}_ctrl"] - merged[f"{WH_POWER_COL}_base"]).abs()

        responded = merged[merged['power_diff'] > RESPONSE_THRESHOLD_KW]

        if responded.empty:
            response_time_min = None
        else:
            dispatch_signal_i = responded.sort_values('Time').iloc[0]['Time']
            response_time_min = (dispatch_signal_i - dispatch_t).total_seconds() / 60

        results.append({
            'Home': home_id,
            'state_at_dispatch': state,
            'response_time_min': response_time_min
        })

    return pd.DataFrame(results)

#########################################
# POST-EVENT SPIKE / PEAK / DURATION METRICS
#########################################

def compute_spike_metrics(df_base, df_ctrl, event_end):
    """
    Compares the controlled fleet's post-event cold-load-pickup power
    against what the fleet's own baseline (no-dispatch) power would have
    been over the SAME post-event window -- an apples-to-apples "what did
    the admission control actually buy us" comparison, not a comparison
    to some other day's peak.
    """
    n_units = df_ctrl['Home'].nunique()

    base_fleet = df_base.groupby('Time')[WH_POWER_COL].sum()
    ctrl_fleet = df_ctrl.groupby('Time')[WH_POWER_COL].sum()

    base_post = base_fleet[base_fleet.index >= event_end]
    ctrl_post = ctrl_fleet[ctrl_fleet.index >= event_end]

    peak_ctrl_kw = ctrl_post.max()
    peak_ctrl_time = ctrl_post.idxmax()
    peak_base_kw = base_post.max()

    per_unit_peak_w = peak_ctrl_kw / n_units * 1000
    peak_reduction_pct = (peak_base_kw - peak_ctrl_kw) / peak_base_kw * 100
    time_of_peak_min = (peak_ctrl_time - event_end).total_seconds() / 60

    # Duration: how long the controlled fleet stays above the baseline's
    # own peak in this window -- captures spike WIDTH, not just height.
    minutes_above_baseline_peak = int((ctrl_post > peak_base_kw).sum())

    # Excess energy: integral of (controlled - baseline) power over the
    # post-event window, positive part only -- total extra cold-load-
    # pickup energy, not just the instantaneous peak.
    aligned = pd.concat([ctrl_post.rename('ctrl'), base_post.rename('base')], axis=1).dropna()
    dt_hours = (aligned.index[1] - aligned.index[0]).total_seconds() / 3600 if len(aligned) > 1 else 1 / 60
    excess_energy_kwh = ((aligned['ctrl'] - aligned['base']).clip(lower=0) * dt_hours).sum()

    return {
        'peak_ctrl_kw': peak_ctrl_kw,
        'peak_ctrl_time': peak_ctrl_time,
        'per_unit_peak_w': per_unit_peak_w,
        'peak_base_kw': peak_base_kw,
        'peak_reduction_pct': peak_reduction_pct,
        'time_of_peak_min': time_of_peak_min,
        'minutes_above_baseline_peak': minutes_above_baseline_peak,
        'excess_energy_kwh': excess_energy_kwh,
    }

#########################################
# ADMISSION / RECOVERY COUNTS
#########################################

def compute_admission_summary(release_summary_df, n_units):
    """
    Counts units admitted (recovered back to baseline control) vs. never
    admitted within the simulated window, from the _release_summary.csv
    written by the admission-control simulation (admitted_time is null
    for a unit that never got released).
    """
    n_admitted = int(release_summary_df['admitted_time'].notna().sum())
    n_not_recovered = n_units - n_admitted
    return {
        'n_admitted': n_admitted,
        'n_not_recovered': n_not_recovered,
        'pct_admitted': n_admitted / n_units * 100,
        'pct_not_recovered': n_not_recovered / n_units * 100,
    }

#########################################
# BINNING / SUMMARY
#########################################

def classify_bin(response_time_min):
    if response_time_min is None:
        return "No Response"
    for lo, hi in RESPONSE_BINS:
        if lo <= response_time_min <= hi:
            return f"{lo}-{hi} min"
    return ">30 min"

def print_summary(n_units, avg_fleet_power, avg_cf, response_df, spike_metrics, admission_summary):
    response_df = response_df.copy()
    response_df['bin'] = response_df['response_time_min'].apply(classify_bin)

    print("=" * 50)
    print("RESERVE SERVICE EVENT SUMMARY")
    print("=" * 50)
    print(f"Dispatch time: {dispatch_t}")
    print(f"Event end:     {event_end}")
    print(f"Number of aggregated units (n): {n_units}")
    print(f"Average aggregated fleet power during event: {avg_fleet_power:.3f} kW")
    print(f"Average Coincidence Factor (CF_event): {avg_cf:.4f}")
    print()

    print("--- Post-event spike / peak ---")
    print(f"  Peak controlled fleet WH power: {spike_metrics['peak_ctrl_kw']:.3f} kW "
          f"({spike_metrics['per_unit_peak_w']:.1f} W/unit)")
    print(f"  Peak baseline fleet WH power (same post-event window): {spike_metrics['peak_base_kw']:.3f} kW")
    print(f"  Peak reduction vs. baseline: {spike_metrics['peak_reduction_pct']:.1f}%")
    print(f"  Time of peak: {spike_metrics['peak_ctrl_time']} "
          f"({spike_metrics['time_of_peak_min']:.1f} min after event end)")
    print()

    print("--- Spike shape / duration ---")
    print(f"  Minutes controlled power stays above baseline's peak: {spike_metrics['minutes_above_baseline_peak']} min")
    print(f"  Excess energy (controlled - baseline, post-event): {spike_metrics['excess_energy_kwh']:.3f} kWh")
    print()

    if admission_summary is not None:
        print("--- Recovery / admission ---")
        print(f"  Admitted & recovered: {admission_summary['n_admitted']} / {n_units} "
              f"({admission_summary['pct_admitted']:.1f}%)")
        print(f"  Not recovered:        {admission_summary['n_not_recovered']} / {n_units} "
              f"({admission_summary['pct_not_recovered']:.1f}%)")
        print()

    print("--- Units responded per time bin ---")
    bin_order = [f"{lo}-{hi} min" for lo, hi in RESPONSE_BINS] + [">30 min", "No Response"]
    bin_counts = response_df['bin'].value_counts().reindex(bin_order, fill_value=0)
    for bin_label, count in bin_counts.items():
        print(f"  {bin_label}: {count}")
    print()

    print("--- Online / Offline state at dispatch, per response bin ---")
    cross_tab = pd.crosstab(response_df['bin'], response_df['state_at_dispatch'])
    cross_tab = cross_tab.reindex(bin_order, fill_value=0)
    print(cross_tab)
    print("=" * 50)

#########################################
# MAIN EXECUTION
#########################################

# df_base, df_ctrl = load_data()

# n_units, fleet_power, cf_series, avg_fleet_power, avg_cf = compute_fleet_power_and_cf(df_ctrl)

df_base, df_ctrl = load_data()

# Set Pmax from the actual observed maximum water heater draw across the
# baseline fleet, rather than an assumed nameplate value.
PMAX_KW = df_base[WH_POWER_COL].max()
print(f"Pmax (observed max Water Heating Electric Power): {PMAX_KW:.4f} kW")

n_units, fleet_power, cf_series, avg_fleet_power, avg_cf = compute_fleet_power_and_cf(df_ctrl)

response_df = compute_response_metrics(df_base, df_ctrl)

spike_metrics = compute_spike_metrics(df_base, df_ctrl, event_end)

release_summary_file = os.path.join(results_dir, input_file_root + "_release_summary.csv")
if os.path.exists(release_summary_file):
    release_summary_df = pd.read_csv(release_summary_file, parse_dates=['admitted_time'])
    admission_summary = compute_admission_summary(release_summary_df, n_units)
else:
    admission_summary = None

print_summary(n_units, avg_fleet_power, avg_cf, response_df, spike_metrics, admission_summary)

# Save per-unit response detail and fleet power/CF time series for reference
output_dir = results_dir
os.makedirs(output_dir, exist_ok=True)

response_df.to_csv(os.path.join(output_dir, input_file_root + "_response_metrics.csv"), index=False)
pd.concat([fleet_power, cf_series], axis=1).to_csv(
    os.path.join(output_dir, input_file_root + "_fleet_power_cf.csv")
)
print(f"\nDetail files saved to: {output_dir}")

# Machine-readable summary for excel_ochre.py (or any other caller) to
# read back without parsing console output. avg_cf here is RD-CF_event
# (Ramp-Down reserve, i.e. SHED) -- this script has no notion of RU-CF
# (Ramp-Up / LOAD reserve) since B3 only ever simulates one
# RESERVE_COMMAND per run, and that's SHED throughout this project.
summary_out = {
    "n_units": int(n_units),
    "avg_fleet_power_kw": float(avg_fleet_power),
    "avg_cf_event": float(avg_cf),
    "peak_ctrl_kw": float(spike_metrics['peak_ctrl_kw']),
    "peak_base_kw": float(spike_metrics['peak_base_kw']),
    "peak_reduction_pct": float(spike_metrics['peak_reduction_pct']),
    "excess_energy_kwh": float(spike_metrics['excess_energy_kwh']),
    "pct_admitted": admission_summary['pct_admitted'] if admission_summary else None,
}
with open(os.path.join(output_dir, input_file_root + "_summary.json"), "w") as f:
    json.dump(summary_out, f, indent=2)
print(f"Summary JSON written to: {os.path.join(output_dir, input_file_root + '_summary.json')}")



#########################################
# PLOT: FLEET POWER WITH EVENT WINDOW MARKED
#########################################

def plot_fleet_power_with_event(df_base, df_ctrl, working_dir, input_file_root):
    """
    Plots aggregated fleet power (baseline vs controlled) over the full
    simulation window, with the reserve dispatch event window marked by
    vertical lines and a shaded region -- similar in convention to
    Bass & Fylling's Reserve Service simulation figure (base case vs
    test case, service period highlighted).
    Saves the figure in the same folder as the other plots.
    """
    # Same folder as the other saved plots
    output_dir = os.path.join(working_dir, "Ready_data", input_file_root)
    os.makedirs(output_dir, exist_ok=True)
    photo_file = os.path.join(output_dir, input_file_root + "_fleet_power_event_plot.png")

    base_fleet = df_base.groupby('Time')[POWER_COL].sum().rename('P_fleet_base')
    ctrl_fleet = df_ctrl.groupby('Time')[POWER_COL].sum().rename('P_fleet_ctrl')

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(base_fleet.index, base_fleet.values, label='Base case (no dispatch)',
            color='blue', linestyle='-')
    ax.plot(ctrl_fleet.index, ctrl_fleet.values, label='Test case (dispatched)',
            color='orange', linestyle='--')

    # --- Mark the reserve event window ---
    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch (dispatch_t)')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')
    ax.axvspan(dispatch_t, event_end, color='red', alpha=0.1)

    ax.set_title('Aggregated Fleet Power During Reserve Service Event')
    ax.set_xlabel('Time')
    ax.set_ylabel('Power (kW)')
    # ax.legend()
    ax.legend(loc='upper left')

    #ax.xaxis.set_major_locator(ticker.MaxNLocator(nbins=8))
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    #fig.autofmt_xdate()


    # plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    # print(f"Plot saved to: {photo_file}")

    plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    print(f"Plot saved to: {photo_file}")
    return photo_file

# plot_fleet_power_with_event(df_base, df_ctrl, working_dir, input_file_root)

photo_file = plot_fleet_power_with_event(df_base, df_ctrl, working_dir, input_file_root)
