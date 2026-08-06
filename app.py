import os
import subprocess
import streamlit as st

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
# APPLICATION IMPORTS
# =====================================================================
import time
import math
import csv
import io
import requests
import urllib.parse
from datetime import datetime, date, timedelta
from playwright.sync_api import sync_playwright
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np

# =====================================================================
# STREAMLIT CONFIGURATION & PERSISTENCE STATE
# =====================================================================
st.set_page_config(page_title="OSHA-WBGT Localized Calculator", layout="wide")

if "step" not in st.session_state:
    st.session_state.step = 1
if "resolved_geo" not in st.session_state:
    st.session_state.resolved_geo = None
if "final_hourly_rows" not in st.session_state:
    st.session_state.final_hourly_rows = None
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
    st.session_state.standard_choice = "NIOSH (Default)"
if "location_meta" not in st.session_state:
    st.session_state.location_meta = {}

def clear_resolved_geo():
    """Callback to clear the confirmed location if the user modifies an input field."""
    if "resolved_geo" in st.session_state:
        st.session_state.resolved_geo = None

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

def fetch_weather_native(lat: float, lon: float, date_str: str, is_forecast: bool) -> dict:
    url = "https://api.open-meteo.com/v1/forecast" if is_forecast else "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon, "start_date": date_str, "end_date": date_str,
        "hourly": ["temperature_2m", "relative_humidity_2m", "surface_pressure", "wind_speed_10m"],
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto"
    }
    try:
        response = requests.get(url, params=params)
        if response.status_code != 200:
            return {"error": f"Weather API blocked the request (HTTP {response.status_code})."}
        data = response.json()
        return {
            "hourly": data.get("hourly", {}),
            "grid_latitude": data.get("latitude", lat),
            "grid_longitude": data.get("longitude", lon)
        }
    except Exception as e:
        return {"error": f"Weather System Error: {str(e)}"}

