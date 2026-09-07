"""
Author: Thomas Metzler
Updated: 7/9/2026

Adjusts HPWH properties in the XML file that OCHRE will read.
Updated to dynamically convert ERWH, Natural Gas, and Tankless units to HPWH.
"""

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
import re
import random  # Added for the distribution function

# ---------------------------------------------------------
# DIRECTORY SETUP
# ---------------------------------------------------------
WORKING_DIR = Path(__file__).resolve().parent

INPUT_DIR = WORKING_DIR / "All Portland Input Files"
OUTPUT_DIR = WORKING_DIR / "HPWH All Portland Input Files"

# Dataset(s) that were already converted to HPWH by an earlier run of this
# script (WaterHeaterType already says 'heat pump water heater', so
# convert_to_HPWH()'s storage/instantaneous check no longer matches them)
# and therefore need fix_hpwh_location() applied directly, in place --
# convert_to_HPWH()'s own Location fix only fires during the initial
# conversion. HPWH_B3_Reserve_V2.py's Input_folder selects which of these
# it actually simulates.
LOCATION_FIX_TARGET_DIRS = [
    WORKING_DIR / "HPWH 50 Input Files",
    WORKING_DIR / "HPWH All Portland Input Files",
    WORKING_DIR / "HPWH All Input Files",
]

# ---------------------------------------------------------
# CONFIGURATIONS
# ---------------------------------------------------------
HPWH_SIZE_CONFIG = {
    "HPWH_size": {
        # Current Volume : {"TankVolume": New Volume, "HeatingCapacity": New Capacity (BTU/hr)}
        50.0: {"TankVolume": 66.0, "HeatingCapacity": 7203.0, "UniformEnergyFactor": 3.95},
        66.0: {"TankVolume": 80.0, "HeatingCapacity": 7334.0, "UniformEnergyFactor": 3.98},
        80.0: {"TankVolume": 80.0, "HeatingCapacity": 7334.0, "UniformEnergyFactor": 3.98}
    },
}

HPWH_CONVERSION_CONFIG = {
    "HPWH_Conversion": {
        "FuelType": "electricity",
        "NewType": "heat pump water heater",
        "TankVolume": "80.0",
        "HeatingCapacity": "7334.0",
        "UniformEnergyFactor": "3.98",
        "BackupHeatingCapacity": "15355.0",
        "HPWHOperatingMode": "hybrid/auto",
        "UsageBin": "medium",
        # A HPWH's compressor loses capacity/COP in cold ambient air, unlike
        # the gas/ERWH units this dataset originally specified -- garages
        # and unconditioned basements run ~40-45F in winter here, which
        # measurably hurt post-event recovery (28/95 units never recovered
        # within the simulation; unconditioned/garage units failed to
        # recover 2-2.4x more often than conditioned-space units). Since
        # this dataset is standing in for real HPWH installations, site the
        # converted unit somewhere a HPWH would actually be installed.
        "Location": "conditioned space",
        "ElementsToRemove": [
            "RecoveryEfficiency",
            "EnergyFactor",
            "PerformanceAdjustment",
            "extension"
        ]
    }
}

# WaterHeatingSystem/Location values (HPXML vocabulary) that are too cold/
# unconditioned to be an appropriate real-world HPWH siting.
UNCONDITIONED_WH_LOCATIONS = {
    "garage",
    "basement - unconditioned",
    "attic - unconditioned",
    "attic - vented",
    "attic - unvented",
    "crawlspace - unconditioned",
    "crawlspace - vented",
    "crawlspace - unvented",
    "other exterior",
    "outside",
}

#Adjust weights for each size to be randomly distributed
HPWH_SIZE_DISTRIB_CONFIG = {
    "HPWH_size_distrib": [
        {"weight": 0.50, "TankVolume": 50.0, "HeatingCapacity": 6887.0, "UniformEnergyFactor": 3.78},
        {"weight": 0.20, "TankVolume": 66.0, "HeatingCapacity": 7203.0, "UniformEnergyFactor": 3.95},
        {"weight": 0.30, "TankVolume": 80.0, "HeatingCapacity": 7334.0, "UniformEnergyFactor": 3.98}
    ]
}

