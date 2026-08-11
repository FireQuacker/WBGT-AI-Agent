import streamlit as st
import pandas as pd
import numpy as np
import requests
import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import matplotlib.pyplot as plt
import seaborn as sns
import plotly.graph_objects as go
from scipy import stats

# ==============================================================================
# STREAMLIT PAGE CONFIGURATION
# ==============================================================================
st.set_page_config(
    page_title="NOAA vs. Open-Meteo Validation Engine",
    page_icon="🌡️",
    layout="wide"
)

# ==============================================================================
# THERMODYNAMIC & PSYCHROMETRIC CALCULATIONS
# ==============================================================================
def stull_wet_bulb(temp_c, rh):
    """Calculates Wet-Bulb Temperature (Twb) in °C using Stull's formula."""
    temp_c = np.array(temp_c, dtype=float)
    rh = np.array(rh, dtype=float)
    twb = (
        temp_c * np.arctan(0.151977 * np.sqrt(rh + 8.313659))
        + np.arctan(temp_c + rh)
        - np.arctan(rh - 1.676331)
        + 0.00391838 * (rh ** 1.5) * np.arctan(0.023101 * rh)
        - 4.686035
    )
    return np.round(twb, 2)

def calculate_shaded_wbgt(temp_c, rh):
    """Calculates Shaded Wet Bulb Globe Temperature (WBGT) in °C."""
    twb = stull_wet_bulb(temp_c, rh)
    wbgt = (0.7 * twb) + (0.3 * np.array(temp_c, dtype=float))
    return np.round(wbgt, 2)

# ==============================================================================
# TIMEZONE & TIME FILTERING HELPERS
# ==============================================================================
def get_timezone_from_coords(lat, lon):
    """Fetches local IANA timezone string based on GPS coordinates."""
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": "2024-01-01", "end_date": "2024-01-01",
        "hourly": "temperature_2m", "timezone": "auto"
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json().get("timezone", "UTC")
    except Exception:
        pass
    return "UTC"

def adjust_lst_to_wall_clock(dt_naive, tz_str):
    """Shifts NOAA LST to Wall-Clock time (handles DST offset)."""
    if pd.isna(dt_naive):
        return pd.NaT
    try:
        tz = ZoneInfo(tz_str)
        localized_test = dt_naive.replace(tzinfo=tz)
        if localized_test.dst() != timedelta(0):
            return dt_naive + timedelta(hours=1)
    except Exception:
        pass
    return dt_naive

# ==============================================================================
# OPEN-METEO DATA FETCH ENGINE
# ==============================================================================
def fetch_open_meteo_data(lat, lon, start_date, end_date, tz_str):
    """Fetches historical hourly parameters from Open-Meteo."""
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start_date, "end_date": end_date,
        "hourly": ["temperature_2m", "relative_humidity_2m", "wind_speed_10m"],
        "wind_speed_unit": "mph", "timezone": tz_str
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            hourly = data.get("hourly", {})
            timestamps = pd.to_datetime(hourly.get("time", []), format="mixed").round("h")
            df = pd.DataFrame({
                "Timestamp": timestamps,
                "OM_Temp_C": hourly.get("temperature_2m", []),
                "OM_RH": hourly.get("relative_humidity_2m", []),
                "OM_Wind_mph": hourly.get("wind_speed_10m", [])
            })
            df = df[df["Timestamp"].dt.hour.between(8, 17)].reset_index(drop=True)
            df["OM_Twb"] = stull_wet_bulb(df["OM_Temp_C"], df["OM_RH"])
            df["OM_WBGT"] = calculate_shaded_wbgt(df["OM_Temp_C"], df["OM_RH"])
            return df
    except Exception:
        pass
    return pd.DataFrame()

