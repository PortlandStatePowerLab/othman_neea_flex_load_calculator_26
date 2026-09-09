import sys
from pathlib import Path
import xlwings as xw
import datetime
import runpy
import matplotlib.pyplot as plt


# Paths
base_dir = Path(__file__).resolve().parent
if str(base_dir) not in sys.path:
    sys.path.insert(0, str(base_dir))

# Connect to the open workbook
WS_NAME = "Calculator"
EXCEL_PATH = base_dir / "FL Reserve Calculator.xlsm"

wb = xw.Book(EXCEL_PATH)
sht = wb.sheets[WS_NAME]

# --- Read raw values from Excel ---
raw_dispatch_time = sht.range('N38').value   # likely datetime.time or datetime.datetime
raw_duration_min = sht.range('N40').value    # minutes, e.g. 120

total_minutes = round(raw_dispatch_time * 24 * 60)
hours, minutes = divmod(total_minutes, 60)
dispatch_time_str = f"{hours:02d}:{minutes:02d}"


# --- Convert to match HPWH_B3's expected format ---
# dispatch_time_str is already set above
duration_hr = round(raw_duration_min / 60.0, 4)

reserve_event = {
    'dispatch_time': dispatch_time_str,
    'duration': duration_hr
}

# Paths to simulation scripts
B3_PATH = base_dir / "HPWH" / "HPWH_B3_Reserve_V4.py"
C1_PATH = base_dir / "HPWH" / "HPWH_C1_parse_OCHRE_data_final.py"
# C2_PATH = base_dir / "HPWH" / "HPWH_C2_Plot_Totpower_WHpower.py"
C3_PATH = base_dir / "HPWH" / "HPWH_C3_Reserve_Calculations.py"


# --- Run B3: simulation ---
b3_globals = runpy.run_path(
    str(B3_PATH),
    init_globals={'reserve_event': reserve_event},
    run_name='__main__'
)

# --- Run C1: parse results, using B3's output ---
c1_globals = runpy.run_path(
    str(C1_PATH),
    init_globals=b3_globals,
    run_name='__main__'
)

# --- Run C2 ---
# c2_globals = runpy.run_path(
#     str(C2_PATH),
#     init_globals=c1_globals,
#     run_name='__main__'
# )

# --- Run C3 ---
c3_globals = runpy.run_path(
    str(C3_PATH),
    init_globals=c1_globals,
    run_name='__main__'
)

# c3_globals = runpy.run_path(str(C3_PATH), init_globals=c1_globals)

# --- Send result back to Excel ---
avg_cf = c3_globals['avg_cf']
sht.range('N31').value = avg_cf

# --- Get the figure from C3 ---
photo_file = c3_globals['photo_file']

sht.pictures.add(photo_file, name='CF_Plot', update=True,
                  left=sht.range('R5').left, top=sht.range('R5').top)



# print(f"Plot inserted at R5 from: {photo_file}")
# print(f"avg_cf written to N31: {avg_cf}")
# print("Plot inserted at R5.")
print("Pipeline complete.")