HPWH_MODEL_CONFIG = {
    "HPWH_model": [
        # AOSmith HPTU-50N
        {"TankVolume": 46.0, "HeatingCapacity": 1391, "UniformEnergyFactor": 3.45, "BackupHeatingCapacity": 15345.0}
    ]
}


# ---------------------------------------------------------
# MODIFIER FUNCTIONS
# ---------------------------------------------------------
def update_ERWH_size(root, config):
    """Updates the size of existing ERWH systems based on original size."""
    ns_match = re.match(r'\{.*\}', root.tag)
    ns_bracket = ns_match.group(0) if ns_match else ''

    for elem in root.iter():
        if elem.tag.split('}')[-1] == 'WaterHeatingSystem':
            vol_elem = None
            cap_elem = None
            ef_elem = None
            
            for child in elem:
                tag_name = child.tag.split('}')[-1]
                if tag_name == 'TankVolume':
                    vol_elem = child
                elif tag_name == 'HeatingCapacity':
                    cap_elem = child
                elif tag_name == 'UniformEnergyFactor':
                    ef_elem = child
            
            if vol_elem is not None and vol_elem.text:
                try:
                    current_vol = float(vol_elem.text.strip())
                except ValueError:
                    continue
                
                if current_vol in config["HPWH_size"]:
                    updates = config["HPWH_size"][current_vol]
                    
                    # 1. Update Tank Volume
                    vol_elem.text = str(updates["TankVolume"])
                    
                    # 2. Update Heating Capacity
                    if cap_elem is not None:
                        cap_elem.text = str(updates["HeatingCapacity"])
                    else:
                        new_cap_elem = ET.Element(f'{ns_bracket}HeatingCapacity')
                        new_cap_elem.text = str(updates["HeatingCapacity"])
                        idx = list(elem).index(vol_elem)
                        elem.insert(idx + 1, new_cap_elem)

                    # 3. Update Energy Factor
                    if ef_elem is not None:
                        ef_elem.text = str(updates["UniformEnergyFactor"])
                    else:
                        new_ef_elem = ET.Element(f'{ns_bracket}UniformEnergyFactor')
                        new_ef_elem.text = str(updates["UniformEnergyFactor"])
                        idx = list(elem).index(vol_elem)
                        elem.insert(idx + 2, new_ef_elem)

def distribute_HPWH_size(root, config):
    """Updates HPWH size based on a weighted random distribution."""
    ns_match = re.match(r'\{.*\}', root.tag)
    ns_bracket = ns_match.group(0) if ns_match else ''

    dist_data = config["HPWH_size_distrib"]
    # Extract the weights to feed into the random choice
    weights = [item["weight"] for item in dist_data]

    for elem in root.iter():
        if elem.tag.split('}')[-1] == 'WaterHeatingSystem':
            vol_elem = None
            cap_elem = None
            ef_elem = None
            
            for child in elem:
                tag_name = child.tag.split('}')[-1]
                if tag_name == 'TankVolume':
                    vol_elem = child
                elif tag_name == 'HeatingCapacity':
                    cap_elem = child
                elif tag_name == 'UniformEnergyFactor':
                    ef_elem = child
            
            # As long as there is an existing water heater to update
            if vol_elem is not None and vol_elem.text:
                # Select a new configuration based on the defined weights
                chosen_update = random.choices(dist_data, weights=weights, k=1)[0]
                
                # 1. Update Tank Volume
                vol_elem.text = str(chosen_update["TankVolume"])
                
                # 2. Update Heating Capacity
                if cap_elem is not None:
                    cap_elem.text = str(chosen_update["HeatingCapacity"])
                else:
                    new_cap_elem = ET.Element(f'{ns_bracket}HeatingCapacity')
                    new_cap_elem.text = str(chosen_update["HeatingCapacity"])
                    idx = list(elem).index(vol_elem)
                    elem.insert(idx + 1, new_cap_elem)
                
                # 3. Update Energy Factor
                if ef_elem is not None:
                    ef_elem.text = str(chosen_update["UniformEnergyFactor"])
                else:
                    new_ef_elem = ET.Element(f'{ns_bracket}UniformEnergyFactor')
                    new_ef_elem.text = str(chosen_update["UniformEnergyFactor"])
                    idx = list(elem).index(vol_elem)
                    elem.insert(idx + 2, new_ef_elem)