# ==============================================================================
# CORE DATA PROCESSING PIPELINE
# ==============================================================================
def process_single_csv(uploaded_file):
    """
    Parses NOAA LCD CSV, applies +/-10 min tie-breaking matching,
    detects C vs F automatically, fetches Open-Meteo, merges, and calculates metrics.
    """
    df_raw = pd.read_csv(uploaded_file, low_memory=False)
    file_name = uploaded_file.name

    lat = df_raw['LATITUDE'].dropna().iloc[0] if 'LATITUDE' in df_raw.columns else np.nan
    lon = df_raw['LONGITUDE'].dropna().iloc[0] if 'LONGITUDE' in df_raw.columns else np.nan

    if pd.isna(lat) or pd.isna(lon):
        return {"error": f"[{file_name}] Missing LATITUDE or LONGITUDE coordinates."}

    station_tz = get_timezone_from_coords(lat, lon)
    
    df = df_raw.copy()
    df['Raw_NOAA_Time'] = pd.to_datetime(df['DATE'], errors='coerce')
    
    df['Temp_Numeric'] = pd.to_numeric(
        df['HourlyDryBulbTemperature'].astype(str).str.extract(r'(-?\d+\.?\d*)')[0], 
        errors='coerce'
    )
    df['RH_Numeric'] = pd.to_numeric(
        df['HourlyRelativeHumidity'].astype(str).str.extract(r'(\d+\.?\d*)')[0], 
        errors='coerce'
    )
    
    if 'HourlyWindSpeed' in df.columns:
        df['Wind_Numeric'] = pd.to_numeric(
            df['HourlyWindSpeed'].astype(str).str.extract(r'(\d+\.?\d*)')[0], 
            errors='coerce'
        )
    else:
        df['Wind_Numeric'] = np.nan

    df = df.dropna(subset=['Raw_NOAA_Time', 'Temp_Numeric']).copy()
    if df.empty:
        return {"error": f"[{file_name}] No valid dry-bulb temperature observations found."}

    # Auto-detect Celsius vs Fahrenheit
    if df['Temp_Numeric'].max() < 55.0:
        df['NOAA_Temp_C'] = df['Temp_Numeric'].round(1)
    else:
        df['NOAA_Temp_C'] = ((df['Temp_Numeric'] - 32) * 5.0 / 9.0).round(1)

    # Shift NOAA LST to Wall-Clock time
    df['Wall_Clock_DT'] = df['Raw_NOAA_Time'].apply(lambda x: adjust_lst_to_wall_clock(x, station_tz))
    
    # Target top of hour
    df['Target_Hour'] = df['Wall_Clock_DT'].dt.round('h')
    
    # Absolute difference in minutes from target hour
    df['Abs_Diff_Min'] = (df['Wall_Clock_DT'] - df['Target_Hour']).dt.total_seconds().abs() / 60.0

    # Filter to +/- 10 minute window
    valid_window = df[df['Abs_Diff_Min'] <= 10.0].copy()

    if valid_window.empty:
        return {"error": f"[{file_name}] No observations fell within +/- 10 minutes of any top of the hour."}

    # Sort to enforce tie-breaking rules:
    # 1. Target_Hour
    # 2. Smaller minute offset (Abs_Diff_Min asc)
    # 3. Earlier timestamp on tie (Wall_Clock_DT asc)
    valid_window = valid_window.sort_values(by=['Target_Hour', 'Abs_Diff_Min', 'Wall_Clock_DT'])
    best_noaa = valid_window.drop_duplicates(subset=['Target_Hour'], keep='first').copy()

    # Filter for daytime work hours (08:00 - 17:00)
    best_noaa['Hour'] = best_noaa['Target_Hour'].dt.hour
    noaa_daytime = best_noaa[(best_noaa['Hour'] >= 8) & (best_noaa['Hour'] <= 17)].copy()

    if noaa_daytime.empty:
        return {"error": f"[{file_name}] No valid NOAA observations matched the 08:00-17:00 work window."}

    noaa_daytime['NOAA_RH'] = noaa_daytime['RH_Numeric'].round(1)
    noaa_daytime['NOAA_Wind_mph'] = noaa_daytime['Wind_Numeric'].round(1)
    noaa_daytime["NOAA_Twb"] = stull_wet_bulb(noaa_daytime["NOAA_Temp_C"], noaa_daytime["NOAA_RH"])
    noaa_daytime["NOAA_WBGT"] = calculate_shaded_wbgt(noaa_daytime["NOAA_Temp_C"], noaa_daytime["NOAA_RH"])

    noaa_daytime = noaa_daytime.rename(columns={'Target_Hour': 'Timestamp'})

    start_date_str = noaa_daytime['Timestamp'].min().strftime('%Y-%m-%d')
    end_date_str = noaa_daytime['Timestamp'].max().strftime('%Y-%m-%d')
    
    om_df = fetch_open_meteo_data(lat, lon, start_date_str, end_date_str, station_tz)
    
    if om_df.empty:
        return {"error": f"[{file_name}] Failed to fetch Open-Meteo data for range {start_date_str} to {end_date_str}."}

    comp_df = pd.merge(
        noaa_daytime[['Timestamp', 'Raw_NOAA_Time', 'NOAA_Temp_C', 'NOAA_RH', 'NOAA_Wind_mph', 'NOAA_Twb', 'NOAA_WBGT']], 
        om_df, 
        on="Timestamp", 
        how="inner"
    )
    
    if comp_df.empty:
        return {"error": f"[{file_name}] No matching timestamps between NOAA and Open-Meteo."}

    # Deltas
    comp_df["Delta_Temp_C_Abs"] = np.abs(comp_df["OM_Temp_C"] - comp_df["NOAA_Temp_C"]).round(2)
    comp_df["Delta_Temp_C_Bias"] = (comp_df["OM_Temp_C"] - comp_df["NOAA_Temp_C"]).round(2)
    comp_df["Delta_WBGT_Abs"] = np.abs(comp_df["OM_WBGT"] - comp_df["NOAA_WBGT"]).round(2)
    
    # Anomaly Flag (> 4.0 °C)
    comp_df["Is_Anomaly"] = comp_df["Delta_Temp_C_Abs"] > 4.0
    anomalies = comp_df[comp_df["Is_Anomaly"]].copy()

    # Statistical Metrics
    noaa_temps = comp_df["NOAA_Temp_C"].dropna()
    om_temps = comp_df["OM_Temp_C"].dropna()
    
    rmse = np.sqrt(np.mean((om_temps - noaa_temps)**2))
    mbe = np.mean(om_temps - noaa_temps)
    
    r_squared = 0
    if len(noaa_temps) > 1:
        slope, intercept, r_value, p_value, std_err = stats.linregress(noaa_temps, om_temps)
        r_squared = r_value ** 2

    comp_df.insert(0, "Source_File", file_name)
    anomalies.insert(0, "Source_File", file_name)

    stats_dict = {
        "File": file_name,
        "Lat": lat,
        "Lon": lon,
        "Timezone": station_tz,
        "Total_Records": len(comp_df),
        "Total_Anomalies_GT_4C": len(anomalies),
        "Mean_Bias_Error_C": round(mbe, 2),
        "RMSE_C": round(rmse, 2),
        "R_Squared": round(r_squared, 4)
    }

    return {
        "success": True,
        "df": comp_df,
        "anomalies": anomalies,
        "stats": stats_dict
    }

