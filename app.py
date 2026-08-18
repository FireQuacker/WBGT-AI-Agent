import os
import subprocess
import streamlit as st
import time
import math
import io
import requests
import urllib.parse
from datetime import datetime, date, timedelta
from playwright.sync_api import sync_playwright, expect
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np
import pandas as pd
import openpyxl
from openpyxl.drawing.image import Image as OpenPyxlImage
import pytz
from timezonefinder import TimezoneFinder

# =====================================================================
# ONE-TIME PLAYWRIGHT INSTALLER (PREVENTS RE-RUN LAG)
# =====================================================================
@st.cache_resource
def install_browser_engine():
    try:
        subprocess.run(["pip", "install", "tzdata"], check=True)
        subprocess.run(["playwright", "install", "chromium"], check=True)
    except Exception as e:
        st.error(f"Background browser engine initialization warning: {e}")

install_browser_engine()

# =====================================================================
# STREAMLIT CONFIGURATION & PERSISTENCE STATE
# =====================================================================
st.set_page_config(page_title="OSHA-WBGT Localized Calculator", layout="wide")

def reset_app_state():
    """Resets all relevant session state keys for a fresh run."""
    keys_to_reset = [
        "step", "pending_geo", "final_hourly_rows", "raw_weather_debug",
        "raw_noaa_df_export", "worker_weight", "fallback_active",
        "location_fallback", "is_forecast", "use_caf", "caf_value",
        "caf_label", "standard_choice", "location_meta", "results"
    ]
    is_forecast = st.session_state.get('is_forecast', False)
    
    for key in keys_to_reset:
        if key in st.session_state:
            del st.session_state[key]
    
    st.session_state.step = 1
    st.session_state.pending_geo = None
    st.session_state.final_hourly_rows = None
    st.session_state.raw_weather_debug = None
    st.session_state.raw_noaa_df_export = None
    st.session_state.worker_weight = 154.0
    st.session_state.fallback_active = False
    st.session_state.location_fallback = False
    st.session_state.is_forecast = is_forecast
    st.session_state.use_caf = False
    st.session_state.caf_value = 0.0
    st.session_state.caf_label = "Standard Work Clothes (0.0 °F)"
    st.session_state.standard_choice = "NIOSH (Default)"
    st.session_state.location_meta = {}

if "step" not in st.session_state:
    reset_app_state()

# =====================================================================
# GEOCODING & METEOROLOGICAL UTILITIES
# =====================================================================
@st.cache_resource
def get_timezone_finder():
    return TimezoneFinder()

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(R * c, 2)

def get_tz_offset_from_coords(lat: float, lon: float, target_date: date) -> str:
    tf = get_timezone_finder()
    tz_name = tf.timezone_at(lng=lon, lat=lat)
    if not tz_name: return str(round(lon / 15))
    try:
        timezone = pytz.timezone(tz_name)
        dt_object = timezone.localize(datetime(target_date.year, target_date.month, target_date.day, 12))
        return str(int(dt_object.utcoffset().total_seconds() / 3600))
    except pytz.UnknownTimeZoneError:
        return str(round(lon / 15))

def geocode_address_native(address: str, mapbox_key: str = None) -> dict:
    try:
        census_url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
        census_params = {"address": address, "benchmark": "Public_AR_Current", "format": "json"}
        census_response = requests.get(census_url, params=census_params, timeout=10)
        if census_response.status_code == 200:
            data = census_response.json()
            matches = data.get("result", {}).get("addressMatches", [])
            if matches:
                coords = matches[0]["coordinates"]
                return {"latitude": coords["y"], "longitude": coords["x"], "matched_address": matches[0].get("matchedAddress", address)}
    except Exception: pass
    if not mapbox_key: return {"error": "US Census geocoder failed. A Mapbox API key is required as a fallback."}
    try:
        encoded_address = urllib.parse.quote(address)
        mapbox_url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{encoded_address}.json"
        mapbox_params = {"access_token": mapbox_key, "limit": 1}
        mapbox_response = requests.get(mapbox_url, params=mapbox_params, timeout=10)
        if mapbox_response.status_code == 200:
            data = mapbox_response.json()
            if data.get("features"):
                coords = data["features"][0]["center"]
                return {"latitude": coords[1], "longitude": coords[0], "matched_address": data["features"][0].get("place_name", address)}
        return {"error": "Mapbox geocoding failed to resolve the address."}
    except Exception as e: return {"error": f"Mapbox Fallback Error: {str(e)}"}

def fetch_weather_native(lat: float, lon: float, date_str: str, is_forecast: bool) -> dict:
    url = "https://api.open-meteo.com/v1/forecast" if is_forecast else "https://archive-api.open-meteo.com/v1/archive"
    params = {"latitude": lat, "longitude": lon, "start_date": date_str, "end_date": date_str, "hourly": ["temperature_2m", "relative_humidity_2m", "surface_pressure", "wind_speed_10m"], "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto"}
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code != 200: return {"error": f"Weather API Error (HTTP {response.status_code})."}
        data = response.json()
        return {"hourly": data.get("hourly", {}), "grid_latitude": data.get("latitude", lat), "grid_longitude": data.get("longitude", lon), "raw_debug": data}
    except Exception as e: return {"error": f"Weather System Error: {str(e)}"}

