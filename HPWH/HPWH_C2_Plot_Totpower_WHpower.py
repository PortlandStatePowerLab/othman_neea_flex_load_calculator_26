"""
#Author: Thomas Metzler
#6/22/2026

#Creates plots for the average water heater and total household power consumption, comparing baseline and controlled
#Works for HPWH 
"""


import pandas as pd
import os
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import matplotlib.dates as mdates
from run_context import load_filename


#Copy path naming from HPWH_parse_OCHRE_data_final.py for consistency
working_dir = os.path.dirname(os.path.abspath(__file__))

#input_file_root = 'HPWH_Test_Loadshape_9'
# OCHRE_FILENAME overrides this when set (used by excel_ochre.py); unset,
# inherit whichever B3 version last ran (see run_context.py) so this
# doesn't need its own name kept in sync by hand -- falling back to the
# hardcoded default only if no B3 run has ever recorded one yet.
input_file_root = os.environ.get('OCHRE_FILENAME') or load_filename('HPWH_AdmissionControl_n95_testx8_cap8bins')

input_file_name1 = input_file_root + "_baseline"
input_file_name2 = input_file_root + "_controlled"

# Simulation script (B3) now writes its aggregated CSVs directly into this
# per-test folder instead of working_dir, so read from the same place.
folder_path = os.path.join(working_dir, "Ready_data", input_file_root)
input_file_1  = os.path.join(folder_path, input_file_name1 +".csv")
input_file_2  = os.path.join(folder_path, input_file_name2 +".csv")

output_append_WHpower = "_WH_power"
output_file_name1 = input_file_name1 + output_append_WHpower + ".csv"
output_file_name2 = input_file_name2 + output_append_WHpower + ".csv"
output_file_1 = os.path.join(folder_path, output_file_name1)
output_file_2 = os.path.join(folder_path, output_file_name2)

output_append_totpower = "_total_power"
output_file_name3 = input_file_name1 + output_append_totpower + ".csv"
output_file_name4 = input_file_name2 + output_append_totpower + ".csv"
output_file_3 = os.path.join(folder_path, output_file_name3)
output_file_4 = os.path.join(folder_path, output_file_name4)

photo_file_1 = os.path.join(folder_path, input_file_root + "_WH_power_plot.png")
photo_file_2 = os.path.join(folder_path, input_file_root + "_Total_power_plot.png")


#Saves the average of each column as a new row
def save_avg(file):
    # 1. Read the CSV file into a DataFrame
    df = pd.read_csv(file)

    # 2. Calculate the mean for numeric columns
    averages = df.mean(numeric_only=True)

    # 3. Append the averages as a new row (using 'Average' as the row label/index)
    df.loc['Average'] = averages

    # 4. Save back to a CSV file
    df.to_csv(file, index=False)

#plot the data and save the plot
def plot_data(baseline_file, controlled_file, title, photo_file, ax):
    # Load the CSV file
    df_base = pd.read_csv(baseline_file, index_col=0)  # Assuming the first column is the index
    df_con= pd.read_csv(controlled_file, index_col=0)  # Assuming the first column is the index

    # Extract the first row (index 0) and last row (index -1)
    # .iloc[[0, -1]] selects both rows into a new DataFrame
    subset_df_base = df_base.iloc[[0, -1]]
    subset_df_con = df_con.iloc[[0, -1]]

    #transpose data to have time as columns and power as rows
    transposed_df_base = subset_df_base.iloc[1,:]
    transposed_df_con = subset_df_con.iloc[1,:]
    
    #Switch to dataframe and reset index to have time as a column
    df_base = pd.DataFrame (transposed_df_base)
    df_con = pd.DataFrame (transposed_df_con)
    
    df_base.index = df_base.index.str.replace('Home', 'Time')
    df_con.index = df_con.index.str.replace('Home', 'Time')

    df_base = df_base.reset_index ()
    # change columns names:
    df_base.columns = ['Time', 'baseline']

    df_con = df_con.reset_index ()
    # change columns names:
    df_con.columns = ['Time', 'controlled']

    # convert time column to a usable datetime fomat
    # df_base['Time'] = pd.to_datetime(df_base['Time'], errors='coerce').dt.strftime('%H:%M')
    # df_con['Time'] = pd.to_datetime(df_con['Time'], errors='coerce').dt.strftime('%H:%M')

    df_base['Time'] = pd.to_datetime(df_base['Time'], errors='coerce')
    df_con['Time'] = pd.to_datetime(df_con['Time'], errors='coerce')

    #plot
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot (df_base['Time'], df_base['baseline'], label='baseline', color='blue', linestyle='-')
    ax.plot (df_con['Time'], df_con['controlled'], label='controlled', color='orange', linestyle='--')

    # Customize and display
    ax.set_title(title)
    ax.set_xlabel('Time')
    ax.set_ylabel('Power (kW)')
    ax.legend() # Displays labels properly
    # ax.set_xlim(0, 24)
    # ax.set_xticks([0,6,12,18,24])

    # ax.xaxis.set_major_locator(ticker.MaxNLocator(nbins=13))  # Limit ticks
        # Customize and display
    ax.set_title(title)
    ax.set_xlabel('Time')
    ax.set_ylabel('Power (kW)')
    ax.legend() # Displays labels properly

    # ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))


    plt.savefig(photo_file, dpi=300, bbox_inches='tight')  # Save the figure

save_avg(output_file_1)
save_avg(output_file_2)
plot_data(output_file_1, output_file_2, 'Average Power Consumption per Water Heater', photo_file_1, "ax1")

save_avg(output_file_3)
save_avg(output_file_4)
plot_data(output_file_3, output_file_4, 'Average Total Power Consumption per Household', photo_file_2, "ax2")

#Show plot at the end so it doesn't overwrite the previous plot
#plt.show()