def convert_to_HPWH(root, config):
    """Converts ERWH, Natural Gas, and Tankless heaters to an HPWH."""
    ns_match = re.match(r'\{.*\}', root.tag)
    ns_bracket = ns_match.group(0) if ns_match else ''
    
    conv_data = config["HPWH_Conversion"]

    for elem in root.iter():
        if elem.tag.split('}')[-1] == 'WaterHeatingSystem':
            type_elem = None
            fuel_elem = None
            loc_elem = None

            # Locate base identifying elements
            for child in elem:
                tag_name = child.tag.split('}')[-1]
                if tag_name == 'WaterHeaterType':
                    type_elem = child
                elif tag_name == 'FuelType':
                    fuel_elem = child
                elif tag_name == 'Location':
                    loc_elem = child

            # Check if it is a storage (ERWH/Gas) or instantaneous (Tankless) heater
            if type_elem is not None and type_elem.text in ['storage water heater', 'instantaneous water heater']:

                # 1. Update Water Heater Type and Fuel Type
                type_elem.text = conv_data["NewType"]
                if fuel_elem is not None:
                    fuel_elem.text = conv_data["FuelType"]

                # 1b. Relocate out of an unconditioned space -- the original
                # equipment's siting doesn't necessarily suit a HPWH
                if loc_elem is not None and loc_elem.text in UNCONDITIONED_WH_LOCATIONS:
                    loc_elem.text = conv_data["Location"]

                # 2. Remove conflicting elements
                to_remove = [child for child in elem if child.tag.split('}')[-1] in conv_data["ElementsToRemove"]]
                for child in to_remove:
                    elem.remove(child)
                    
                # 3. Add or update HPWH specific elements in correct schema order
                # We anchor around FractionDHWLoadServed to maintain valid XML sequences
                fraction_elem = next((c for c in elem if c.tag.split('}')[-1] == 'FractionDHWLoadServed'), None)
                
                # Schema insertion order: (Tag Name, Value, Anchor Element, Insert After Anchor?)
                updates = [
                    ("TankVolume", conv_data["TankVolume"], fraction_elem, False), 
                    ("HeatingCapacity", conv_data["HeatingCapacity"], fraction_elem, True), 
                    ("BackupHeatingCapacity", conv_data["BackupHeatingCapacity"], fraction_elem, True),
                    ("UniformEnergyFactor", conv_data["UniformEnergyFactor"], fraction_elem, True),
                    ("HPWHOperatingMode", conv_data["HPWHOperatingMode"], fraction_elem, True),
                    ("UsageBin", conv_data["UsageBin"], fraction_elem, True)
                ]

                # Tracks our moving target for schema placement
                current_anchor = fraction_elem

                for tag, value, anchor, insert_after in updates:
                    existing = next((c for c in elem if c.tag.split('}')[-1] == tag), None)
                    
                    # Update if it exists
                    if existing is not None:
                        existing.text = str(value)
                        if insert_after:
                            current_anchor = existing
                    # Create and place if missing
                    else:
                        new_elem = ET.Element(f'{ns_bracket}{tag}')
                        new_elem.text = str(value)
                        
                        if anchor is not None and current_anchor in list(elem):
                            idx = list(elem).index(current_anchor if insert_after else anchor)
                            insert_pos = idx + 1 if insert_after else idx
                            elem.insert(insert_pos, new_elem)
                            if insert_after:
                                current_anchor = new_elem
                        else:
                            # Fallback if anchor is totally missing from file
                            elem.append(new_elem)