def process_weather_noaa_csv(uploaded_file, target_date, start_hour, end_hour):
    try:
        df = pd.read_csv(uploaded_file, dtype=str)
    except Exception as e: return {"error": f"Failed to read CSV file: {str(e)}"}
    if 'DATE' not in df.columns: return {"error": "Invalid CSV: 'DATE' column not found."}

    lat = float(df['LATITUDE'].iloc[0]) if 'LATITUDE' in df.columns and pd.notna(df['LATITUDE'].iloc[0]) else 0.0
    lon = float(df['LONGITUDE'].iloc[0]) if 'LONGITUDE' in df.columns and pd.notna(df['LONGITUDE'].iloc[0]) else 0.0
    station_name = df['NAME'].iloc[0] if 'NAME' in df.columns and pd.notna(df['NAME'].iloc[0]) else "NOAA CSV Station"

    tf = get_timezone_finder()
    tz_name = tf.timezone_at(lng=lon, lat=lat)
    if not tz_name: return {"error": f"Could not determine timezone for station: {station_name}."}
    local_tz = pytz.timezone(tz_name)

    df['DATE_raw'] = pd.to_datetime(df['DATE'], errors='coerce')
    df.dropna(subset=['DATE_raw'], inplace=True)
    df['DATE_parsed'] = df['DATE_raw'].apply(lambda x: local_tz.localize(x, is_dst=None))

    target_dt_start = local_tz.localize(datetime.combine(target_date, datetime.min.time()))
    df_day = df[(df['DATE_parsed'] >= (target_dt_start - timedelta(hours=2))) & (df['DATE_parsed'] <= (target_dt_start + timedelta(days=1, hours=2)))].copy()
    if df_day.empty: return {"error": f"No data in CSV for {target_date.strftime('%Y-%m-%d')}."}

    def clean_numeric(val):
        if pd.isna(val): return None
        val_str = str(val).strip().replace('*','').replace('s','').replace('V','')
        try: return float(''.join(c for c in val_str if c.isdigit() or c in '.-'))
        except (ValueError, TypeError): return None
            
    hourly_records = {}
    for hr in range(start_hour, end_hour + 1):
        target_time = target_dt_start + timedelta(hours=hr)
        window = df_day[(df_day['DATE_parsed'] >= (target_time - timedelta(minutes=10))) & (df_day['DATE_parsed'] <= (target_time + timedelta(minutes=10)))].copy()
        
        if not window.empty:
            window['time_diff'] = abs((window['DATE_parsed'] - target_time).dt.total_seconds())
            best_row = window.sort_values(by='time_diff').iloc[0]
            raw_t, rh, ws, pres = [clean_numeric(best_row.get(col)) for col in ['HourlyDryBulbTemperature', 'HourlyRelativeHumidity', 'HourlyWindSpeed', 'HourlyStationPressure']]
            notes = []
            if any(v is None for v in [raw_t, rh, ws]): notes.append("Key variables missing.")
            
            temp = round((raw_t * 1.8) + 32, 1) if raw_t is not None and raw_t < 45.0 else raw_t
            if raw_t is not None and raw_t < 45.0: notes.append(f"Temp {raw_t}°C conv to {temp}°F")
            final_pres = pres if pres is not None else 29.92
            if pres is None: notes.append("Pressure missing; assumed 29.92 inHg")
            
            hourly_records[hr] = {"temperature_f": temp, "relative_humidity_percent": rh, "wind_speed_mph": ws, "barometric_pressure_inhg": final_pres, "calculator_time": best_row['DATE_parsed'].strftime('%H:%M'), "note_additions": " | ".join(notes), "skip_calc": any(v is None for v in [raw_t, rh, ws])}
        else:
            hourly_records[hr] = {"temperature_f": None, "relative_humidity_percent": None, "wind_speed_mph": None, "barometric_pressure_inhg": 29.92, "calculator_time": f"{hr:02d}:00", "note_additions": "No data in window.", "skip_calc": True}
    return {"hourly_records": hourly_records, "latitude": lat, "longitude": lon, "station_name": station_name}

def calculate_wbgt_meteorological_fallback(temp_f, rh_pct, wind_mph, hour_24h=12, is_sun=True):
    tc = (temp_f - 32) * 5.0 / 9.0; rh = rh_pct
    tw = (tc * math.atan(0.151977 * (rh + 8.313766)**0.5) + math.atan(tc + rh) - math.atan(rh - 1.676331) + 0.00391838 * (rh**1.5) * math.atan(0.023101 * rh) - 4.686035)
    tg_c = tc + 1.0
    if is_sun:
        wind_ms = max(wind_mph * 0.44704, 0.1)
        solar_rad = 850.0 * math.sin(math.pi * (hour_24h - 6) / 12.0) if 6 <= hour_24h <= 18 else 0.0
        tg_c = tc + 0.015 * solar_rad - 0.12 * wind_ms
        if tg_c < tc: tg_c = tc + 0.5
    wbgt_c = (0.7 * tw + 0.2 * tg_c + 0.1 * tc) if is_sun else (0.7 * tw + 0.3 * tg_c)
    return round((wbgt_c * 1.8) + 32, 1)