def generate_excel_bytes(summary_df, anomalies_df, combined_df):
    """Generates an in-memory Excel file with multiple sheets."""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        summary_df.to_excel(writer, sheet_name='Master_Summary', index=False)
        anomalies_df.to_excel(writer, sheet_name='Anomaly_Ledger', index=False)
        combined_df.to_excel(writer, sheet_name='Full_Merged_Data', index=False)
    return output.getvalue()

# ==============================================================================
# STREAMLIT USER INTERFACE & VISUALIZATIONS
# ==============================================================================
def main():
    st.title("🧪 NOAA vs. Open-Meteo Validation Engine")
    st.markdown(
        "Automated operational validation tool comparing NOAA ground observation stations against Open-Meteo historical reanalysis models. "
        "Evaluates thermal stress metrics ($T_{air}$, $T_{wb}$, $WBGT$) across daytime operational hours (08:00–17:00)."
    )

    tab_single, tab_batch, tab_methodology = st.tabs([
        "📊 Single-Site Deep Dive", 
        "📂 Batch Processor", 
        "📚 Methodology & Technical Reference"
    ])

    # -------------------------------------------------------------------------
    # TAB 1: SINGLE-SITE DEEP DIVE
    # -------------------------------------------------------------------------
    with tab_single:
        st.subheader("Interactive Single Site Analysis")
        single_file = st.file_uploader("Upload a single NOAA LCD CSV", type=["csv"], key="single")

        if single_file is not None:
            with st.spinner("Executing data pipeline..."):
                result = process_single_csv(single_file)
            
            if "error" in result:
                st.error(result["error"])
            else:
                df = result["df"]
                anom = result["anomalies"]
                stats_info = result["stats"]

                st.success(f"Processed **{stats_info['Total_Records']}** valid daytime hours. Station Timezone: `{stats_info['Timezone']}`")

                # Metrics Row
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Anomalies (>4°C)", stats_info['Total_Anomalies_GT_4C'], delta_color="inverse")
                col2.metric("Mean Bias Error (°C)", stats_info['Mean_Bias_Error_C'])
                col3.metric("Root Mean Square Error (°C)", stats_info['RMSE_C'])
                col4.metric("R-Squared", f"{stats_info['R_Squared']:.4f}")

                # Plotly Interactive Time-Series
                st.write("### 📈 Interactive Temperature Comparison")
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=df['Timestamp'], y=df['NOAA_Temp_C'], mode='lines+markers', name='NOAA (LST-Adjusted)', line=dict(color='#1f77b4', width=2)))
                fig.add_trace(go.Scatter(x=df['Timestamp'], y=df['OM_Temp_C'], mode='lines+markers', name='Open-Meteo', line=dict(color='#ff7f0e', dash='dash', width=2)))
                
                if not anom.empty:
                    fig.add_trace(go.Scatter(
                        x=anom['Timestamp'], y=anom['OM_Temp_C'], 
                        mode='markers', name='>4°C Anomaly', 
                        marker=dict(color='red', size=12, symbol='x')
                    ))
                
                fig.update_layout(height=450, xaxis_title="Wall-Clock Timestamp", yaxis_title="Ambient Temperature (°C)", hovermode="x unified", margin=dict(l=0, r=0, t=30, b=0))
                st.plotly_chart(fig, use_container_width=True)

                # Seaborn Error Distribution
                st.write("### 🔔 Systemic Bias & Error Distribution")
                fig_dist, ax_dist = plt.subplots(figsize=(10, 3))
                sns.kdeplot(data=df, x="Delta_Temp_C_Bias", fill=True, color="#2ca02c", ax=ax_dist)
                ax_dist.axvline(0, color='black', linestyle='--')
                ax_dist.set_title("Distribution of Open-Meteo Bias Error (Positive = OM runs hotter than NOAA)")
                ax_dist.set_xlabel("Temperature Difference (°C)")
                st.pyplot(fig_dist)

                # Anomaly Ledger
                if not anom.empty:
                    st.error("### 🚨 Critical Anomaly Ledger (>4°C Absolute Deviation)")
                    display_cols = ["Timestamp", "Raw_NOAA_Time", "NOAA_Temp_C", "OM_Temp_C", "Delta_Temp_C_Abs"]
                    st.dataframe(anom[display_cols].sort_values("Delta_Temp_C_Abs", ascending=False), use_container_width=True)
                else:
                    st.success("No anomalies >4°C detected in this dataset.")

                # Excel Download
                excel_bytes = generate_excel_bytes(pd.DataFrame([stats_info]), anom, df)
                st.download_button(
                    label="⬇️ Download Full Excel Report",
                    data=excel_bytes,
                    file_name=f"Validation_Report_{single_file.name.replace('.csv', '')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

    # -------------------------------------------------------------------------
    # TAB 2: BATCH PROCESSOR
    # -------------------------------------------------------------------------
    with tab_batch:
        st.subheader("Multi-Site Batch Processing")
        batch_files = st.file_uploader("Upload multiple NOAA LCD CSVs", type=["csv"], accept_multiple_files=True, key="batch")

        if batch_files and st.button("Run Batch Pipeline"):
            all_dfs = []
            all_anomalies = []
            all_stats = []
            
            progress_bar = st.progress(0)
            status_text = st.empty()

            for idx, file in enumerate(batch_files):
                status_text.text(f"Processing ({idx+1}/{len(batch_files)}): {file.name}")
                res = process_single_csv(file)
                
                if "error" in res:
                    st.warning(res["error"])
                else:
                    all_dfs.append(res["df"])
                    all_anomalies.append(res["anomalies"])
                    all_stats.append(res["stats"])
                
                progress_bar.progress((idx + 1) / len(batch_files))
            
            status_text.text("Batch processing complete!")
            
            if all_stats:
                master_summary = pd.DataFrame(all_stats)
                master_df = pd.concat(all_dfs, ignore_index=True)
                master_anomalies = pd.concat(all_anomalies, ignore_index=True) if all_anomalies else pd.DataFrame()
                
                st.write("### 📋 Master Site Summary")
                st.dataframe(master_summary, use_container_width=True)

                if not master_anomalies.empty:
                    st.write(f"### 🚨 Combined Anomaly Ledger ({len(master_anomalies)} Total Flags)")
                    st.dataframe(master_anomalies[["Source_File", "Timestamp", "NOAA_Temp_C", "OM_Temp_C", "Delta_Temp_C_Abs"]], use_container_width=True)
                
                excel_bytes = generate_excel_bytes(master_summary, master_anomalies, master_df)
                st.download_button(
                    label="⬇️ Download Master Batch Excel Report",
                    data=excel_bytes,
                    file_name=f"Master_Batch_Validation_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary"
                )

    # -------------------------------------------------------------------------
    # TAB 3: METHODOLOGY & TECHNICAL REFERENCE
    # -------------------------------------------------------------------------
    with tab_methodology:
        st.markdown("""
        ## 📚 Methodology & Technical Reference

        ### 1. Purpose & Core Functionality
        This validation engine cross-analyzes ground-based weather observations from the **NOAA Local Climatological Data (LCD)** network against **Open-Meteo historical reanalysis models**. 

        Its primary function is to audit model accuracy for occupational heat stress monitoring, evaluating dry-bulb ambient temperature ($T_{air}$), relative humidity ($RH$), and calculated Wet Bulb Globe Temperature ($WBGT$).

        ---

        ### 2. Data Alignment Pipeline

        #### A. Timezone & Daylight Saving Alignment
        NOAA LCD files record observations in **Local Standard Time (LST)** year-round. To ensure accurate pairing with local wall-clock hours during Daylight Saving Time (DST):
        * The station's IANA timezone is resolved dynamically using GPS coordinates (`LATITUDE`, `LONGITUDE`).
        * Timestamps occurring during active DST (e.g., Eastern Daylight Time / EDT) are shifted forward by $+1\\text{ hour}$ to align with local wall-clock activity.

        #### B. Top-of-Hour Matching & Tie-Breaking Rule
        NOAA weather stations issue readings at irregular off-hour timestamps (e.g., `07:55`, `08:05`, `08:12`). To map observations to Open-Meteo's hourly grid:
        1. Observations are bounded within a strict **$\\pm 10$-minute window** around each target hour ($HH:00$).
        2. **Offset Rule:** If multiple readings exist within the window, the observation closest to $HH:00$ is selected.
        3. **Tie-Breaking Rule:** If two observations share the exact same minute offset (e.g., `07:55` and `08:05`), the pipeline deterministically selects the **earlier timestamp** (`07:55`).

        #### C. Automatic Unit Detection
        To prevent unit conversion errors across varying NOAA export formats:
        * The engine inspects the maximum temperature value ($T_{\\text{max}}$) in the file.
        * If $T_{\\text{max}} < 55.0$, the file is automatically treated as **Celsius (°C)** and no formula is applied.
        * If $T_{\\text{max}} \\ge 55.0$, the dataset is converted from **Fahrenheit (°F)** using:
          $$T_{\\text{C}} = (T_{\\text{F}} - 32) \\times \\frac{5}{9}$$

        #### D. Operational Window Truncation
        Comparisons are restricted strictly to **daytime work shifts (08:00 to 17:00 local time)**. Outer hours and non-matching dates are automatically excluded.

        ---

        ### 3. Psychrometric & Heat Stress Formulas

        #### Wet-Bulb Temperature ($T_{wb}$) — Stull's Equation
        Wet-bulb temperature is calculated using Stull’s empirical psychrometric approximation (valid for $RH$ between $5\\%$ and $99\\%$ and temperatures between $-20^\\circ\\text{C}$ and $50^\\circ\\text{C}$):
        
        $$T_{wb} = T \\cdot \\arctan\\left(0.151977 \\sqrt{RH + 8.313659}\\right) + \\arctan(T + RH) - \\arctan(RH - 1.676331) + 0.00391838 \\cdot RH^{1.5} \\cdot \\arctan(0.023101 \\cdot RH) - 4.686035$$

        #### Shaded Wet-Bulb Globe Temperature ($WBGT_{\\text{shaded}}$)
        In indoor or shaded outdoor occupational settings without direct solar radiation, $WBGT$ is calculated as a weighted ratio:
        
        $$WBGT_{\\text{shaded}} = (0.7 \\times T_{wb}) + (0.3 \\times T_{air})$$

        ---

        ### 4. Statistical Validation Metrics

        * **Mean Bias Error (MBE):** Measures systematic over- or under-estimation by Open-Meteo relative to NOAA ($MBE > 0$ indicates Open-Meteo runs warmer):
          $$MBE = \\frac{1}{n} \\sum_{i=1}^{n} (T_{\\text{OM}, i} - T_{\\text{NOAA}, i})$$

        * **Root Mean Square Error (RMSE):** Measures overall magnitude of model variance:
          $$RMSE = \\sqrt{\\frac{1}{n} \\sum_{i=1}^{n} (T_{\\text{OM}, i} - T_{\\text{NOAA}, i})^2}$$

        * **Coefficient of Determination ($R^2$):** Quantifies the proportion of variance shared between NOAA and Open-Meteo datasets.

        * **Critical Anomaly Flag:** Highlights any hourly record where the absolute temperature deviation exceeds $4.0^\\circ\\text{C}$ ($|T_{\\text{OM}} - T_{\\text{NOAA}}| > 4.0^\\circ\\text{C}$).

        ---

        ### 5. Data Variable Glossary

        | Variable Name | Unit | Description |
        | :--- | :--- | :--- |
        | `Timestamp` | `YYYY-MM-DD HH:00` | Target top-of-hour timestamp in local wall-clock time. |
        | `Raw_NOAA_Time` | `YYYY-MM-DD HH:MM` | Original observation timestamp from NOAA LCD file (LST). |
        | `NOAA_Temp_C` | °C | Dry-bulb ambient air temperature observed by NOAA station. |
        | `OM_Temp_C` | °C | 2-meter air temperature modeled by Open-Meteo. |
        | `NOAA_RH` / `OM_RH` | % | Relative humidity from NOAA ground station and Open-Meteo model. |
        | `NOAA_Twb` / `OM_Twb` | °C | Calculated wet-bulb temperature using Stull's equation. |
        | `NOAA_WBGT` / `OM_WBGT` | °C | Calculated shaded Wet-Bulb Globe Temperature. |
        | `Delta_Temp_C_Abs` | °C | Absolute deviation: $\|T_{\\text{OM}} - T_{\\text{NOAA}}\|$. |
        | `Delta_Temp_C_Bias` | °C | Directional error: $T_{\\text{OM}} - T_{\\text{NOAA}}$ (positive = OM hotter). |
        | `Is_Anomaly` | Boolean | `True` if absolute temperature difference $> 4.0^\\circ\\text{C}$. |
        """)

if __name__ == "__main__":
    main()