def fix_hpwh_location(root, config):
    """
    Relocates ALREADY-CONVERTED HPWH units (WaterHeaterType == 'heat pump
    water heater') out of an unconditioned space. convert_to_HPWH() only
    updates Location while converting a storage/instantaneous heater; a
    dataset that's already been converted (WaterHeaterType already says
    'heat pump water heater') is invisible to that check, so this is a
    separate pass meant to be run directly against an already-converted
    dataset.
    """
    conv_data = config["HPWH_Conversion"]

    for elem in root.iter():
        if elem.tag.split('}')[-1] == 'WaterHeatingSystem':
            type_elem = None
            loc_elem = None
            for child in elem:
                tag_name = child.tag.split('}')[-1]
                if tag_name == 'WaterHeaterType':
                    type_elem = child
                elif tag_name == 'Location':
                    loc_elem = child

            if (type_elem is not None and type_elem.text == 'heat pump water heater'
                    and loc_elem is not None and loc_elem.text in UNCONDITIONED_WH_LOCATIONS):
                loc_elem.text = conv_data["Location"]

def convert_single_model(root, config):
    """Converts all HPWH to a single model."""
    ns_match = re.match(r'\{.*\}', root.tag)
    ns_bracket = ns_match.group(0) if ns_match else ''
    
    # Access the first item in the list
    model_data = config["HPWH_model"][0]

    for elem in root.iter():
        if elem.tag.split('}')[-1] == 'WaterHeatingSystem':
            type_elem = None
            cap_elem = None
            ef_elem = None
            vol_elem = None
            backheat_elem = None
            
            # Locate identifying elements using exact match
            for child in elem:
                tag_name = child.tag.split('}')[-1]
                if tag_name == 'TankVolume':
                    vol_elem = child
                elif tag_name == 'HeatingCapacity':
                    cap_elem = child
                elif tag_name == 'UniformEnergyFactor':
                    ef_elem = child
                elif tag_name == 'BackupHeatingCapacity':
                    backheat_elem = child
            
            if vol_elem is not None and vol_elem.text:
                try:
                    current_vol = float(vol_elem.text.strip())
                except ValueError:
                    continue
                
                # 1. Update Tank Volume
                vol_elem.text = str(model_data["TankVolume"])
                    
                # 2. Update Heating Capacity
                if cap_elem is not None:
                    cap_elem.text = str(model_data["HeatingCapacity"])
                else:
                    new_cap_elem = ET.Element(f'{ns_bracket}HeatingCapacity')
                    new_cap_elem.text = str(model_data["HeatingCapacity"])
                    idx = list(elem).index(vol_elem)
                    elem.insert(idx + 1, new_cap_elem)

                # 3. Update Energy Factor
                if ef_elem is not None:
                    ef_elem.text = str(model_data["UniformEnergyFactor"])
                else:
                    new_ef_elem = ET.Element(f'{ns_bracket}UniformEnergyFactor')
                    new_ef_elem.text = str(model_data["UniformEnergyFactor"])
                    idx = list(elem).index(vol_elem)
                    elem.insert(idx + 2, new_ef_elem)

                # 4. Update Backup Heating
                if backheat_elem is not None:
                    backheat_elem.text = str(model_data["BackupHeatingCapacity"])
                else:
                    new_backheat_elem = ET.Element(f'{ns_bracket}BackupHeatingCapacity')
                    new_backheat_elem.text = str(model_data["BackupHeatingCapacity"])
                    idx = list(elem).index(vol_elem)
                    elem.insert(idx + 3, new_backheat_elem)