def process_hourly_result(hour_data, data_source_label, standard_choice, fallback_mode=False):
    is_acgih = "ACGIH" in standard_choice
    limit_key, alert_key = ("ACGIH_TLV_F", "ACGIH_AL_F") if is_acgih else ("NIOSH_REL_F", "NIOSH_RAL_F")
    limit_name, alert_name = ("TLV", "AL") if is_acgih else ("REL", "RAL")

    if hour_data.get("skip_calc", False):
        row = {k: "N/A" for k in ["Air_Temp_F", "Humidity_Pct", "Wind_Speed_mph", "Barometric_Pressure_inHg", "Sun_WBGT_F", "Shade_WBGT_F", limit_key, alert_key]}
        row.update({"Date": hour_data["date_string_final"], "Time": hour_data["time_display"], "Workload": hour_data["workload_label"], "Adjusted_Watts": hour_data["final_watts"], "Safety_Status": "Data Missing", "Weather_Data_Source": data_source_label, "Notes": hour_data.get("note_additions", "Key variables missing.")})
        return row

    temp, rh, ws, pres = [hour_data.get(k) for k in ['temperature_f', 'relative_humidity_percent', 'wind_speed_mph', 'barometric_pressure_inhg']]
    notes = [hour_data.get("note_additions", "")]
    if st.session_state.location_fallback: notes.append("City/State/Zip used")
    if st.session_state.use_caf: notes.append(f"CAF Applied: {st.session_state.caf_label}")

    if fallback_mode:
        st.session_state.fallback_active = True
        sun_f = calculate_wbgt_meteorological_fallback(temp, rh, ws, hour_data['hour_24h'], True)
        shade_f = calculate_wbgt_meteorological_fallback(temp, rh, ws, hour_data['hour_24h'], False)
        notes.append("Offline Stull Fallback Used")
    else:
        sun_f, shade_f = hour_data['sun_f'], hour_data['shade_f']
        if "clamp_notes" in hour_data: notes.extend(hour_data['clamp_notes'])

    if st.session_state.use_caf:
        sun_f = round(sun_f + st.session_state.caf_value, 1)
        shade_f = round(shade_f + st.session_state.caf_value, 1)

    adj_watts = hour_data["final_watts"]
    limit_f = round(((56.7 - 11.5 * math.log10(adj_watts)) * 1.8) + 32, 1)
    alert_f = round(((59.9 - 14.1 * math.log10(adj_watts)) * 1.8) + 32, 1)
    status = "Normal"
    if sun_f > limit_f or shade_f > limit_f: status = f"BREACH: {limit_name}"
    elif sun_f > alert_f or shade_f > alert_f: status = f"WARNING: {alert_name}"
    
    return {"Date": hour_data["date_string_final"], "Time": hour_data["time_display"], "Air_Temp_F": temp, "Humidity_Pct": rh, "Wind_Speed_mph": ws, "Barometric_Pressure_inHg": pres, "Sun_WBGT_F": sun_f, "Shade_WBGT_F": shade_f, "Workload": hour_data["workload_label"], "Adjusted_Watts": adj_watts, limit_key: limit_f, alert_key: alert_f, "Safety_Status": status, "Weather_Data_Source": data_source_label, "Notes": " | ".join(filter(None, notes))}

# =====================================================================
# CORRECTED WEB AUTOMATION ENGINE (FIXED LONGITUDE_ABSOLUTE KEY)
# =====================================================================
def run_browser_automation(hourly_data, data_source_label, standard_choice):
    computed_results, progress_bar, status_text = [], st.progress(0), st.empty()
    st.session_state.fallback_active = False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            status_text.text("Navigating to OSHA WBGT Calculator...")
            page.goto("https://www.osha.gov/heat-exposure/wbgt-calculator", wait_until="load", timeout=25000)
            
            frame_locator = page.frame_locator('iframe[src*="advanced-wbgt-calculator.html"]')
            if not frame_locator.first.is_visible(timeout=10000):
                raise Exception("Could not find calculator iframe.")
            frame = frame_locator.first

            for index, hour in enumerate(hourly_data):
                progress_bar.progress((index + 1) / len(hourly_data))
                status_text.text(f"Processing Hour: {hour['time_display']} ({index+1}/{len(hourly_data)})")
                if hour.get("skip_calc", False):
                    computed_results.append(process_hourly_result(hour, data_source_label, standard_choice))
                    continue
                
                row_fallback = False
                try:
                    temp, rh, ws, pres = [hour.get(k) for k in ['temperature_f', 'relative_humidity_percent', 'wind_speed_mph', 'barometric_pressure_inhg']]
                    safe_temp, safe_rh, safe_ws, safe_pres = max(min(temp, 120.0), 32.0), max(min(rh, 100), 1), max(min(ws, 50.0), 0.0), max(min(pres, 32.0), 25.0)
                    hour['clamp_notes'] = [f"{n} clamped" for v, s_v, n in [(temp,safe_temp,"Temp"), (rh,safe_rh,"RH")] if v!=s_v]

                    frame.fill('input[name="dd"]', hour["date_string_final"])
                    frame.fill('input[name="tm"]', hour.get("calculator_time", f"{hour['hour_24h']:02d}:00"))
                    frame.fill('input[name="lat"]', str(hour["latitude"]))
                    # FIXED LINE: Uses "longitude_absolute" which is correctly generated in Step 1 geocoding/NOAA functions
                    frame.fill('input[name="lon"]', str(hour["longitude_absolute"]))
                    frame.select_option('select[name="tz"]', value=hour["tz_value"])
                    frame.fill('input[name="temp"]', str(safe_temp))
                    frame.fill('input[name="rh"]', str(safe_rh))
                    frame.fill('input[name="ws"]', str(safe_ws))
                    frame.fill('input[name="pres"]', str(safe_pres))
                    
                    frame.click('input[value="Submit"]')
                    expect(frame.locator('input[name="wbgt_sun"]')).to_contain("/", timeout=15000)
                    
                    sun_val, shade_val = frame.locator('input[name="wbgt_sun"]').input_value(), frame.locator('input[name="wbgt_shade"]').input_value()
                    if "/" in sun_val:
                        hour['sun_f'] = float(sun_val.split('/')[1].strip()[:-1])
                        hour['shade_f'] = float(shade_val.split('/')[1].strip()[:-1])
                    else: 
                        row_fallback = True
                except Exception as e:
                    row_fallback = True
                
                computed_results.append(process_hourly_result(hour, data_source_label, standard_choice, fallback_mode=row_fallback))
            browser.close()
    except Exception as e:
        status_text.error(f"Browser automation failed: {e}. Using offline fallback for all hours.")
        computed_results = [process_hourly_result(h, data_source_label, standard_choice, True) for h in hourly_data]
    status_text.text("Processing completed successfully.")
    return computed_results

