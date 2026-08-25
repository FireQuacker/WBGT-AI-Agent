import os
import subprocess
import streamlit as st
import time
import math
import io
import requests
import urllib.parse
from datetime import datetime, date, timedelta
from playwright.sync_api import sync_playwright
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np
import pandas as pd
import openpyxl
from openpyxl.drawing.image import Image as OpenPyxlImage

# =====================================================================
# ONE-TIME PLAYWRIGHT INSTALLER (PREVENTS RE-RUN LAG)
# =====================================================================
@st.cache_resource
def install_browser_engine():
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
    except Exception as e:
        st.error(f"Background browser engine initialization warning: {e}")

install_browser_engine()

# =====================================================================
# STREAMLIT CONFIGURATION & PERSISTENCE STATE
# =====================================================================
st.set_page_config(page_title="OSHA-WBGT Localized Calculator", layout="wide")

if "step" not in st.session_state:
    st.session_state.step = 1
if "pending_geo" not in st.session_state:
    st.session_state.pending_geo = None
if "final_hourly_rows" not in st.session_state:
    st.session_state.final_hourly_rows = None
if "raw_weather_debug" not in st.session_state:
    st.session_state.raw_weather_debug = None
if "raw_noaa_df_export" not in st.session_state:
    st.session_state.raw_noaa_df_export = None
if "worker_weight" not in st.session_state:
    st.session_state.worker_weight = 154.0
if "fallback_active" not in st.session_state:
    st.session_state.fallback_active = False
if "location_fallback" not in st.session_state:
    st.session_state.location_fallback = False
if "is_forecast" not in st.session_state:
    st.session_state.is_forecast = False
if "use_caf" not in st.session_state:
    st.session_state.use_caf = False
if "caf_value" not in st.session_state:
    st.session_state.caf_value = 0.0
if "caf_label" not in st.session_state:
    st.session_state.caf_label = "Standard Work Clothes (0.0 °F)"
if "standard_choice" not in st.session_state:
    st.session_state.standard_choice = "ACGIH (Default)"
if "location_meta" not in st.session_state:
    st.session_state.location_meta = {}

# =====================================================================
# GEOCODING & METEOROLOGICAL UTILITIES
# =====================================================================
def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8  
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(R * c, 2)

def get_osha_tz_value(lon: float) -> str:
    if lon >= -85.5: return "-5"
    elif lon >= -103.5: return "-6"
    elif lon >= -115.5: return "-7"
    elif lon >= -130.0: return "-8"
    elif lon >= -150.0: return "-9"
    else: return "-10"

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
                matched_str = matches[0].get("matchedAddress", address)
                return {"latitude": coords["y"], "longitude": coords["x"], "matched_address": matched_str}
    except Exception:
        pass

    if not mapbox_key:
        return {"error": "US Census database could not pinpoint this address. Please verify address details or provide a MAPBOX_API_KEY in settings."}
    
    try:
        encoded_address = urllib.parse.quote(address)
        mapbox_url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{encoded_address}.json"
        mapbox_params = {"access_token": mapbox_key, "limit": 1}
        mapbox_response = requests.get(mapbox_url, params=mapbox_params, timeout=10)
        
        if mapbox_response.status_code == 200:
            data = mapbox_response.json()
            features = data.get("features", [])
            if features:
                coords = features[0]["center"]
                matched_str = features[0].get("place_name", address)
                return {"latitude": coords[1], "longitude": coords[0], "matched_address": matched_str}
        
        return {"error": "Location coordinates could not be resolved."}
    except Exception as e:
        return {"error": f"Mapbox Fallback System Error: {str(e)}"}

def fetch_weather_native(lat: float, lon: float, date_str: str, is_forecast: bool) -> dict:
    url = "https://api.open-meteo.com/v1/forecast" if is_forecast else "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon, "start_date": date_str, "end_date": date_str,
        "hourly": ["temperature_2m", "relative_humidity_2m", "surface_pressure", "wind_speed_10m"],
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto"
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code != 200:
            return {"error": f"Weather API blocked the request (HTTP {response.status_code})."}
        data = response.json()
        return {
            "hourly": data.get("hourly", {}),
            "grid_latitude": data.get("latitude", lat),
            "grid_longitude": data.get("longitude", lon),
            "raw_debug": data
        }
    except Exception as e:
        return {"error": f"Weather System Error: {str(e)}"}

