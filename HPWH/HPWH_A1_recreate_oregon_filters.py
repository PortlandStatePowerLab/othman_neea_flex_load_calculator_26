# -*- coding: utf-8 -*-
"""
Recreate HPWH_OR_50gal.csv, HPWH_OR_66gal.csv, HPWH_OR_80gal.csv, and HPWH_OR_All.csv from
OR_upgrade9.csv.

Recovered filter logic:
  - upgrade == 9
  - in.state == "OR"
  - in.city == "OR, Portland"
  - in.geometry_building_type_recs == "Single-Family Detached"
  - split by upgrade.water_heater_efficiency:
      Electric Heat Pump, 50 gal, 3.78 UEF     -> HPWH_OR_50gal.csv
      Electric Heat Pump, 66 gal, 3.95 UEF     -> HPWH_OR_66gal.csv
      Electric Heat Pump, 80 gal, 3.98 UEF     -> HPWH_OR_80gal.csv
  - all three files combined                   -> HPWH_OR_All.csv

This script preserves the original master-file row order and columns.


Authors Jeff Dinsmore & Thomas Metzler 6/17/2026
"""

from pathlib import Path
import pandas as pd
import os

working_dir = os.path.dirname(os.path.abspath(__file__))


#-------------------------------------------------------
#metadata file name
input_file = "OR_upgrade9.csv"

#filtered file save folder
save_folder = "HPWH Filtered"
#-------------------------------------------------------

SOURCE_FILE = Path(
        os.path.join(
        working_dir,
        "Metadata",
        input_file
    )
)

OUTPUT_DIR = Path(
        os.path.join(
        working_dir,
        save_folder
    )
)

#EXCLUDE_BLDG_IDS = {11875, 234402, 433735}
EXCLUDE_BLDG_IDS = {}

# EXPECTED_COUNTS = {
#     "HPWH_OR_50gal.csv": 434,
#     "HPWH_OR_66gal.csv": 145,
#     "HPWH_OR_80gal.csv": 39,
#     "HPWH_OR_All.csv": 618,
# }


def recreate_files(source_file: Path = SOURCE_FILE, output_dir: Path = OUTPUT_DIR) -> None:
    if not source_file.exists():
        raise FileNotFoundError(f"Source file not found: {source_file}")

    output_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(source_file, low_memory=False)

    required_columns = [
        "bldg_id",
        "upgrade",
        "in.state",
        "in.city",
        "in.geometry_building_type_recs",
        "upgrade.water_heater_efficiency",
        "out.params.size_water_heater..gal"
    ]
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    base_filter_50 = (
        (df["upgrade"] == 9)
        & (df["in.state"] == "OR")
        & (df["in.city"] == "OR, Portland")
        & (df["in.geometry_building_type_recs"] == "Single-Family Detached")
        & (df["out.params.size_water_heater..gal"] == 50)
        & (~df["bldg_id"].isin(EXCLUDE_BLDG_IDS))
    )
    
    df[base_filter_50].to_csv(output_dir / 'HPWH_OR_50gal.csv', index=False)

    base_filter_66 = (
        (df["upgrade"] == 9)
        & (df["in.state"] == "OR")
        & (df["in.city"] == "OR, Portland")
        & (df["in.geometry_building_type_recs"] == "Single-Family Detached")
        & (df["out.params.size_water_heater..gal"] == 66)
        & (~df["bldg_id"].isin(EXCLUDE_BLDG_IDS))
    )
    df[base_filter_66].to_csv (output_dir / 'HPWH_OR_66gal.csv', index=False)


    base_filter_80 = (
        (df["upgrade"] == 9)
        & (df["in.state"] == "OR")
        & (df["in.city"] == "OR, Portland")
        & (df["in.geometry_building_type_recs"] == "Single-Family Detached")
        & (df["out.params.size_water_heater..gal"] == 80)
        & (~df["bldg_id"].isin(EXCLUDE_BLDG_IDS))
    )
    df[base_filter_80].to_csv (output_dir / 'HPWH_OR_80gal.csv', index=False)

    base_filter_all = (
        (df["upgrade"] == 9)
        & (df["in.state"] == "OR")
        & (df["in.city"] == "OR, Portland")
        & (df["in.geometry_building_type_recs"] == "Single-Family Detached")
        & (~df["bldg_id"].isin(EXCLUDE_BLDG_IDS))
    )
    df[base_filter_all].to_csv (output_dir / 'HPWH_OR_All.csv', index=False)


if __name__ == "__main__":
    recreate_files()