# =====================================================================
# MATPLOTLIB COMPLIANCE GRAPH GENERATOR
# =====================================================================
def generate_compliance_plot(results, worker_weight, is_forecast, use_caf, caf_label, standard_choice):
    is_acgih = "ACGIH" in standard_choice
    limit_label = 'ACGIH TLV (Acclimatized Limit)' if is_acgih else 'NIOSH REL (Acclimatized Limit)'
    alert_label = 'ACGIH Action Limit (Unacclimatized)' if is_acgih else 'NIOSH RAL (Unacclimatized Alert)'
    standard_prefix = "ACGIH" if is_acgih else "NIOSH"

    watts_range = np.linspace(90, 610, 500)
    limit_curve_f = [(56.7 - (11.5 * math.log10(w))) * 1.8 + 32 for w in watts_range]
    alert_curve_f = [(59.9 - (14.1 * math.log10(w))) * 1.8 + 32 for w in watts_range]
    
    fig, ax = plt.subplots(figsize=(11, 6.5))
    
    standard_w = {180: "Light (180W)", 300: "Moderate (300W)", 415: "Heavy (415W)", 520: "Very Heavy (520W)"}
    for w_val, label in standard_w.items():
        ax.axvline(x=w_val, color='grey', linestyle=':', alpha=0.4)
        ax.text(w_val + 4, 66, label, rotation=90, color='grey', alpha=0.6, fontsize=9, va='bottom')
    
    ax.plot(watts_range, limit_curve_f, color='crimson', linestyle='-', linewidth=2.5, label=limit_label)
    ax.plot(watts_range, alert_curve_f, color='darkorange', linestyle='--', linewidth=2.5, label=alert_label)
    
    x_watts = [r["Adjusted_Watts"] for r in results if r["Sun_WBGT_F"] != "N/A"]
    y_sun = [r["Sun_WBGT_F"] for r in results if r["Sun_WBGT_F"] != "N/A"]
    y_shade = [r["Shade_WBGT_F"] for r in results if r["Shade_WBGT_F"] != "N/A"]
    
    if x_watts:
        min_w, max_w = min(x_watts), max(x_watts)
        min_wbgt, max_wbgt = min(y_shade + y_sun), max(y_shade + y_sun)
        
        w_padding = 30 if (max_w - min_w) < 20 else 20
        box_x = min_w - w_padding; box_w = (max_w - min_w) + (w_padding * 2)
        box_y = min_wbgt - 1.5; box_h = (max_wbgt - min_wbgt) + 3.0
        
        rect = patches.Rectangle((box_x, box_y), box_w, box_h,
                                 linewidth=1.5, edgecolor='none', facecolor='#E6D8E7', alpha=0.4,
                                 label='Shift Exposure Envelope Box')
        ax.add_patch(rect)
    
    if use_caf:
        if x_watts:
            ax.scatter(x_watts, y_sun, color='darkred', marker='d', zorder=5, label='Effective Sun WBGT (CAF-Adjusted)')
            ax.scatter(x_watts, y_shade, color='darkblue', marker='p', zorder=5, label='Effective Shade WBGT (CAF-Adjusted)')
    else:
        if x_watts:
            ax.scatter(x_watts, y_sun, color='red', marker='o', zorder=5, label='Hourly Exposure (Sun WBGT)')
            ax.scatter(x_watts, y_shade, color='blue', marker='s', zorder=5, label='Hourly Exposure (Shade WBGT)')
    
    for r in results:
        if r["Sun_WBGT_F"] != "N/A":
            ax.annotate(r["Time"], (r["Adjusted_Watts"], r["Sun_WBGT_F"]), textcoords="offset points", xytext=(6, 5), fontsize=8, color='darkred', fontweight='bold')
            ax.annotate(r["Time"], (r["Adjusted_Watts"], r["Shade_WBGT_F"]), textcoords="offset points", xytext=(6, -12), fontsize=8, color='darkblue')

    title_prefix = "Predictive" if is_forecast else "Historical"
    caf_subtitle = f"\nClothing Adjustment Factor (CAF): {caf_label}" if use_caf else ""
    ax.set_title(f"{standard_prefix} Heat Stress Analytical Assessment Plot ({title_prefix}){caf_subtitle}\nWorker Structural Weight: {worker_weight:.1f} lbs", fontsize=11, fontweight='bold')
    ax.set_xlabel("Adjusted Metabolic Rate (Watts)", fontsize=11)
    ax.set_ylabel("Wet Bulb Globe Temperature Index (WBGT in °F)", fontsize=11)
    
    ax.set_xlim(90, 610)
    y_min_bound = 65
    y_max_bound = max(98, max(y_sun + y_shade) + 5) if x_watts else 98
    ax.set_ylim(y_min_bound, y_max_bound)
    
    ax.grid(True, linestyle=':', alpha=0.5)
    ax.legend(loc='upper right', framealpha=0.9)
    return fig