# =====================================================================
# NOAA CSV PARSING ENGINE (+/- 10 MINUTE TIE BREAK & DST/UNIT HANDLING)
# =====================================================================
def process_weather_noaa_csv(uploaded_file, target_date, start_hour, end_hour):
    try:
        df = pd.read_csv(uploaded_file, dtype=str)
    except Exception as e:
        return {"error": f"Failed to read CSV file: {str(e)}"}
        
    if 'DATE' not in df.columns:
        return {"error": "Invalid CSV format. Expected 'DATE' column from NOAA LCD data."}
        
    df['DATE_raw'] = pd.to_datetime(df['DATE'], errors='coerce')
    df = df.dropna(subset=['DATE_raw'])
    
    is_dst = df['DATE_raw'].dt.month.between(3, 11)
    df['DST_Adjusted_Time'] = df['DATE_raw'].copy()
    df.loc[is_dst, 'DST_Adjusted_Time'] = df['DATE_raw'] + pd.Timedelta(hours=1)
    
    df['DATE_parsed'] = df['DST_Adjusted_Time']
    
    target_dt_start = datetime.combine(target_date, datetime.min.time())
    target_dt_end = target_dt_start + timedelta(days=1)
    
    df_day = df[(df['DATE_parsed'] >= (target_dt_start - timedelta(hours=2))) & 
                (df['DATE_parsed'] <= (target_dt_end + timedelta(hours=2)))].copy()
                
    if df_day.empty:
        return {"error": f"No data found in the CSV for the selected date ({target_date.strftime('%Y-%m-%d')}). Please verify the file covers this timeframe."}
        
    lat = float(df_day['LATITUDE'].iloc[0]) if 'LATITUDE' in df_day.columns and not pd.isna(df_day['LATITUDE'].iloc[0]) else 0.0
    lon = float(df_day['LONGITUDE'].iloc[0]) if 'LONGITUDE' in df_day.columns and not pd.isna(df_day['LONGITUDE'].iloc[0]) else 0.0
    station_name = df_day['NAME'].iloc[0] if 'NAME' in df_day.columns and not pd.isna(df_day['NAME'].iloc[0]) else "NOAA Local CSV Station"
    
    def clean_numeric(val):
        if pd.isna(val): return None
        val_str = str(val).strip().replace('*', '').replace('s', '').replace('V', '')
        try:
            numeric = ''.join(c for c in val_str if c.isdigit() or c == '.' or c == '-')
            return float(numeric) if numeric else None
        except ValueError:
            return None
            
    hourly_records = {}
    raw_export_rows = []
    
    for hr in range(start_hour, end_hour + 1):
        target_time = target_dt_start + timedelta(hours=hr)
        window_start = target_time - timedelta(minutes=10)
        window_end = target_time + timedelta(minutes=10)
        
        df_window = df_day[(df_day['DATE_parsed'] >= window_start) & (df_day['DATE_parsed'] <= window_end)].copy()
        
        note_additions = []
        
        if not df_window.empty:
            df_window['time_diff'] = (df_window['DATE_parsed'] - target_time).dt.total_seconds()
            df_window['abs_diff'] = df_window['time_diff'].abs()
            df_window = df_window.sort_values(by=['abs_diff', 'time_diff'], ascending=[True, True])
            
            best_row = None
            for _, row in df_window.iterrows():
                if clean_numeric(row.get('HourlyDryBulbTemperature')) is not None:
                    best_row = row
                    break
            
            if best_row is None:
                best_row = df_window.iloc[0]
                
            raw_t = clean_numeric(best_row.get('HourlyDryBulbTemperature'))
            rh_val = clean_numeric(best_row.get('HourlyRelativeHumidity'))
            w_val = clean_numeric(best_row.get('HourlyWindSpeed'))
            p_val = clean_numeric(best_row.get('HourlyStationPressure'))
            
            skip_calc = False
            last_temp = None
            last_rh = None
            last_wind = None
            
            if raw_t is None or rh_val is None or w_val is None:
                note_additions.append("Key variables missing. Suggest a run with Open-Meteo.")
                skip_calc = True
            
            if raw_t is not None:
                if raw_t < 45.0:
                    last_temp = round((raw_t * 1.8) + 32.0, 1)
                    note_additions.append(f"NOAA Temp {raw_t}°C converted to {last_temp}°F")
                else:
                    last_temp = raw_t
                    
            if rh_val is not None: last_rh = rh_val
            if w_val is not None: last_wind = w_val
            
            if p_val is not None:
                last_pres = p_val
            else:
                last_pres = 29.92
                note_additions.append("Station Pressure missing; assumed standard 29.92 inHg")
            
            matched_ts = str(best_row['DATE'])
            calc_time = best_row['DST_Adjusted_Time'].strftime('%H:%M')
            
            raw_export_rows.append({
                "Target_Date": target_date.strftime("%Y-%m-%d"),
                "Target_Hour": f"{hr:02d}:00",
                "Raw_NOAA_Timestamp": matched_ts,
                "Rounded_DST_Adjust": target_time.strftime("%Y-%m-%d %H:%M:%S"),
                "Raw_DryBulb": raw_t,
                "Processed": last_temp,
                "Raw_Relative": rh_val,
                "Raw_Wind": w_val,
                "Raw_Station": p_val,
                "Assumed_Pressure_Used": p_val is None,
                "Station_Name": station_name
            })
            
            hourly_records[hr] = {
                "temperature_f": last_temp,
                "relative_humidity_percent": last_rh,
                "wind_speed_mph": last_wind,
                "barometric_pressure_inhg": last_pres,
                "matched_timestamp": matched_ts,
                "calculator_time": calc_time,
                "note_additions": " | ".join(note_additions),
                "skip_calc": skip_calc
            }
        else:
            raw_export_rows.append({
                "Target_Date": target_date.strftime("%Y-%m-%d"),
                "Target_Hour": f"{hr:02d}:00",
                "Raw_NOAA_Timestamp": "No Data in Window",
                "Rounded_DST_Adjust": target_time.strftime("%Y-%m-%d %H:%M:%S"),
                "Raw_DryBulb": None,
                "Processed": None,
                "Raw_Relative": None,
                "Raw_Wind": None,
                "Raw_Station": None,
                "Assumed_Pressure_Used": True,
                "Station_Name": station_name
            })
            
            hourly_records[hr] = {
                "temperature_f": None,
                "relative_humidity_percent": None,
                "wind_speed_mph": None,
                "barometric_pressure_inhg": 29.92,
                "matched_timestamp": "No Data in Window",
                "calculator_time": f"{hr:02d}:00",
                "note_additions": "Key variables missing. Suggest a run with Open-Meteo.",
                "skip_calc": True
            }
            
    raw_noaa_df = pd.DataFrame(raw_export_rows)
    
    return {
        "hourly_records": hourly_records,
        "latitude": lat,
        "longitude": lon,
        "station_name": station_name,
        "raw_noaa_df": raw_noaa_df
    }

def resolve_location(street: str, city: str, state: str, zip_code: str, mapbox_key: str):
    street = street.strip()
    city = city.strip()
    state = state.strip()
    zip_code = zip_code.strip()
    
    exact_parts = [p for p in [street, city, state, zip_code] if p]
    exact_address = ", ".join(exact_parts)
    
    general_parts = [p for p in [city, state, zip_code] if p]
    general_address = ", ".join(general_parts)
    
    if street:
        res1 = geocode_address_native(exact_address, mapbox_key)
        if "error" not in res1:
            return res1, False, exact_address
            
    if general_address:
        res2 = geocode_address_native(general_address, mapbox_key)
        if "error" not in res2:
            return res2, True, general_address
            
    return {"error": "Location coordinates could not be resolved. Please verify City, State, and ZIP Code."}, False, exact_address or general_address

