"""
Excel <-> OCHRE bridge for the FL Reserve Calculator workbook.

RUNS UNDER NATIVE WINDOWS PYTHON, NOT THE WSL VENV. xlwings' Book.caller()
needs live COM access to the running Excel process, which only exists on
Windows -- a WSL/Linux Python can't drive Excel no matter what's
pip-installed into it. Everything the actual OCHRE simulation needs
(OCHRE itself, the HPWH scripts, the input-file datasets) only exists in
this project's WSL venv, so this script bridges the two: it reads/writes
Excel cells directly (Windows side), then shells out to WSL via wsl.exe
to run the simulation, then reads the results back in (both sides can see
the same files, since Windows can read the WSL filesystem over the
\\\\wsl.localhost\\... path this script itself lives on).

Triggered from an Excel button via a VBA macro (RunPython), which calls
main() -- see the VBA snippet in the project notes for the button wiring.
Do not run this file with the WSL venv's python; it will fail to import
xlwings' Windows COM backend.

RU-CF_event (Ramp-Up / LOAD-based reserve) is NOT computed yet -- the
LOAD command path in the simulation has never been validated (only SHED
has). Only RD-CF_event (Ramp-Down / SHED-based) is wired up below; the
RU-CF_event write-back is commented out until the LOAD path exists.
"""

import os
import sys
import json
import shlex
import subprocess
import datetime as dt
import xlwings as xw

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# WSL-side path to this same project -- used only to build the wsl.exe
# bridge command line (the simulation runs there, not on Windows).
# Everything else in this script (reading the workbook, reading results
# back, embedding the plot) uses SCRIPT_DIR instead, which Windows
# resolves transparently whether this file was opened via a \\wsl.localhost
# path or a mapped drive.
WSL_PROJECT_DIR = "/home/othman/projects/othman_neea_flex_load_calculator_26"
WSL_VENV_PYTHON = f"{WSL_PROJECT_DIR}/.venv/bin/python"
WSL_HPWH_DIR = f"{WSL_PROJECT_DIR}/HPWH"


def _run_wsl_script(script_name, env_vars):
    """
    Runs one of the HPWH_*.py scripts inside WSL via wsl.exe, with
    env_vars set inline in the bash command (Windows environment
    variables aren't visible inside WSL without WSLENV configuration, so
    this sets them explicitly instead of relying on that). Raises with
    the script's own stderr on failure so problems surface clearly in
    Excel instead of a bare non-zero exit code.
    """
    assignments = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env_vars.items())
    remote_cmd = f"cd {shlex.quote(WSL_HPWH_DIR)} && {assignments} {shlex.quote(WSL_VENV_PYTHON)} {shlex.quote(script_name)}"

    result = subprocess.run(
        ["wsl.exe", "-e", "bash", "-lc", remote_cmd],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{script_name} failed in WSL (exit {result.returncode}):\n"
            f"{result.stderr[-3000:]}"
        )
    return result


def main():
    wb = xw.Book.caller()
    sht = wb.sheets['Calculator']

    # FLType (I6/M6, currently just holding the "Flex Load Type" header
    # label, not a real selection) is meant to let this calculator target
    # different flex load types -- HPWH, EV Charger, Dryer, etc. -- via a
    # dropdown, with I7 further selecting a specific product/model within
    # that type. Both are planned but not implemented yet: the dropdowns
    # aren't wired up, and this simulation only ever models an HPWH. So
    # there's nothing meaningful to validate FLType against right now.
    # TODO: once the FLType/product dropdowns are live and populated with
    # the category strings the sheet's own formulas already expect (see
    # Calculator!O8, O9, ...), reinstate a check here that FLType ==
    # 'Heat Pump Water Heater' before running -- this simulation can't
    # represent any other flex load type.

    wb.app.status_bar = "Running OCHRE reserve simulation (this can take several minutes)..."

    # ---- Read inputs from Excel ----
    # N38 is a pure time-of-day cell (no date component) -- Excel stores
    # it as a fraction of a day (e.g. 0.625 == 15:00). xlwings' datetime
    # converter (.options(dt.datetime)/(dt.time)) throws on this specific
    # case (tries to build a pre-epoch timestamp internally and fails), so
    # convert the raw fraction ourselves instead of routing through it.
    start_time_frac = sht.range('N38').value  # raw float, fraction of a day
    duration_min = sht.range('N40').value     # Event Duration (min) -- Excel formula result
    timestep_min = sht.range('N41').value     # Timestep (min)

    total_minutes = round(start_time_frac * 24 * 60)
    hh, mm = divmod(total_minutes, 60)
    dispatch_time_str = f"{hh:02d}:{mm:02d}"
    duration_hr = duration_min / 60  # simulation scripts want HOURS, sheet gives MINUTES

    # Distinct filename per run so repeated button-presses don't clobber
    # each other's results.
    run_id = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"ExcelRun_{run_id}"

    env_vars = {
        "OCHRE_FILENAME": filename,
        "OCHRE_DISPATCH_TIME": dispatch_time_str,
        "OCHRE_DURATION_HR": duration_hr,
        "OCHRE_TIMESTEP_MIN": int(timestep_min),
    }

    # ---- Run simulation + analysis pipeline in WSL (file-based handoff,
    # same as manual use -- each script writes to Ready_data/<filename>/
    # and the next one reads it back from there) ----
    _run_wsl_script("HPWH_B3_Reserve_V3.py", env_vars)
    wb.app.status_bar = "Simulation complete. Parsing results..."
    _run_wsl_script("HPWH_C1_parse_OCHRE_data_final.py", env_vars)
    _run_wsl_script("HPWH_C2_Plot_Totpower_WHpower.py", env_vars)
    _run_wsl_script("HPWH_C3_Reserve_Calculations.py", env_vars)

    # ---- Read back the machine-readable summary C3 wrote (Windows can
    # read this directly -- same filesystem, different path spelling) ----
    results_dir = os.path.join(SCRIPT_DIR, "HPWH", "Ready_data", filename)
    summary_path = os.path.join(results_dir, filename + "_summary.json")
    with open(summary_path) as f:
        results = json.load(f)

    # ---- Write results back to Excel ----
    # RD-CF_event (Ramp-Down / SHED reserve) -- what's actually simulated.
    sht.range('N31').value = results['avg_cf_event']

    # RU-CF_event (Ramp-Up / LOAD reserve) -- not yet implemented; the
    # LOAD command path in the simulation is untested. Un-comment once
    # it's validated and the simulation is extended to run both
    # directions per button-press:
    # sht.range('N30').value = results['avg_cf_event_ru']

    # ---- Embed the fleet-power plot C3 already generates ----
    plot_path = os.path.join(results_dir, filename + "_fleet_power_event_plot.png")
    if os.path.exists(plot_path):
        sht.pictures.add(
            plot_path, name='ReservePowerPlot', update=True,
            left=sht.range('R5').left, top=sht.range('R5').top,
            width=300, height=200,
        )

    wb.app.status_bar = f"Done -- RD-CF_event = {results['avg_cf_event']:.4f}"


if __name__ == '__main__':
    # Manual/CLI testing without pressing the Excel button -- attaches to
    # the workbook directly via set_mock_caller() instead of a real
    # RunPython call. MUST be run with native Windows Python (not the WSL
    # venv) -- e.g. from PowerShell:
    #   python excel_ochre.py
    xw.Book(os.path.join(SCRIPT_DIR, 'FL Reserve Calculator.xlsm')).set_mock_caller()
    main()