# =====================================================================
# LOCATION CONFIRMATION POP-UP DIALOG (FOR OPEN-METEO ONLY)
# =====================================================================
@st.dialog("Confirm Target Location")
def show_location_confirmation_dialog():
    geo = st.session_state.pending_geo
    if not geo: return
        
    st.write("Please confirm that the retrieved location matches your site:")
    col_entered, col_matched = st.columns(2)
    with col_entered:
        st.markdown("**User Entered Location:**"); st.info(geo["raw_entered"])
    with col_matched:
        st.markdown("**Retrieved / Geocoded Site:**"); st.success(geo["matched_address"])
        
    st.caption(f"📍 **Coordinates:** Latitude {geo['latitude']}, Longitude {geo['longitude']}")
    if geo.get("fallback_used"):
        st.warning("⚠️ Geocoding fell back to City/State/ZIP center.")

    col_confirm, col_edit = st.columns(2)
    with col_confirm:
        if st.button("Confirm & Proceed →", type="primary", use_container_width=True):
            st.session_state.confirmed_geo = geo
            st.session_state.pending_geo = None
            
            target_date = geo["target_date"]
            start_hour, end_hour = geo["shift_hours"]
            worker_weight = geo["worker_weight"]
            date_str = target_date.strftime("%Y-%m-%d")
            
            with st.spinner("Retrieving weather matrices..."):
                weather_res = fetch_weather_native(geo["latitude"], geo["longitude"], date_str, st.session_state.is_forecast)
            
            if "error" in weather_res or "hourly" not in weather_res:
                st.error(weather_res.get("error", "Failed to retrieve weather data."))
            else:
                hourly = weather_res["hourly"]
                grid_lat = weather_res["grid_latitude"]; grid_lon = weather_res["grid_longitude"]
                dist_miles = haversine_distance(geo["latitude"], geo["longitude"], grid_lat, grid_lon)
                
                st.session_state.location_meta = {
                    "user_entered": geo["raw_entered"], "validated": geo["matched_address"],
                    "target_lat": geo["latitude"], "target_lon": geo["longitude"],
                    "grid_lat": grid_lat, "grid_lon": grid_lon,
                    "distance_miles": dist_miles, "data_source": "Open-Meteo"
                }
                
                active_rows = []
                for i in range(len(hourly["time"])):
                    hr_int = int(hourly["time"][i].split("T")[1].split(":")[0])
                    if start_hour <= hr_int <= end_hour:
                        ampm = "12:00 AM" if hr_int==0 else ("12:00 PM" if hr_int==12 else (f"{hr_int-12}:00 PM" if hr_int>12 else f"{hr_int}:00 AM"))
                        
                        active_rows.append({
                            "date_string_final": target_date.strftime("%m/%d/%Y"), "time_display": ampm, "hour_24h": hr_int,
                            "calculator_time": f"{hr_int:02d}:00", "skip_calc": False, "note_additions": "",
                            "latitude": geo["latitude"], "longitude": geo["longitude"],
                            "longitude_absolute": abs(geo["longitude"]), # CRITICAL DATA BINDING KEY PASSED TO PLAYWRIGHT
                            "tz_value": get_tz_offset_from_coords(geo["latitude"], geo["longitude"], target_date),
                            "temperature_f": hourly["temperature_2m"][i], 
                            "relative_humidity_percent": int(hourly["relative_humidity_2m"][i]), 
                            "wind_speed_mph": hourly["wind_speed_10m"][i], 
                            "barometric_pressure_inhg": round(hourly["surface_pressure"][i] * 0.02953, 2)
                        })
                
                st.session_state.final_hourly_rows = active_rows
                st.session_state.worker_weight = worker_weight
                st.session_state.location_fallback = geo.get("fallback_used", False)
                st.session_state.step = 2
                st.rerun()

    with col_edit:
        if st.button("Edit Location", type="secondary", use_container_width=True):
            st.session_state.pending_geo = None
            st.rerun()

# =====================================================================
# UI / STREAMLIT APP ENGINE
# =====================================================================
st.session_state.is_forecast = st.toggle(
    "📅 Switch to Future Forecast Mode (For Planning & Prediction)", 
    value=st.session_state.get("is_forecast", False),
    disabled=(st.session_state.step > 1)
)

if st.session_state.is_forecast:
    st.title("🌤️ OSHA-WBGT Predictive Forecast Calculator")
    st.warning("⚠️ **FORECAST WARNING:** This tool uses forecast models for future planning. On-site environmental tracking remains mandatory.")
else:
    st.title("☀️ OSHA-WBGT Localized Calculator")

st.markdown("**Occupational Heat Exposure Analytics by Andre Taylor**")
st.divider()