def calculate_wbgt_meteorological_fallback(temp_f, rh_pct, wind_mph, hour_24h=12, is_sun=True):
    tc = (temp_f - 32) * 5.0 / 9.0
    rh = rh_pct
    tw = (tc * math.atan(0.151977 * (rh + 8.313766)**0.5) 
          + math.atan(tc + rh) 
          + math.atan(rh - 1.676331) 
          + 0.00391838 * (rh)**1.5 * math.atan(0.023101 * rh) 
          - 4.686035)
    
    if is_sun:
        wind_ms = max(wind_mph * 0.44704, 0.1)
        if 6 <= hour_24h <= 18:
            solar_rad = 850.0 * math.sin(math.pi * (hour_24h - 6) / 12.0)
        else:
            solar_rad = 0.0
            
        tg_c = tc + 0.015 * solar_rad - 0.12 * wind_ms
        if tg_c < tc: tg_c = tc + 0.5
    else:
        tg_c = tc + 1.0
        
    wbgt_c = (0.7 * tw + 0.2 * tg_c + 0.1 * tc) if is_sun else (0.7 * tw + 0.3 * tg_c)
    return round((wbgt_c * 1.8) + 32, 1)

# =====================================================================
# WEB AUTOMATION BACKEND ENGINE
# =====================================================================
def run_browser_automation(hourly_data, data_source_label, standard_choice):
    computed_results = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    st.session_state.fallback_active = False
    
    is_acgih = "ACGIH" in standard_choice
    limit_key = "ACGIH_TLV_F" if is_acgih else "NIOSH_REL_F"
    alert_key = "ACGIH_AL_F" if is_acgih else "NIOSH_RAL_F"
    limit_name = "TLV" if is_acgih else "REL"
    alert_name = "AL" if is_acgih else "RAL"
        
    try:
        with sync_playwright() as p:
            status_text.text("Launching headless browser context...")
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"])
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
            page = context.new_page()
            
            target_url = "https://www.osha.gov/heat-exposure/wbgt-calculator"
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(1.5)
                target_frame = page
                for frame in page.frames:
                    try:
                        frame.locator('input[name="temp"]').wait_for(state="attached", timeout=1200)
                        target_frame = frame
                        break
                    except Exception:
                        continue
            except Exception:
                target_frame = page

            total_rows = len(hourly_data)
            for index, hour in enumerate(hourly_data):
                status_text.text(f"Scraping OSHA Calculator for hour: {hour['time_display']} ({index+1}/{total_rows})...")
                progress_bar.progress((index) / total_rows)
                
                if hour.get("skip_calc", False):
                    row_dict = {
                        "Date": hour["date_string_final"],
                        "Time": hour["time_display"], 
                        "Air_Temp_F": hour.get('temperature_f', "N/A") if hour.get('temperature_f') is not None else "N/A", 
                        "Humidity_Pct": hour.get('relative_humidity_percent', "N/A") if hour.get('relative_humidity_percent') is not None else "N/A", 
                        "Wind_Speed_mph": hour.get('wind_speed_mph', "N/A") if hour.get('wind_speed_mph') is not None else "N/A",
                        "Barometric_Pressure_inHg": hour.get('barometric_pressure_inhg', "N/A") if hour.get('barometric_pressure_inhg') is not None else "N/A",
                        "Sun_WBGT_F": "N/A", 
                        "Shade_WBGT_F": "N/A", 
                        "Workload": hour["workload_label"], 
                        "Adjusted_Watts": hour["final_watts"]
                    }
                    row_dict[limit_key] = "N/A"
                    row_dict[alert_key] = "N/A"
                    row_dict["Safety_Status"] = "Data Missing"
                    row_dict["Weather_Data_Source"] = data_source_label
                    row_dict["Notes"] = hour.get("note_additions", "Key variables missing. Suggest a run with Open-Meteo.")
                    
                    computed_results.append(row_dict)
                    continue
                
                row_fallback = False
                sun_f, shade_f = 0.0, 0.0
                
                orig_temp = float(hour['temperature_f'])
                orig_rh = int(hour['relative_humidity_percent'])
                orig_ws = float(hour['wind_speed_mph'])
                orig_pres = float(hour['barometric_pressure_inhg'])
                
                safe_temp = max(min(orig_temp, 120.0), 32.0)
                safe_rh = max(min(orig_rh, 100), 1)
                safe_ws = max(min(orig_ws, 50.0), 0.0)
                safe_pres = max(min(orig_pres, 32.0), 25.0)

                notes_list = []
                if "note_additions" in hour and hour["note_additions"]:
                    notes_list.append(hour["note_additions"])
                if orig_temp < 32.0: notes_list.append("Air Temp clamped up to 32.0 °F")
                elif orig_temp > 120.0: notes_list.append("Air Temp clamped down to 120.0 °F")
                if orig_rh < 1: notes_list.append("RH clamped up to 1%")
                elif orig_rh > 100: notes_list.append("RH clamped down to 100%")
                if st.session_state.location_fallback: notes_list.append("City/State/Zip used (exact location unresolved)")
                if st.session_state.use_caf: notes_list.append(f"CAF Applied: {st.session_state.caf_label}")
                
                notes_str = " | ".join(notes_list) if notes_list else "None"
                
                try:
                    calculator_time = hour.get("calculator_time", f"{hour['hour_24h']:02d}:00")
                    target_frame.locator('input[name="dd"]').fill(str(hour["date_string_final"]))
                    target_frame.locator('input[name="tm"]').fill(calculator_time)
                    target_frame.locator('input[name="lat"]').fill(str(hour["latitude"]))
                    target_frame.locator('input[name="lon"]').fill(str(hour["longitude_absolute"]))
                    
                    target_frame.locator('input[name="temp"]').fill(str(safe_temp))
                    target_frame.locator('input[name="rh"]').fill(str(safe_rh))
                    target_frame.locator('input[name="ws"]').fill(str(safe_ws))
                    target_frame.locator('input[name="pres"]').fill(str(safe_pres))
                    
                    try: 
                        target_frame.locator('select[name="tz"]').select_option(value=hour["tz_value"], timeout=100)
                    except Exception: 
                        pass
                    
                    time.sleep(0.1)
                    target_frame.locator('input[value="Submit"]').click()
                    
                    sun_wbgt, shade_wbgt = "---", "---"
                    for _ in range(30):  
                        time.sleep(0.1)
                        live_sun_val = target_frame.locator('input[name="wbgt_sun"]').input_value()
                        if live_sun_val and live_sun_val != "---" and live_sun_val.strip() != "":
                            sun_wbgt = live_sun_val.strip()
                            shade_wbgt = target_frame.locator('input[name="wbgt_shade"]').input_value().strip()
                            break
                    
                    if "/" in sun_wbgt:
                        sun_f = float(sun_wbgt.split("/")[1].replace("F","").strip())
                        shade_f = float(shade_wbgt.split("/")[1].replace("F","").strip())
                    else: 
                        row_fallback = True
                except Exception:
                    row_fallback = True
                
                if row_fallback:
                    st.session_state.fallback_active = True
                    sun_f = calculate_wbgt_meteorological_fallback(orig_temp, orig_rh, orig_ws, hour['hour_24h'], is_sun=True)
                    shade_f = calculate_wbgt_meteorological_fallback(orig_temp, orig_rh, orig_ws, hour['hour_24h'], is_sun=False)
                    notes_str = "Offline Stull Fallback Used" if notes_str == "None" else notes_str + " | Offline Stull Fallback Used"
                
                if st.session_state.use_caf:
                    sun_f = round(sun_f + st.session_state.caf_value, 1)
                    shade_f = round(shade_f + st.session_state.caf_value, 1)
                    
                adjusted_watts = hour["final_watts"]
                limit_c = 56.7 - (11.5 * math.log10(adjusted_watts))
                alert_c = 59.9 - (14.1 * math.log10(adjusted_watts))
                
                limit_f = round((limit_c * 1.8) + 32, 1)
                alert_f = round((alert_c * 1.8) + 32, 1)
                
                status = "Normal"
                if sun_f > limit_f or shade_f > limit_f: 
                    status = f"BREACH: {limit_name}"
                elif sun_f > alert_f or shade_f > alert_f: 
                    status = f"WARNING: {alert_name}"
                
                row_dict = {
                    "Date": hour["date_string_final"],
                    "Time": hour["time_display"], 
                    "Air_Temp_F": orig_temp, 
                    "Humidity_Pct": orig_rh, 
                    "Wind_Speed_mph": orig_ws,
                    "Barometric_Pressure_inHg": orig_pres,
                    "Sun_WBGT_F": sun_f, 
                    "Shade_WBGT_F": shade_f, 
                    "Workload": hour["workload_label"], 
                    "Adjusted_Watts": adjusted_watts
                }
                row_dict[limit_key] = limit_f
                row_dict[alert_key] = alert_f
                row_dict["Safety_Status"] = status
                row_dict["Weather_Data_Source"] = data_source_label
                row_dict["Notes"] = notes_str
                
                computed_results.append(row_dict)
                
            browser.close()
            
    except Exception:
        st.session_state.fallback_active = True
        computed_results = []
        for index, hour in enumerate(hourly_data):
            if hour.get("skip_calc", False):
                row_dict = {
                    "Date": hour["date_string_final"],
                    "Time": hour["time_display"], 
                    "Air_Temp_F": hour.get('temperature_f', "N/A") if hour.get('temperature_f') is not None else "N/A", 
                    "Humidity_Pct": hour.get('relative_humidity_percent', "N/A") if hour.get('relative_humidity_percent') is not None else "N/A", 
                    "Wind_Speed_mph": hour.get('wind_speed_mph', "N/A") if hour.get('wind_speed_mph') is not None else "N/A",
                    "Barometric_Pressure_inHg": hour.get('barometric_pressure_inhg', "N/A") if hour.get('barometric_pressure_inhg') is not None else "N/A",
                    "Sun_WBGT_F": "N/A", 
                    "Shade_WBGT_F": "N/A", 
                    "Workload": hour["workload_label"], 
                    "Adjusted_Watts": hour["final_watts"]
                }
                row_dict[limit_key] = "N/A"
                row_dict[alert_key] = "N/A"
                row_dict["Safety_Status"] = "Data Missing"
                row_dict["Weather_Data_Source"] = data_source_label
                row_dict["Notes"] = hour.get("note_additions", "Key variables missing. Suggest a run with Open-Meteo.")
                
                computed_results.append(row_dict)
                continue

            orig_temp = float(hour['temperature_f'])
            orig_rh = int(hour['relative_humidity_percent'])
            orig_ws = float(hour['wind_speed_mph'])
            orig_pres = float(hour['barometric_pressure_inhg'])
            
            sun_f = calculate_wbgt_meteorological_fallback(orig_temp, orig_rh, orig_ws, hour['hour_24h'], is_sun=True)
            shade_f = calculate_wbgt_meteorological_fallback(orig_temp, orig_rh, orig_ws, hour['hour_24h'], is_sun=False)
            
            if st.session_state.use_caf:
                sun_f = round(sun_f + st.session_state.caf_value, 1)
                shade_f = round(shade_f + st.session_state.caf_value, 1)
            
            notes_list = []
            if "note_additions" in hour and hour["note_additions"]:
                notes_list.append(hour["note_additions"])
            if st.session_state.location_fallback: notes_list.append("City/State/Zip used (exact location unresolved)")
            if st.session_state.use_caf: notes_list.append(f"CAF Applied: {st.session_state.caf_label}")
            notes_list.append("Offline Stull Fallback Used")
            notes_str = " | ".join(notes_list)
            
            adjusted_watts = hour["final_watts"]
            limit_c = 56.7 - (11.5 * math.log10(adjusted_watts))
            alert_c = 59.9 - (14.1 * math.log10(adjusted_watts))
            limit_f = round((limit_c * 1.8) + 32, 1)
            alert_f = round((alert_c * 1.8) + 32, 1)
            
            status = "Normal"
            if sun_f > limit_f or shade_f > limit_f: 
                status = f"BREACH: {limit_name}"
            elif sun_f > alert_f or shade_f > alert_f: 
                status = f"WARNING: {alert_name}"
            
            row_dict = {
                "Date": hour["date_string_final"],
                "Time": hour["time_display"], 
                "Air_Temp_F": orig_temp, 
                "Humidity_Pct": orig_rh, 
                "Wind_Speed_mph": orig_ws,
                "Barometric_Pressure_inHg": orig_pres,
                "Sun_WBGT_F": sun_f, 
                "Shade_WBGT_F": shade_f, 
                "Workload": hour["workload_label"], 
                "Adjusted_Watts": adjusted_watts
            }
            row_dict[limit_key] = limit_f
            row_dict[alert_key] = alert_f
            row_dict["Safety_Status"] = status
            row_dict["Weather_Data_Source"] = data_source_label
            row_dict["Notes"] = notes_str
            
            computed_results.append(row_dict)

    progress_bar.progress(1.0)
    status_text.text("Processing operation completed successfully.")
    return computed_results