# ---------------------------------------------------------
# DUPLICATION LOGIC
# ---------------------------------------------------------
def duplicate_directories(input_dir, output_dir):
    """Safely copies the entire directory structure over."""
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    
    if not input_path.exists():
        print(f"Error: Could not find input directory at {input_path.resolve()}")
        return False

    print(f"Copying files from '{input_path.name}' to '{output_path.name}'...")
    if output_path.exists():
        shutil.copytree(input_path, output_path, dirs_exist_ok=True)
    else:
        shutil.copytree(input_path, output_path)
    return True

# ---------------------------------------------------------
# MAIN EXECUTION BLOCK
# ---------------------------------------------------------
if __name__ == "__main__":
    print("Starting OCHRE HPXML batch update...")
    
    # 1. Duplicate the directory once
    success = duplicate_directories(INPUT_DIR, OUTPUT_DIR)
    
    if success:
        # 2. Iterate through the newly created output directory
        output_path = Path(OUTPUT_DIR)
        
        for xml_file in output_path.rglob('*.xml'):
            
            try:
                # Dynamically register namespaces
                for event, (prefix, uri) in ET.iterparse(xml_file, events=['start-ns']):
                    ET.register_namespace(prefix, uri)
                
                tree = ET.parse(xml_file)
                root = tree.getroot()
                
                # ==========================================
                # TURN YOUR UPDATES ON OR OFF HERE
                # ==========================================
                
                # update_ERWH_size(root, HPWH_SIZE_CONFIG)
                convert_to_HPWH(root, HPWH_CONVERSION_CONFIG)
                # distribute_HPWH_size(root, HPWH_SIZE_DISTRIB_CONFIG)
                # convert_single_model(root, HPWH_MODEL_CONFIG)
                
                # ==========================================
                
                # Apply pretty-print formatting to the tree
                if hasattr(ET, 'indent'):
                    ET.indent(tree, space="  ", level=0)

                # Write changes back to the duplicated file
                tree.write(xml_file, encoding='UTF-8', xml_declaration=True)
                
            except ET.ParseError as e:
                print(f"Failed to parse XML for {xml_file}: {e}")
            except Exception as e:
                print(f"An error occurred while processing {xml_file}: {e}")

        print("Batch update complete.")

    # 3. Relocate already-converted HPWH units sitting in an unconditioned
    # space, directly in place, in any dataset(s) that were converted
    # before this fix existed.
    for target_dir in LOCATION_FIX_TARGET_DIRS:
        if not target_dir.exists():
            print(f"[location fix] Skipping {target_dir} (not found)")
            continue

        print(f"[location fix] Scanning {target_dir} ...")
        changed = unchanged = errors = 0
        for xml_file in target_dir.rglob('home.xml'):
            try:
                for event, (prefix, uri) in ET.iterparse(xml_file, events=['start-ns']):
                    ET.register_namespace(prefix, uri)
                tree = ET.parse(xml_file)
                root = tree.getroot()

                before_loc = next(
                    (c.text for e in root.iter() if e.tag.split('}')[-1] == 'WaterHeatingSystem'
                     for c in e if c.tag.split('}')[-1] == 'Location'), None)

                fix_hpwh_location(root, HPWH_CONVERSION_CONFIG)

                after_loc = next(
                    (c.text for e in root.iter() if e.tag.split('}')[-1] == 'WaterHeatingSystem'
                     for c in e if c.tag.split('}')[-1] == 'Location'), None)

                if before_loc != after_loc:
                    if hasattr(ET, 'indent'):
                        ET.indent(tree, space="  ", level=0)
                    tree.write(xml_file, encoding='UTF-8', xml_declaration=True)
                    changed += 1
                else:
                    unchanged += 1
            except Exception as e:
                print(f"[location fix] Error on {xml_file}: {e}")
                errors += 1

        print(f"[location fix] {target_dir.name}: changed={changed} unchanged={unchanged} errors={errors}")