with st.expander("📚 Methodology, Data Sources & About the Author"):
    st.markdown("""
    ### 📍 Address Matching & Geocoding Pipeline
    This application utilizes a highly accurate, dual-geocoding approach to pinpoint workplace locations. Initial location requests are passed through the **US Census Bureau's native geocoding database** for street-level matching. If this fails, the system automatically uses the **Mapbox (OpenStreetMap) API** to ensure precise coordinate extraction.

    ### 🌤️ Weather Data & Meteorological Modeling
    After establishing coordinates, the application uses one of two methods for weather data:
    1.  **Open-Meteo API:** For both historical and future forecast data, the app interfaces with the Open-Meteo API. 
        - **Historical Mode:** Uses the ERA5 global reanalysis dataset.
        - **Forecast Mode:** Uses a blend of leading forecast models like NOAA's GFS and HRRR.
    2.  **NOAA Local CSV Upload:** Users can upload a CSV file from the NOAA NCEI database. The application intelligently parses this data, using the station's coordinates to determine the local time zone and automatically handle Daylight Saving Time (DST) for the given date.

    ### 🤖 Automation and Fallback Logic
    The application automates data entry into the official **[OSHA WBGT Calculator](https://www.osha.gov/heat-exposure/wbgt-calculator)**. Using a headless browser, it submits the weather data for each hour and scrapes the resulting WBGT values. If the OSHA website is unreachable or the process fails, a built-in **Stull's Equation Fallback** is automatically triggered to estimate the WBGT based on meteorological principles.

    ### ⚖️ Workload Calculation
    Worker metabolism is calculated using two methods:
    1.  **Standard Method:** Users select a workload (Light, Moderate, etc.). The base wattage is then adjusted proportionally based on the worker's weight relative to a standard 154 lb (70 kg) reference.
    2.  **Advanced Clinical Method:** Users can input a specific MET value. The app converts this to watts, with options to use a standard formula or a more precise calculation based on the worker's individual biometrics (age, sex, height) via the Mifflin-St Jeor equation.

    ### 👨‍🔬 About the Developer
    **Andre Taylor** is a Health Scientist for the Occupational Safety and Health Administration (OSHA) and a leading Subject Matter Expert (SME) on workplace heat exposure, physiological hazard assessments, and industrial mitigation strategies. Andre bridges the critical operational gap between clinical health sciences and practical, on-the-ground occupational safety.
    """)

mapbox_secret = os.environ.get("MAPBOX_API_KEY", st.secrets.get("MAPBOX_API_KEY", "") if hasattr(st, "secrets") else "")

if st.session_state.pending_geo is not None:
    show_location_confirmation_dialog()