# =====================================================================
# MATPLOTLIB GRAPHICS COMPLIANCE GENERATOR
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
        box_x = min_w - w_padding
        box_w = (max_w - min_w) + (w_padding * 2)
        box_y = min_wbgt - 1.5
        box_h = (max_wbgt - min_wbgt) + 3.0
        
        rect = patches.Rectangle((box_x, box_y), box_w, box_h,
                                 linewidth=1.5, edgecolor='none', facecolor='#E6D8E7', alpha=0.4,
                                 label='Shift Exposure Envelope Box')
        ax.add_patch(rect)
    
    if use_caf:
        if x_watts:
            ax.scatter(x_watts, y_sun, color='darkred', marker='d', s=130, zorder=5, label='Effective Sun WBGT (CAF-Adjusted)')
            ax.scatter(x_watts, y_shade, color='darkblue', marker='p', s=120, zorder=5, label='Effective Shade WBGT (CAF-Adjusted)')
        
        for i, r in enumerate(results):
            if r["Sun_WBGT_F"] != "N/A":
                ax.annotate(r["Time"], (r["Adjusted_Watts"], r["Sun_WBGT_F"]), textcoords="offset points", xytext=(6, 5), fontsize=8, color='darkred', fontweight='bold')
                ax.annotate(r["Time"], (r["Adjusted_Watts"], r["Shade_WBGT_F"]), textcoords="offset points", xytext=(6, -12), fontsize=8, color='darkblue')
    else:
        if x_watts:
            ax.scatter(x_watts, y_sun, color='red', marker='o', s=120, zorder=5, label='Hourly Exposure (Sun WBGT)')
            ax.scatter(x_watts, y_shade, color='blue', marker='s', s=100, zorder=5, label='Hourly Exposure (Shade WBGT)')
        
        for i, r in enumerate(results):
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
    if x_watts:
        y_max_bound = max(98, max_wbgt + 5)
    else:
        y_max_bound = 98
        
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
    
    if not geo: 
        return
        
    st.write("Please confirm that the retrieved location matches your intended site before proceeding:")
    
    col_entered, col_matched = st.columns(2)
    with col_entered:
        st.markdown("**User Entered Location:**")
        st.info(geo["raw_entered"])
    with col_matched:
        st.markdown("**Retrieved / Geocoded Site:**")
        st.success(geo["matched_address"])
        
    st.caption(f"📍 **Coordinates:** Latitude {geo['latitude']}, Longitude {geo['longitude']}")
    
    if geo.get("fallback_used"):
        st.warning("⚠️ Street address could not be specifically pinpointed. Geocoding fell back to City/State/ZIP center.")

    col_confirm, col_edit = st.columns(2)
    with col_confirm:
        if st.button("Confirm & Proceed →", type="primary", use_container_width=True):
            st.session_state.confirmed_geo = geo
            st.session_state.pending_geo = None
            
            target_date = geo["target_date"]
            start_hour, end_hour = geo["shift_hours"]
            worker_weight = geo["worker_weight"]
            
            date_str = target_date.strftime("%Y-%m-%d")
            
            with st.spinner("Retrieving atmospheric matrices from Open-Meteo provider..."):
                weather_res = fetch_weather_native(geo["latitude"], geo["longitude"], date_str, st.session_state.is_forecast)
            
            if "error" in weather_res or "hourly" not in weather_res or not weather_res["hourly"]:
                st.error(weather_res.get("error", "Could not pull valid weather timeline matrices for this date/location."))
            else:
                hourly = weather_res["hourly"]
                grid_lat = weather_res["grid_latitude"]
                grid_lon = weather_res["grid_longitude"]
                
                dist_miles = haversine_distance(geo["latitude"], geo["longitude"], grid_lat, grid_lon)
                
                st.session_state.location_meta = {
                    "user_entered": geo["raw_entered"],
                    "validated": geo["matched_address"],
                    "target_lat": geo["latitude"],
                    "target_lon": geo["longitude"],
                    "grid_lat": grid_lat,
                    "grid_lon": grid_lon,
                    "distance_miles": dist_miles,
                    "data_source": "Open-Meteo"
                }
                    
                st.session_state.raw_weather_debug = weather_res.get("raw_debug", None)
                st.session_state.raw_noaa_df_export = None
                
                active_rows = []
                for i in range(len(hourly["time"])):
                    hr_int = int(hourly["time"][i].split("T")[1].split(":")[0])
                    if start_hour <= hr_int <= end_hour:
                        ampm = "12:00 AM" if hr_int==0 else ("12:00 PM" if hr_int==12 else (f"{hr_int-12}:00 PM" if hr_int>12 else f"{hr_int}:00 AM"))
                        
                        active_rows.append({
                            "date_string_final": target_date.strftime("%m/%d/%Y"), 
                            "time_display": ampm, 
                            "hour_24h": hr_int,
                            "calculator_time": f"{hr_int:02d}:00",
                            "skip_calc": False,
                            "noaa_matched_timestamp": "N/A",
                            "note_additions": "",
                            "user_entered_address": geo["raw_entered"],
                            "validated_address": geo["matched_address"],
                            "latitude": geo["latitude"], "longitude": geo["longitude"],
                            "grid_latitude": grid_lat, "grid_longitude": grid_lon,
                            "grid_distance_miles": dist_miles,
                            "longitude_absolute": abs(geo["longitude"]), 
                            "tz_value": get_osha_tz_value(geo["longitude"]),
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
    "📅 Switch to Future Forecast Mode (For Planning & Prediction, NOAA CSV Upload unavailable for this mode)", 
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

with st.expander("📖 How-To Guide: Running Heat Stress Assessments"):
    st.markdown("""
    ### Step 1: Enter Location and Date
    1. **Choose your weather source**: Pick [Open-Meteo](https://open-meteo.com/) to use standard weather history or forecasts, or [NOAA Station Data](https://www.ncei.noaa.gov/maps/lcd/) if you have a local CSV weather file to upload.
    2. **Type in your location**: Enter your street address, city, state, or ZIP code, or switch on the GPS option if you prefer exact coordinates. (If you uploaded a NOAA file, this happens automatically).
    3. **Pick your shift details**: Select the date of your work or inspection, choose your working hours using the time slider, and enter the worker's weight.
    4. **Load the data**: Click the process button to grab the weather details and check your site's location.

    ---
    ### Step 2: Set Work Levels and Clothing
    1. **Pick your safety standard**: Choose between **ACGIH** (Default / Primary) or **NIOSH** guidelines.
    2. **Clothing Adjustment Factors**: Turn on the clothing adjustment option, check the [Clothing Adjustment Factors](https://www.osha.gov/heat-exposure/hazards#ClothingAdjustmentFactors) reference guide, and pick the matching outfit type.
    3. **Choose how hard people are working**: 
       * **Standard mode**: Pick simple activity levels for each hour (like Light, Moderate, or Heavy) from the dropdowns.
       * **Advanced mode**: If you prefer, enter specific metabolic energy values or worker details like age and height for a more tailored estimate. This is advanced and should only be used when familiar with these workload estimates.
    4. **Run the calculation**: Click to start the analysis so the tool can run the numbers and check safety limits. This is done by running the [OSHA Outdoor WBGT Calculator](https://www.osha.gov/heat-exposure/wbgt-calculator) in the background and collecting the results.

    ---
    ### Step 3: Review Results and Download Reports
    1. **Check your details**: Look over the location summary to make sure the weather station and address match your site.
    2. **View the exposure graphic**: Look at the chart to see how your shift's conditions compare against safe heat exposure limits.
    3. **Read the hourly table and notes**: Check the table for a breakdown of temperatures, humidity, wind, and safety status for every hour of your shift. **Check the notes** column to find any additional notations, such as missing data, automated fallback alerts, or data substitutions.
    4. **Save your report**: Click the download button to save a complete Excel spreadsheet containing all your data, notes, and exposure graphics for your records.
    """)

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

# --- WIZARD STEP 1: UI-BASED TARGET PARAMETER INPUTS ---
if st.session_state.step == 1:
    st.subheader("Step 1: Set Target Parameters & Profile Matrix")
    
    st.markdown("**Meteorological Data Provider**")
    data_source = st.radio("Select Provider:", ["Open-Meteo (Default)", "NOAA Station Data (Local CSV Upload)"])
    
    use_gps = False
    uploaded_noaa_csv = None
    
    if "NOAA" in data_source:
        st.info("💡 **NOAA Local Climatological Data (LCD):** Upload a CSV dataset downloaded from the NOAA NCEI tool. The application will automatically extract GPS coordinates and timeline details from the file.")
        uploaded_noaa_csv = st.file_uploader("Upload NOAA LCD Weather File (.csv)", type=["csv"])
    else:
        col_loc_header, col_loc_toggle = st.columns([3, 1])
        with col_loc_header:
            st.markdown("**Location Details**")
        with col_loc_toggle:
            use_gps = st.toggle("Use GPS Coordinates", value=False)
        
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
            
    with c_shift2: start_hour, end_hour = st.slider("Shift Operating Hours (24-Hour Clock)", min_value=0, max_value=23, value=(8, 16), format="%d:00")
    with c_shift3: worker_weight = st.number_input("Employee Weight (lbs)", min_value=50.0, max_value=400.0, value=154.0, step=1.0)
    
    button_text = "Process Weather Data"
    
    if st.button(button_text, type="primary"):
        if st.session_state.is_forecast and "NOAA" in data_source:
            st.error("Cannot use NOAA Historical CSV Data for future forecasts. Please switch to Open-Meteo or pick a past date.")
        elif "NOAA" in data_source:
            if uploaded_noaa_csv is None:
                st.error("Please upload a NOAA CSV file before proceeding.")
            else:
                with st.spinner("Processing NOAA CSV data..."):
                    noaa_result = process_weather_noaa_csv(uploaded_noaa_csv, target_date, start_hour, end_hour)
                    if "error" in noaa_result:
                        st.error(noaa_result["error"])
                    else:
                        active_rows = []
                        for hr_int in range(start_hour, end_hour + 1):
                            hr_data = noaa_result["hourly_records"][hr_int]
                            ampm = "12:00 AM" if hr_int==0 else ("12:00 PM" if hr_int==12 else (f"{hr_int-12}:00 PM" if hr_int>12 else f"{hr_int}:00 AM"))
                            
                            active_rows.append({
                                "date_string_final": target_date.strftime("%m/%d/%Y"), 
                                "time_display": ampm, 
                                "hour_24h": hr_int,
                                "calculator_time": hr_data.get("calculator_time", f"{hr_int:02d}:00"),
                                "skip_calc": hr_data.get("skip_calc", False),
                                "noaa_matched_timestamp": hr_data.get("matched_timestamp", "N/A"),
                                "note_additions": hr_data.get("note_additions", ""),
                                "user_entered_address": "NOAA CSV Upload",
                                "validated_address": f"Station: {noaa_result['station_name']}",
                                "latitude": noaa_result["latitude"], "longitude": noaa_result["longitude"],
                                "grid_latitude": noaa_result["latitude"], "grid_longitude": noaa_result["longitude"],
                                "grid_distance_miles": 0.0,
                                "longitude_absolute": abs(noaa_result["longitude"]), 
                                "tz_value": get_osha_tz_value(noaa_result["longitude"]),
                                "temperature_f": hr_data["temperature_f"], 
                                "relative_humidity_percent": hr_data["relative_humidity_percent"], 
                                "wind_speed_mph": hr_data["wind_speed_mph"], 
                                "barometric_pressure_inhg": hr_data["barometric_pressure_inhg"]
                            })
                        
                        st.session_state.location_meta = {
                            "user_entered": "NOAA CSV Upload",
                            "validated": f"Station: {noaa_result['station_name']}",
                            "target_lat": noaa_result["latitude"],
                            "target_lon": noaa_result["longitude"],
                            "grid_lat": noaa_result["latitude"],
                            "grid_lon": noaa_result["longitude"],
                            "distance_miles": 0.0,
                            "data_source": "NOAA Station Data (Local CSV Upload)",
                            "station_name": noaa_result["station_name"]
                        }
                        
                        st.session_state.raw_noaa_df_export = noaa_result["raw_noaa_df"]
                        st.session_state.raw_weather_debug = None
                        st.session_state.final_hourly_rows = active_rows
                        st.session_state.worker_weight = worker_weight
                        st.session_state.location_fallback = False
                        st.session_state.step = 2
                        st.rerun()
        else:
            if use_gps:
                try:
                    lat_val = float(target_lat_in)
                    lon_val = float(target_lon_in)
                    st.session_state.pending_geo = {
                        "latitude": lat_val,
                        "longitude": lon_val,
                        "matched_address": f"Exact Coordinates ({lat_val}, {lon_val})",
                        "raw_entered": f"GPS: {lat_val}, {lon_val}",
                        "fallback_used": False,
                        "target_date": target_date,
                        "shift_hours": (start_hour, end_hour),
                        "worker_weight": worker_weight,
                        "data_source": data_source
                    }
                    st.rerun()
                except ValueError:
                    st.error("Please enter valid numerical values for Latitude and Longitude.")
            else:
                if not target_city.strip() and not target_zip.strip() and not target_street.strip():
                    st.warning("Please supply at least a City/State, ZIP Code, or Street Address.")
                else:
                    with st.spinner("Resolving location coordinates..."):
                        geo, fallback_used, raw_entered_address = resolve_location(target_street, target_city, target_state, target_zip, mapbox_secret)
                        
                        if "error" in geo:
                            st.error(geo["error"])
                        else:
                            st.session_state.pending_geo = {
                                "latitude": geo["latitude"],
                                "longitude": geo["longitude"],
                                "matched_address": geo.get("matched_address", raw_entered_address),
                                "raw_entered": raw_entered_address,
                                "fallback_used": fallback_used and bool(target_street.strip()),
                                "target_date": target_date,
                                "shift_hours": (start_hour, end_hour),
                                "worker_weight": worker_weight,
                                "data_source": data_source
                            }
                            st.rerun()

# --- WIZARD STEP 2: DYNAMIC HOURLY WORKLOAD DESIGNER ---
elif st.session_state.step == 2:
    st.subheader("Step 2: Assign Hourly Worker Metabolism / Workloads")
    
    st.markdown("### Heat Stress Standard")
    standard_choice = st.radio(
        "Select Evaluation Standard", 
        ["ACGIH (Default / Primary)", "NIOSH"],
        index=0 if "ACGIH" in st.session_state.get("standard_choice", "ACGIH") else 1,
        help="ACGIH TLVs® and Action Limits are optimized for professional heat stress evaluation. NIOSH RELs/RALs are available as secondary reference."
    )
    st.session_state.standard_choice = standard_choice
    
    if "ACGIH" in standard_choice:
        st.info("ℹ️ **ACGIH® Evaluation Active:** Computations will evaluate exposure against ACGIH® Threshold Limit Values (TLV®) and Action Limits (AL).")
    else:
        st.info("ℹ️ **NIOSH Evaluation Active:** Computations will evaluate exposure against NIOSH Recommended Exposure Limits (REL) and Recommended Alert Limits (RAL).")
    
    st.markdown("### Clothing & PPE Adjustment Factor (Optional)")
    use_caf = st.toggle("Apply Clothing Adjustment Factor (CAF)", value=st.session_state.use_caf)
    caf_value = 0.0
    caf_label = "Standard Work Clothes (0.0 °F)"
    
    if use_caf:
        caf_dict = {
            "Short sleeves and pants (-1.8 °F)": -1.8,
            "Work clothes / Cloth coveralls (0.0 °F)": 0.0,
            "SMS polypropylene coveralls (+0.9 °F)": 0.9,
            "Polyolefin coveralls (+1.8 °F)": 1.8,
            "Double-layer woven clothing (+5.4 °F)": 5.4,
            "Limited-use vapor-barrier coveralls (+19.8 °F)": 19.8
        }
        caf_choice = st.selectbox("Select Clothing Ensemble / PPE Type", list(caf_dict.keys()))
        caf_value = caf_dict[caf_choice]
        caf_label = caf_choice
        st.info(f"ℹ️ Active CAF Correction: **{caf_value:+.1f} °F** will be added to the calculated WBGT values.")
        
    st.session_state.use_caf = use_caf
    st.session_state.caf_value = caf_value
    st.session_state.caf_label = caf_label
    
    st.divider()
    use_clinical = st.toggle("Advanced Clinical / Custom Workload", value=False)
    
    account_for_weight = True
    clinical_method = "Standard Ainsworth (Weight Only)"
    sex = "Male"
    age = 35
    height_in = 70.0
    
    if use_clinical:
        st.info("Clinical Mode Active: Enter specific Ainsworth MET values for each hour below.")
        account_for_weight = st.toggle("Account for employee physiological data?", value=True)
        
        if account_for_weight:
            clinical_method = st.radio(
                "Metabolic Calculation Method", 
                ["Standard Ainsworth (Weight Only)", "Corrected METs (Mifflin-St Jeor)"],
                help="Standard uses a flat 1 kcal/kg/hr baseline. Corrected uses the clinical Mifflin-St Jeor equation for precise Resting Metabolic Rate."
            )
            if clinical_method == "Corrected METs (Mifflin-St Jeor)":
                st.markdown("**Enter Worker Biometrics:**")
                c_bio1, c_bio2, c_bio3 = st.columns(3)
                sex = c_bio1.selectbox("Biological Sex", ["Male", "Female"])
                age = c_bio2.number_input("Age (Years)", min_value=18, max_value=100, value=35)
                height_in = c_bio3.number_input("Height (Inches)", min_value=40.0, max_value=90.0, value=70.0)
    
    st.divider()
    
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
            st.session_state.step = 1
            st.rerun()
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
                            if sex == "Male": 
                                rmr_kcal_day = (10 * worker_kg) + (6.25 * height_cm) - (5 * age) + 5
                            else: 
                                rmr_kcal_day = (10 * worker_kg) + (6.25 * height_cm) - (5 * age) - 161
                            rmr_kcal_hr = rmr_kcal_day / 24.0
                            calc_watts = met_val * rmr_kcal_hr * 1.16222
                    
                    row["workload_label"] = f"{met_val} METs"
                    row["final_watts"] = round(calc_watts, 1)
                
            with st.spinner("Executing calculations..."):
                data_source_val = st.session_state.location_meta.get("data_source", "Open-Meteo")
                if "NOAA" in data_source_val:
                    stn_name = st.session_state.location_meta.get("station_name", "Unknown Station")
                    data_source_label = f"NOAA LCD Station Data ({stn_name})"
                else:
                    data_source_label = (
                        "Open-Meteo Forecast (NOAA HRRR / GFS Models)" 
                        if st.session_state.is_forecast 
                        else "Open-Meteo Archive (ERA5 / NOAA Station Reanalysis)"
                    )
                results = run_browser_automation(st.session_state.final_hourly_rows, data_source_label, st.session_state.standard_choice)
                
            if results:
                st.session_state.results = results
                st.session_state.step = 3
                st.rerun()

    st.divider()
    if st.session_state.raw_weather_debug:
        with st.expander("🔍 Raw Meteorological Data Diagnostics (Troubleshooting View)", expanded=False):
            st.markdown("Inspect this data to verify the raw atmospheric matrices returned by the API provider:")
            st.json(st.session_state.raw_weather_debug)
            
            if st.session_state.final_hourly_rows:
                st.markdown("**Parsed Hourly Values & Source Timestamp Fed to OSHA Calculator:**")
                debug_df = pd.DataFrame(st.session_state.final_hourly_rows)[["time_display", "noaa_matched_timestamp", "temperature_f", "relative_humidity_percent", "wind_speed_mph", "barometric_pressure_inhg"]]
                st.dataframe(debug_df, use_container_width=True)

# --- WIZARD STEP 3: INTERACTIVE REPORT VIEWER & EXPORT ---
elif st.session_state.step == 3:
    st.subheader("Step 3: Compliance Engineering Summary Analysis Output")
    
    if st.session_state.fallback_active: 
        st.warning("⚠️ **Playwright Fallback Active**: The system successfully estimated WBGT offline utilizing Stull's equation.")
    else: 
        st.success("✅ Wet Bulb Globe Temperature (WBGT) data compiled successfully.")
        
    meta = st.session_state.get("location_meta", {})
    if meta:
        station_info = ""
        if "station_name" in meta:
            station_info = f"\n* **NOAA LCD Weather Station:** {meta.get('station_name')}"
            
        st.info(
            f"📍 **Address Audit Trail:**\n"
            f"* **Entered Location:** {meta.get('user_entered', 'N/A')}\n"
            f"* **Validated/Geocoded Location:** {meta.get('validated', 'N/A')} (Lat: {meta.get('target_lat')}, Lon: {meta.get('target_lon')})\n"
            f"* **Weather Point:** Lat {meta.get('grid_lat')}, Lon {meta.get('grid_lon')}{station_info}\n"
            f"* **Distance to Weather Data Point:** **{meta.get('distance_miles', 0.0):.2f} miles**"
        )
        
    fig = generate_compliance_plot(
        st.session_state.results, 
        st.session_state.worker_weight, 
        st.session_state.is_forecast, 
        st.session_state.use_caf, 
        st.session_state.caf_label,
        st.session_state.standard_choice
    )
    st.pyplot(fig)
    
    st.subheader("Raw Exposure Tracking Metrics Matrix")
    
    df_results = pd.DataFrame(st.session_state.results)
    st.dataframe(df_results, use_container_width=True)
    
    if st.session_state.results:
        raw_exposure_date = st.session_state.results[0]["Date"]
        file_date_str = datetime.strptime(raw_exposure_date, "%m/%d/%Y").strftime("%Y%m%d")
    else:
        file_date_str = datetime.now().strftime("%Y%m%d")
    
    img_buffer = io.BytesIO()
    fig.savefig(img_buffer, format="png", bbox_inches="tight", dpi=150)
    img_buffer.seek(0)
    
    excel_buffer = io.BytesIO()
    
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        df_results.to_excel(writer, sheet_name="Exposure_Data", index=False)
        
        meta_df = pd.DataFrame([{
            "User_Entered_Address": meta.get("user_entered", "N/A"),
            "Validated_Address": meta.get("validated", "N/A"),
            "Target_Latitude": meta.get("target_lat"),
            "Target_Longitude": meta.get("target_lon"),
            "Weather_Grid_Latitude": meta.get("grid_lat"),
            "Weather_Grid_Longitude": meta.get("grid_lon"),
            "Grid_Distance_Miles": meta.get("distance_miles")
        }])
        meta_df.to_excel(writer, sheet_name="Location_Details", index=False)
        
        if st.session_state.raw_noaa_df_export is not None:
            st.session_state.raw_noaa_df_export.to_excel(writer, sheet_name="Raw_NOAA_Data", index=False)
        
        workbook = writer.book
        exposure_sheet = writer.sheets["Exposure_Data"]
        
        excel_img = OpenPyxlImage(img_buffer)
        excel_img.width = 650
        excel_img.height = 380
        
        insert_row = len(df_results) + 4
        exposure_sheet.add_image(excel_img, f"A{insert_row}")
        
    excel_file_data = excel_buffer.getvalue()
    file_name = f"Heat_Stress_Report_{file_date_str}.xlsx"
    
    st.download_button(
        label="Download Compliance Report Spreadsheet (.XLSX)",
        data=excel_file_data,
        file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    
    st.divider()
    if st.button("🔄 Execute Fresh Inspection Run"):
        st.session_state.step = 1
        st.session_state.pending_geo = None
        st.session_state.final_hourly_rows = None
        st.session_state.raw_weather_debug = None
        st.session_state.raw_noaa_df_export = None
        st.session_state.fallback_active = False
        st.session_state.location_fallback = False
        st.session_state.use_caf = False
        st.session_state.caf_value = 0.0
        st.session_state.location_meta = {}
        st.rerun()