def calculate_wbgt_meteorological_fallback(temp_f, rh_pct, wind_mph, hour_24h=12, is_sun=True):
    tc = (temp_f - 32) * 5.0 / 9.0
    rh = rh_pct
    tw = (tc * math.atan(0.151977 * (rh + 8.313766)**0.5) 
          + math.atan(tc + rh) 
          - math.atan(rh - 1.676331) 
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
            except Exception as conn_error:
                st.warning(f"Connection warning: {conn_error}. Attempting calculations directly.")
                target_frame = page

            total_rows = len(hourly_data)
            for index, hour in enumerate(hourly_data):
                status_text.text(f"Scraping OSHA Calculator for hour: {hour['time_display']} ({index+1}/{total_rows})...")
                progress_bar.progress((index) / total_rows)
                
                row_fallback = False
                sun_f, shade_f = 0.0, 0.0
                
                orig_temp, orig_rh, orig_ws, orig_pres = float(hour['temperature_f']), int(hour['relative_humidity_percent']), float(hour['wind_speed_mph']), float(hour['barometric_pressure_inhg'])
                safe_temp, safe_rh, safe_ws, safe_pres = max(min(orig_temp, 120.0), 32.0), max(min(orig_rh, 100), 1), max(min(orig_ws, 50.0), 0.0), max(min(orig_pres, 32.0), 25.0)

                notes_list = []
                if orig_temp < 32.0: notes_list.append("Air Temp rounded up to 32.0 °F")
                elif orig_temp > 120.0: notes_list.append("Air Temp rounded down to 120.0 °F")
                if orig_rh < 1: notes_list.append("RH rounded up to 1%")
                elif orig_rh > 100: notes_list.append("RH rounded down to 100%")
                if st.session_state.location_fallback: notes_list.append("City/State/Zip were used as exact location could not be resolved")
                if st.session_state.use_caf: notes_list.append(f"CAF Applied: {st.session_state.caf_label}")
                
                notes_str = " | ".join(notes_list) if notes_list else "None"
                
                try:
                    formatted_time = f"{hour['hour_24h']:02d}:00"
                    target_frame.locator('input[name="dd"]').fill(str(hour["date_string_final"]))
                    target_frame.locator('input[name="tm"]').fill(formatted_time)
                    target_frame.locator('input[name="lat"]').fill(str(hour["latitude"]))
                    target_frame.locator('input[name="lon"]').fill(str(hour["longitude_absolute"]))
                    
                    target_frame.locator('input[name="temp"]').fill(str(safe_temp))
                    target_frame.locator('input[name="rh"]').fill(str(safe_rh))
                    target_frame.locator('input[name="ws"]').fill(str(safe_ws))
                    target_frame.locator('input[name="pres"]').fill(str(safe_pres))
                    
                    try: target_frame.locator('select[name="tz"]').select_option(value=hour["tz_value"], timeout=100)
                    except Exception: pass
                    
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
                    else: row_fallback = True
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
                if sun_f > limit_f or shade_f > limit_f: status = f"BREACH: {limit_name}"
                elif sun_f > alert_f or shade_f > alert_f: status = f"WARNING: {alert_name}"
                
                row_dict = {
                    "Date": hour["date_string_final"],
                    "Time": hour["time_display"], 
                    "User_Entered_Address": hour["user_entered_address"],
                    "Validated_Address": hour["validated_address"],
                    "Target_Latitude": hour["latitude"],
                    "Target_Longitude": hour["longitude"],
                    "Weather_Grid_Latitude": hour["grid_latitude"],
                    "Weather_Grid_Longitude": hour["grid_longitude"],
                    "Grid_Distance_Miles": hour["grid_distance_miles"],
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
            orig_temp, orig_rh, orig_ws = float(hour['temperature_f']), int(hour['relative_humidity_percent']), float(hour['wind_speed_mph'])
            orig_pres = float(hour['barometric_pressure_inhg'])
            sun_f = calculate_wbgt_meteorological_fallback(orig_temp, orig_rh, orig_ws, hour['hour_24h'], is_sun=True)
            shade_f = calculate_wbgt_meteorological_fallback(orig_temp, orig_rh, orig_ws, hour['hour_24h'], is_sun=False)
            
            if st.session_state.use_caf:
                sun_f = round(sun_f + st.session_state.caf_value, 1)
                shade_f = round(shade_f + st.session_state.caf_value, 1)
            
            notes_list = []
            if st.session_state.location_fallback: notes_list.append("City/State/Zip were used as exact location could not be resolved")
            if st.session_state.use_caf: notes_list.append(f"CAF Applied: {st.session_state.caf_label}")
            notes_list.append("Offline Stull Fallback Used")
            notes_str = " | ".join(notes_list)
            
            adjusted_watts = hour["final_watts"]
            limit_c = 56.7 - (11.5 * math.log10(adjusted_watts))
            alert_c = 59.9 - (14.1 * math.log10(adjusted_watts))
            limit_f = round((limit_c * 1.8) + 32, 1)
            alert_f = round((alert_c * 1.8) + 32, 1)
            
            status = "Normal"
            if sun_f > limit_f or shade_f > limit_f: status = f"BREACH: {limit_name}"
            elif sun_f > alert_f or shade_f > alert_f: status = f"WARNING: {alert_name}"
            
            row_dict = {
                "Date": hour["date_string_final"],
                "Time": hour["time_display"], 
                "User_Entered_Address": hour["user_entered_address"],
                "Validated_Address": hour["validated_address"],
                "Target_Latitude": hour["latitude"],
                "Target_Longitude": hour["longitude"],
                "Weather_Grid_Latitude": hour["grid_latitude"],
                "Weather_Grid_Longitude": hour["grid_longitude"],
                "Grid_Distance_Miles": hour["grid_distance_miles"],
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
    
    x_watts = [r["Adjusted_Watts"] for r in results]
    y_sun = [r["Sun_WBGT_F"] for r in results]
    y_shade = [r["Shade_WBGT_F"] for r in results]
    
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
        ax.scatter(x_watts, y_sun, color='darkred', marker='d', s=130, zorder=5, label='Effective Sun WBGT (CAF-Adjusted)')
        ax.scatter(x_watts, y_shade, color='darkblue', marker='p', s=120, zorder=5, label='Effective Shade WBGT (CAF-Adjusted)')
        
        for i, r in enumerate(results):
            ax.annotate(r["Time"], (x_watts[i], y_sun[i]), xytext=(5, 5), textcoords="offset points", fontsize=8, color='darkred', weight='bold')
    else:
        ax.scatter(x_watts, y_sun, color='darkred', marker='o', s=110, zorder=5, edgecolor='black', label='Sun WBGT Data Point')
        ax.scatter(x_watts, y_shade, color='darkblue', marker='o', s=110, zorder=5, edgecolor='black', label='Shade WBGT Data Point')
        for i, r in enumerate(results):
            ax.annotate(r["Time"], (x_watts[i], y_sun[i]), xytext=(5, 5), textcoords="offset points", fontsize=8, color='darkred')
    
    date_label = f"({results[0]['Date']} - Forecasted Data)" if is_forecast else f"({results[0]['Date']} - Historical Data)" if results else ""
    title_str = f"OSHA/NIOSH Estimated WBGT Heat Stress Profile vs Limit Curves {date_label}\n"
    title_str += f"Worker Body Mass: {worker_weight} lbs"
    if use_caf: title_str += f" | Clothing Profile: {caf_label}"
    
    ax.set_title(title_str, fontsize=12, fontweight='bold', pad=15)
    ax.set_xlabel('Metabolic Workload (Watts, Mass-Adjusted)', fontsize=11, fontweight='bold')
    ax.set_ylabel('Effective WBGT Exposure Level (°F)', fontsize=11, fontweight='bold')
    
    ax.set_xlim(90, 610)
    ax.set_ylim(65, 115)
    
    ax.fill_between(watts_range, limit_curve_f, 115, color='crimson', alpha=0.08)
    ax.fill_between(watts_range, alert_curve_f, limit_curve_f, color='orange', alpha=0.08)
    ax.fill_between(watts_range, 65, alert_curve_f, color='green', alpha=0.08)
    
    ax.grid(True, linestyle='--', alpha=0.6)
    ax.legend(loc='upper right', framealpha=0.9)
    plt.tight_layout()
    
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=300)
    buf.seek(0)
    return buf

# =====================================================================
# STREAMLIT UI: SIDEBAR CONFIGURATION
# =====================================================================
st.sidebar.title("App Settings & Secrets")
st.sidebar.markdown("---")

st.session_state.standard_choice = st.sidebar.radio("Limit Matrix Reference Standard:", ["NIOSH (Default)", "ACGIH (2022+)"])

mapbox_secret = st.sidebar.text_input("Mapbox API Key (Optional Fallback):", type="password")

st.sidebar.markdown("---")
if st.sidebar.button("Clear Application Cache", type="secondary"):
    st.cache_resource.clear()
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()

st.title("OSHA-WBGT Localized Heat Stress Estimator")
st.markdown("Automated compliance tool utilizing local weather grids and web integration. Calculates estimated Sun and Shade Wet Bulb Globe Temperature (WBGT) thresholds.")

# =====================================================================
# STEP 1: VERIFY TARGET LOCATION & TIMING
# =====================================================================
if st.session_state.step == 1:
    st.subheader("Step 1: Set Target Location & Shift Parameters")
    
    c_mode1, c_mode2 = st.columns([1, 1])
    with c_mode1:
        input_mode = st.radio("Location Input Method", ["Address Search", "Exact GPS Coordinates"], on_change=clear_resolved_geo)

    st.markdown("#### Geographic Selection")
    if input_mode == "Address Search":
        c_addr1, c_addr2, c_addr3, c_addr4 = st.columns([2, 2, 1, 1.5])
        with c_addr1: target_street = st.text_input("Street Address (Optional)", value="", placeholder="e.g., 7339 State Road", on_change=clear_resolved_geo)
        with c_addr2: target_city = st.text_input("City", value="", placeholder="e.g., Philadelphia", on_change=clear_resolved_geo)
        with c_addr3: target_state = st.text_input("State", value="", placeholder="e.g., PA", on_change=clear_resolved_geo)
        with c_addr4: target_zip = st.text_input("ZIP Code", value="", placeholder="e.g., 19136", on_change=clear_resolved_geo)
    else:
        c_gps1, c_gps2 = st.columns(2)
        with c_gps1: target_lat_input = st.text_input("Latitude", value="", placeholder="e.g., 40.0345", on_change=clear_resolved_geo)
        with c_gps2: target_lon_input = st.text_input("Longitude", value="", placeholder="e.g., -75.0181", on_change=clear_resolved_geo)

    st.markdown("#### Meteorological Setup")
    c_date1, c_date2, c_date3, c_date4 = st.columns(4)
    with c_date1: target_date = st.date_input("Inspection Date", value=date.today(), max_value=date.today() + timedelta(days=14))
    with c_date2: start_hour = st.number_input("Shift Start Hour (0-23)", min_value=0, max_value=23, value=11, step=1)
    with c_date3: end_hour = st.number_input("Shift End Hour (0-23)", min_value=0, max_value=23, value=15, step=1)
    with c_date4: worker_weight = st.number_input("Worker Total Mass (lbs)", min_value=90.0, max_value=400.0, value=154.0, step=1.0)
    
    st.markdown("#### Clothing Adjustments (CAF)")
    caf_enabled = st.checkbox("Apply Clothing Adjustment Factor (CAF)?")
    if caf_enabled:
        caf_selection = st.selectbox("Select Worker Ensemble (Applies penalty to WBGT):", [
            "Standard Work Clothes (+0.0)", "Double-Layer Uniform (+5.4)", "Vapor-Barrier Suit (+19.8)"
        ])
    
    if st.button("Verify Location", type="secondary"):
        if input_mode == "Exact GPS Coordinates":
            try:
                lat_val = float(target_lat_input)
                lon_val = float(target_lon_input)
                st.session_state.resolved_geo = {
                    "latitude": lat_val,
                    "longitude": lon_val,
                    "matched_address": f"Exact GPS Input ({lat_val}, {lon_val})",
                    "raw_entered": f"{lat_val}, {lon_val}",
                    "fallback_used": False
                }
            except ValueError:
                st.error("Please enter valid numerical coordinates.")
        else:
            if not target_city.strip() and not target_zip.strip() and not target_street.strip():
                st.warning("Please supply at least a City/State, ZIP Code, or Street Address.")
            elif start_hour > end_hour:
                st.error("End hour must be equal to or greater than the Start hour.")
            else:
                with st.spinner("Resolving coordinates..."):
                    geo, fallback_used, raw_entered_address = resolve_location(target_street, target_city, target_state, target_zip, mapbox_secret)
                    if "error" in geo:
                        st.error(geo["error"])
                    else:
                        st.session_state.resolved_geo = {
                            "latitude": geo["latitude"],
                            "longitude": geo["longitude"],
                            "matched_address": geo.get("matched_address", raw_entered_address),
                            "raw_entered": raw_entered_address,
                            "fallback_used": fallback_used and bool(target_street.strip())
                        }
    
    if st.session_state.resolved_geo:
        st.markdown("---")
        st.markdown("### Location Verification")
        st.success(f"**Matched Location:** {st.session_state.resolved_geo['matched_address']}")
        st.info(f"**Coordinates:** Lat {st.session_state.resolved_geo['latitude']}, Lon {st.session_state.resolved_geo['longitude']}")

        if st.session_state.resolved_geo.get("fallback_used"):
            st.warning("Exact street address could not be resolved. Defaulting to general City/State/ZIP coordinates. Please confirm this is acceptable.")

        today_dt = date.today()
        is_forecast = target_date > today_dt
        button_text = "Confirm Location & Fetch Forecasted Weather" if is_forecast else "Confirm Location & Fetch Historical Weather"
        
        if st.button(button_text, type="primary"):
            st.session_state.is_forecast = is_forecast
            st.session_state.worker_weight = float(worker_weight)
            st.session_state.location_fallback = st.session_state.resolved_geo.get("fallback_used", False)
            
            st.session_state.use_caf = caf_enabled
            if caf_enabled:
                st.session_state.caf_label = caf_selection.split(" (+")[0]
                st.session_state.caf_value = float(caf_selection.split("(+")[1].replace(")", ""))
            else:
                st.session_state.caf_label = "Standard Work Clothes (0.0 °F)"
                st.session_state.caf_value = 0.0

            geo_lat = st.session_state.resolved_geo["latitude"]
            geo_lon = st.session_state.resolved_geo["longitude"]
            matched_address = st.session_state.resolved_geo["matched_address"]
            raw_entered_address = st.session_state.resolved_geo["raw_entered"]

            st.session_state.location_meta = {
                "geo_lat": geo_lat,
                "geo_lon": geo_lon,
                "matched_address": matched_address,
                "raw_entered_address": raw_entered_address
            }

            with st.spinner("Pulling meteorological timeline from Open-Meteo Open Source Grid..."):
                date_str = target_date.strftime("%Y-%m-%d")
                weather_res = fetch_weather_native(geo_lat, geo_lon, date_str, is_forecast)
                
                if "error" in weather_res:
                    st.error(weather_res["error"])
                else:
                    h_data = weather_res["hourly"]
                    w_lat = weather_res["grid_latitude"]
                    w_lon = weather_res["grid_longitude"]
                    grid_dist = haversine_distance(geo_lat, geo_lon, w_lat, w_lon)
                    
                    if "time" not in h_data:
                        st.error("Meteorological data missing for requested date. It may be too far in the past or future.")
                    else:
                        hourly_rows = []
                        tz_val = get_osha_tz_value(geo_lon)
                        
                        try:
                            for idx, t_iso in enumerate(h_data["time"]):
                                dt_obj = datetime.fromisoformat(t_iso)
                                hr = dt_obj.hour
                                if start_hour <= hr <= end_hour:
                                    temp_f = h_data["temperature_2m"][idx]
                                    rh_val = h_data["relative_humidity_2m"][idx]
                                    wind_val = h_data["wind_speed_10m"][idx]
                                    pressure = h_data.get("surface_pressure", [29.92]*len(h_data["time"]))[idx]
                                    
                                    pressure_inhg = float(pressure) * 0.02953
                                    
                                    hourly_rows.append({
                                        "date_string_final": date_str,
                                        "time_display": dt_obj.strftime("%I:00 %p"),
                                        "hour_24h": hr,
                                        "temperature_f": temp_f,
                                        "relative_humidity_percent": rh_val,
                                        "wind_speed_mph": wind_val,
                                        "barometric_pressure_inhg": pressure_inhg,
                                        "latitude": geo_lat,
                                        "longitude": geo_lon,
                                        "longitude_absolute": abs(geo_lon),
                                        "grid_latitude": w_lat,
                                        "grid_longitude": w_lon,
                                        "grid_distance_miles": grid_dist,
                                        "tz_value": tz_val,
                                        "user_entered_address": raw_entered_address,
                                        "validated_address": matched_address
                                    })
                            
                            st.session_state.final_hourly_rows = hourly_rows
                            st.session_state.step = 2
                            st.rerun()
                        except Exception as parse_error:
                            st.error(f"Failed to process meteorological timeline: {parse_error}")

# =====================================================================
# STEP 2: METABOLIC WORKLOAD CONFIGURATION
# =====================================================================
elif st.session_state.step == 2:
    st.subheader("Step 2: Calibrate Metabolic Workloads per Shift Hour")
    st.info("Set the primary workload profile for each timeframe. (Mass corrections are applied algorithmically before evaluation).")
    
    rows = st.session_state.final_hourly_rows
    worker_mass = st.session_state.worker_weight
    
    workload_mapping = {
        "Light (Sitting/Standing, Light Arms)": 180,
        "Moderate (Walking, Moderate Lifting)": 300,
        "Heavy (Heavy Lifting, Fast Walking)": 415,
        "Very Heavy (Intense Shoveling/Running)": 520
    }
    
    with st.form("workload_matrix"):
        for i, row in enumerate(rows):
            st.markdown(f"**Hour Slot:** {row['time_display']} (Base Temp: {row['temperature_f']}°F, RH: {row['relative_humidity_percent']}%, Wind: {row['wind_speed_mph']} mph)")
            selected_load = st.selectbox(f"Workload Profile for {row['time_display']}:", options=list(workload_mapping.keys()), key=f"wl_{i}")
            rows[i]["workload_label"] = selected_load
            
            base_watts = workload_mapping[selected_load]
            mass_correction_factor = worker_mass / 154.0
            rows[i]["final_watts"] = round(base_watts * mass_correction_factor, 1)
            st.divider()
            
        if st.form_submit_button("Initiate Regulatory Analysis & Scrape Engine", type="primary"):
            st.session_state.final_hourly_rows = rows
            st.session_state.step = 3
            st.rerun()
            
    if st.button("Cancel & Return to Setup"):
        st.session_state.step = 1
        st.rerun()

# =====================================================================
# STEP 3: EXPOSURE RESULTS & GRAPHICS
# =====================================================================
elif st.session_state.step == 3:
    st.subheader("Step 3: Exposure Threshold Findings")
    
    source_label = "Open-Meteo Grid (Forecasted)" if st.session_state.is_forecast else "Open-Meteo Grid (Historical)"
    
    st.markdown("#### Execution Log")
    computed_data = run_browser_automation(st.session_state.final_hourly_rows, source_label, st.session_state.standard_choice)
    
    st.markdown("---")
    st.markdown("#### Formalized Assessment Graph")
    plot_buffer = generate_compliance_plot(computed_data, st.session_state.worker_weight, st.session_state.is_forecast, st.session_state.use_caf, st.session_state.caf_label, st.session_state.standard_choice)
    st.image(plot_buffer, use_column_width=True)
    
    if st.session_state.fallback_active:
        st.warning("**Compliance Note:** Primary OSHA scraping connectivity was disrupted during execution. WBGT thresholds were automatically mapped using an offline stull approximation algorithm.")

    st.markdown("#### Tabular Shift Breakdown")
    for rec in computed_data:
        st.write(f"**{rec['Time']}** | Load: {rec['Adjusted_Watts']} W | Temp: {rec['Air_Temp_F']} °F | RH: {rec['Humidity_Pct']} % | **Sun WBGT: {rec['Sun_WBGT_F']} °F** | **Shade WBGT: {rec['Shade_WBGT_F']} °F** | State: `{rec['Safety_Status']}`")

    st.markdown("---")
    csv_buffer = io.StringIO()
    if computed_data:
        writer = csv.DictWriter(csv_buffer, fieldnames=computed_data[0].keys())
        writer.writeheader()
        for c in computed_data: writer.writerow(c)
        
        st.download_button(
            label="Download Diagnostic CSV Log",
            data=csv_buffer.getvalue(),
            file_name=f"osha_wbgt_diagnostic_{computed_data[0]['Date']}.csv",
            mime="text/csv",
            type="primary"
        )
        
    if st.button("Execute Fresh Inspection Run", type="secondary"):
        st.session_state.step = 1
        st.session_state.resolved_geo = None
        st.rerun()