# --- WIZARD STEP 1: TARGET PARAMETER INPUTS ---
if st.session_state.step == 1:
    st.subheader("Step 1: Set Target Parameters & Profile Matrix")
    
    noaa_disabled = st.session_state.is_forecast
    if noaa_disabled:
        data_source_index = 0
        st.session_state.data_source_choice = "Open-Meteo (Default/Free)"
    else:
        data_source_index = 1 if st.session_state.get("data_source_choice") == "NOAA Station Data (Local CSV Upload)" else 0

    data_source = st.radio("Select Provider:", ["Open-Meteo (Default/Free)", "NOAA Station Data (Local CSV Upload)"],
                           index=data_source_index,
                           key="data_source_choice",
                           help="NOAA CSV upload is disabled in Future Forecast Mode.")
    
    use_gps = False; uploaded_noaa_csv = None
    
    if "NOAA" in data_source and not noaa_disabled:
        st.info("💡 **NOAA Local Climatological Data:** Upload a CSV dataset downloaded from the NOAA NCEI tool.")
        uploaded_noaa_csv = st.file_uploader("Upload NOAA LCD Weather File (.csv)", type=["csv"])
    else:
        col_loc_header, col_loc_toggle = st.columns([3, 1])
        with col_loc_header: st.markdown("**Location Details**")
        with col_loc_toggle: use_gps = st.toggle("Use GPS Coordinates", value=False)
        
        if not use_gps:
            c_addr1, c_addr2, c_addr3, c_addr4 = st.columns([2, 2, 1, 1.5])
            with c_addr1: target_street = st.text_input("Street Address (Optional)", value="", placeholder="e.g., 7339 State Road")
            with c_addr2: target_city = st.text_input("City", value="", placeholder="e.g., Philadelphia")
            with c_addr3: target_state = st.text_input("State", value="", placeholder="e.g., PA")
            with c_addr4: target_zip = st.text_input("ZIP Code", value="", placeholder="e.g., 19136")
        else:
            c_gps1, c_gps2 = st.columns(2)
            with c_gps1: target_lat_in = st.text_input("Latitude", value="", placeholder="e.g., 40.0345")
            with c_gps2: target_lon_in = st.text_input("Longitude", value="", placeholder="e.g., -75.0181")

    st.markdown("**Shift & Employee Details**")
    c_shift1, c_shift2, c_shift3 = st.columns([1.5, 2, 1.5])
    
    today_date = date.today()
    with c_shift1: 
        if st.session_state.is_forecast:
            target_date = st.date_input("Planned Work Date", value=today_date, min_value=today_date, max_value=today_date + timedelta(days=14))
        else:
            target_date = st.date_input("Inspection Date", value=today_date - timedelta(days=1), max_value=today_date)
            
    with c_shift2: start_hour, end_hour = st.slider("Shift Operating Hours", min_value=0, max_value=23, value=(8, 16), format="%d:00")
    with c_shift3: worker_weight = st.number_input("Employee Weight (lbs)", min_value=50.0, max_value=400.0, value=154.0, step=1.0)
    
    if st.button("Process Weather Data", type="primary"):
        if "NOAA" in data_source and not noaa_disabled:
            if uploaded_noaa_csv is None: st.error("Please upload a NOAA CSV file.")
            else:
                with st.spinner("Processing NOAA CSV..."):
                    noaa_result = process_weather_noaa_csv(uploaded_noaa_csv, target_date, start_hour, end_hour)
                    if "error" in noaa_result: st.error(noaa_result["error"])
                    else:
                        active_rows = []
                        for hr_int in range(start_hour, end_hour + 1):
                            hr_data = noaa_result["hourly_records"][hr_int]
                            ampm = "12:00 AM" if hr_int==0 else ("12:00 PM" if hr_int==12 else (f"{hr_int-12}:00 PM" if hr_int>12 else f"{hr_int}:00 AM"))
                            
                            active_rows.append({
                                "date_string_final": target_date.strftime("%m/%d/%Y"), "time_display": ampm, "hour_24h": hr_int,
                                "calculator_time": hr_data.get("calculator_time", f"{hr_int:02d}:00"), "skip_calc": hr_data.get("skip_calc", False),
                                "note_additions": hr_data.get("note_additions", ""),
                                "latitude": noaa_result["latitude"], "longitude": noaa_result["longitude"],
                                "longitude_absolute": abs(noaa_result["longitude"]), 
                                "tz_value": get_tz_offset_from_coords(noaa_result["latitude"], noaa_result["longitude"], target_date),
                                "temperature_f": hr_data["temperature_f"], "relative_humidity_percent": hr_data["relative_humidity_percent"], 
                                "wind_speed_mph": hr_data["wind_speed_mph"], "barometric_pressure_inhg": hr_data["barometric_pressure_inhg"]
                            })
                        
                        st.session_state.location_meta = {
                            "user_entered": "NOAA CSV Upload", "validated": f"Station: {noaa_result['station_name']}",
                            "target_lat": noaa_result["latitude"], "target_lon": noaa_result["longitude"],
                            "grid_lat": noaa_result["latitude"], "grid_lon": noaa_result["longitude"], "distance_miles": 0.0,
                            "data_source": "NOAA Station Data (Local CSV Upload)", "station_name": noaa_result["station_name"]
                        }
                        
                        st.session_state.raw_noaa_df_export = None
                        st.session_state.raw_weather_debug = None
                        st.session_state.final_hourly_rows = active_rows
                        st.session_state.worker_weight = worker_weight
                        st.session_state.location_fallback = False
                        st.session_state.step = 2
                        st.rerun()
        else:
            if use_gps:
                try:
                    lat_val = float(target_lat_in); lon_val = float(target_lon_in)
                    st.session_state.pending_geo = {
                        "latitude": lat_val, "longitude": lon_val, "matched_address": f"Exact Coordinates ({lat_val}, {lon_val})",
                        "raw_entered": f"GPS: {lat_val}, {lon_val}", "fallback_used": False, "target_date": target_date,
                        "shift_hours": (start_hour, end_hour), "worker_weight": worker_weight, "data_source": data_source
                    }
                    st.rerun()
                except ValueError: st.error("Please enter valid decimal coordinates.")
            else:
                with st.spinner("Resolving location coordinates..."):
                    geo, fallback_used, raw_entered_address = resolve_location(target_street, target_city, target_state, target_zip, mapbox_secret)
                    if "error" in geo: st.error(geo["error"])
                    else:
                        st.session_state.pending_geo = {
                            "latitude": geo["latitude"], "longitude": geo["longitude"],
                            "matched_address": geo.get("matched_address", raw_entered_address), "raw_entered": raw_entered_address,
                            "fallback_used": fallback_used and bool(target_street.strip()), "target_date": target_date,
                            "shift_hours": (start_hour, end_hour), "worker_weight": worker_weight, "data_source": data_source
                        }
                        st.rerun()

# --- WIZARD STEP 2: METABOLISM & WORKLOADS ---
elif st.session_state.step == 2:
    st.subheader("Step 2: Assign Hourly Worker Metabolism / Workloads")
    
    standard_choice = st.radio("Select Evaluation Standard", ["NIOSH (Default)", "ACGIH (Requires Permission)"])
    st.session_state.standard_choice = standard_choice
    
    use_caf = st.toggle("Apply Clothing Adjustment Factor (CAF)", value=st.session_state.use_caf)
    caf_value = 0.0; caf_label = "Standard Work Clothes (0.0 °F)"
    if use_caf:
        caf_dict = {"Short sleeves and pants (-1.8 °F)": -1.8, "Work clothes / Cloth coveralls (0.0 °F)": 0.0,
                    "SMS polypropylene coveralls (+0.9 °F)": 0.9, "Polyolefin coveralls (+1.8 °F)": 1.8,
                    "Double-layer woven clothing (+5.4 °F)": 5.4, "Limited-use vapor-barrier coveralls (+19.8 °F)": 19.8}
        caf_choice = st.selectbox("Select Clothing Ensemble / PPE Type", list(caf_dict.keys()))
        caf_value = caf_dict[caf_choice]; caf_label = caf_choice
        st.info(f"ℹ️ Active CAF Correction: **{caf_value:+.1f} °F** added to WBGT.")
        
    st.session_state.use_caf = use_caf
    st.session_state.caf_value = caf_value
    st.session_state.caf_label = caf_label
    
    st.divider()
    use_clinical = st.toggle("Advanced Clinical / Custom Workload", value=False)
    account_for_weight = True; clinical_method = "Standard Ainsworth (Weight Only)"; sex = "Male"; age = 35; height_in = 70.0
    
    if use_clinical:
        account_for_weight = st.toggle("Account for employee physiological data?", value=True)
        if account_for_weight:
            clinical_method = st.radio("Metabolic Calculation Method", ["Standard Ainsworth (Weight Only)", "Corrected METs (Mifflin-St Jeor)"])
            if clinical_method == "Corrected METs (Mifflin-St Jeor)":
                c_bio1, c_bio2, c_bio3 = st.columns(3)
                sex = c_bio1.selectbox("Biological Sex", ["Male", "Female"])
                age = c_bio2.number_input("Age", min_value=18, max_value=100, value=35)
                height_in = c_bio3.number_input("Height (Inches)", min_value=40.0, max_value=90.0, value=70.0)
    
    preset_map = {"Light (180W)": 180, "Moderate (300W)": 300, "Heavy (415W)": 415, "Very Heavy (520W)": 520}
    selections = {}
    cols = st.columns(min(len(st.session_state.final_hourly_rows), 4))
    
    for idx, row in enumerate(st.session_state.final_hourly_rows):
        col_target = cols[idx % len(cols)]
        with col_target:
            if not use_clinical:
                selections[row["hour_24h"]] = st.selectbox(f"Hour: {row['time_display']}", options=list(preset_map.keys()), index=1, key=f"sel_{row['hour_24h']}")
            else:
                selections[row["hour_24h"]] = st.number_input(f"Hour: {row['time_display']} (METs)", min_value=0.9, max_value=18.0, value=3.5, step=0.1, key=f"met_{row['hour_24h']}")
                
    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("← Modify Location or Timeline"):
            st.session_state.step = 1; st.rerun()
    with c2:
        if st.button("Run Scraper & Generate Analysis →", type="primary"):
            for row in st.session_state.final_hourly_rows:
                if not use_clinical:
                    chosen_w = preset_map[selections[row["hour_24h"]]]
                    row["workload_label"] = selections[row["hour_24h"]].split(" ")[0]
                    row["final_watts"] = round((chosen_w * st.session_state.worker_weight) / 154.0, 1)
                else:
                    met_val = selections[row["hour_24h"]]
                    worker_kg = st.session_state.worker_weight * 0.453592
                    if not account_for_weight:
                        calc_watts = met_val * 70.0 * 1.163
                    else:
                        if clinical_method == "Standard Ainsworth (Weight Only)":
                            calc_watts = met_val * worker_kg * 1.163
                        else:
                            height_cm = height_in * 2.54
                            if sex == "Male": rmr_kcal_day = (10 * worker_kg) + (6.25 * height_cm) - (5 * age) + 5
                            else: rmr_kcal_day = (10 * worker_kg) + (6.25 * height_cm) - (5 * age) - 161
                            rmr_kcal_hr = rmr_kcal_day / 24.0
                            calc_watts = met_val * rmr_kcal_hr * 1.16222
                    row["workload_label"] = f"{met_val} METs"; row["final_watts"] = round(calc_watts, 1)
                
            with st.spinner("Executing calculations..."):
                data_source_val = st.session_state.location_meta.get("data_source", "Open-Meteo")
                data_source_label = "Open-Meteo Forecast" if st.session_state.is_forecast else "Open-Meteo Archive"
                results = run_browser_automation(st.session_state.final_hourly_rows, data_source_label, st.session_state.standard_choice)
                
            if results:
                st.session_state.results = results
                st.session_state.step = 3
                st.rerun()

# --- WIZARD STEP 3: RESULTS & SPREADSHEET EXPORT ---
elif st.session_state.step == 3:
    st.subheader("Step 3: Compliance Engineering Summary Analysis Output")
    
    if st.session_state.get("fallback_active", False):
        st.warning("⚠️ **Playwright Fallback Active**: Stull's equation was used to estimate WBGT offline.")
    else: 
        st.success("✅ Wet Bulb Globe Temperature (WBGT) data successfully scraped from OSHA.")
        
    meta = st.session_state.get("location_meta", {})
    if meta:
        st.info(f"📍 **Address Audit Trail:**\n"
                f"* **Entered Location:** {meta.get('user_entered', 'N/A')}\n"
                f"* **Validated/Geocoded Location:** {meta.get('validated', 'N/A')} (Lat: {meta.get('target_lat')}, Lon: {meta.get('target_lon')})")
        
    fig = generate_compliance_plot(st.session_state.results, st.session_state.worker_weight, st.session_state.is_forecast, 
                                   st.session_state.use_caf, st.session_state.caf_label, st.session_state.standard_choice)
    st.pyplot(fig)
    
    df_results = pd.DataFrame(st.session_state.results)
    st.dataframe(df_results, use_container_width=True)
    
    file_date_str = datetime.now().strftime("%Y%m%d")
    
    img_buffer = io.BytesIO(); fig.savefig(img_buffer, format="png", bbox_inches="tight", dpi=150); img_buffer.seek(0)
    excel_buffer = io.BytesIO()
    
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        df_results.to_excel(writer, sheet_name="Exposure_Data", index=False)
        pd.DataFrame([meta]).to_excel(writer, sheet_name="Location_Details", index=False)
        
        exposure_sheet = writer.sheets["Exposure_Data"]
        excel_img = OpenPyxlImage(img_buffer); excel_img.width = 650; excel_img.height = 380
        exposure_sheet.add_image(excel_img, f"A{len(df_results) + 4}")
        
    excel_file_data = excel_buffer.getvalue()
    st.download_button(label="Download Compliance Report (.XLSX)", data=excel_file_data, 
                       file_name=f"Heat_Stress_Report_{file_date_str}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    
    st.divider()
    if st.button("🔄 Execute Fresh Inspection Run"):
        reset_app_state()
        st.rerun()
