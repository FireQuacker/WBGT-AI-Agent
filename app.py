import os
import subprocess
import streamlit as st

# =====================================================================
# ONE-TIME PLAYWRIGHT INSTALLER (PREVENTS RE-RUN LAG)
# =====================================================================
@st.cache_resource
def install_browser_engine():
    try:
        # Runs quietly in the background exactly once at initial launch
        subprocess.run(["playwright", "install", "chromium"], check=True)
    except Exception as e:
        st.error(f"Background browser engine initialization warning: {e}")

# Trigger the one-time installation check
install_browser_engine()

# =====================================================================
# APPLICATION IMPORTS
# =====================================================================
import time
import math
import csv
import io
import requests
from datetime import datetime
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from playwright.sync_api import sync_playwright
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np

# =====================================================================
# STREAMLIT CONFIGURATION & PERSISTENCE STATE
# =====================================================================
st.set_page_config(page_title="OSHA Heat Stress Dashboard", layout="wide")

if "step" not in st.session_state:
    st.session_state.step = 1
if "final_hourly_rows" not in st.session_state:
    st.session_state.final_hourly_rows = None
if "worker_weight" not in st.session_state:
    st.session_state.worker_weight = 154.0
if "fallback_active" not in st.session_state:
    st.session_state.fallback_active = False

# =====================================================================
# INTENT EXTRACTION SCHEMA & UTILITIES
# =====================================================================
class UserIntent(BaseModel):
    address: str = Field(description="The physical address, city, or location requested by the user.")
    date: str = Field(description="The requested date converted into strict YYYY-MM-DD format.")
    start_hour_24h: int = Field(description="The starting hour extracted and converted to 24-hour integer format (0-23).")
    end_hour_24h: int = Field(description="The ending hour extracted and converted to 24-hour integer format (0-23).")

def get_osha_tz_value(lon: float) -> str:
    if lon >= -85.5: return "-5"
    elif lon >= -103.5: return "-6"
    elif lon >= -115.5: return "-7"
    elif lon >= -130.0: return "-8"
    elif lon >= -150.0: return "-9"
    else: return "-10"

def geocode_address_native(address: str) -> dict:
    url = "https://nominatim.openstreetmap.org/search"
    query_params = {"q": address, "format": "json", "limit": 1}
    headers = {"User-Agent": "OSHA-WBGT-Web-Dashboard/2.0"}
    try:
        response = requests.get(url, params=query_params, headers=headers)
        data = response.json()
        if data: return {"latitude": float(data[0]["lat"]), "longitude": float(data[0]["lon"])}
        return {"error": "Location coordinates could not be resolved."}
    except Exception as e:
        return {"error": str(e)}

def fetch_weather_native(lat: float, lon: float, date_str: str) -> dict:
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon, "start_date": date_str, "end_date": date_str,
        "hourly": ["temperature_2m", "relative_humidity_2m", "surface_pressure", "wind_speed_10m"],
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto"
    }
    try:
        response = requests.get(url, params=params)
        return response.json()
    except Exception as e:
        return {"error": str(e)}

def calculate_wbgt_meteorological_fallback(temp_f, rh_pct, wind_mph, is_sun=True):
    """
    Calculates an offline approximation of WBGT using recognized empirical equations.
    Triggers automatically if the OSHA automated scraper is blocked or offline.
    """
    tc = (temp_f - 32) * 5.0 / 9.0
    rh = rh_pct
    tw = (tc * math.atan(0.151977 * (rh + 8.313766)**0.5) 
          + math.atan(tc + rh) 
          - math.atan(rh - 1.676331) 
          + 0.00391838 * (rh)**1.5 * math.atan(0.023101 * rh) 
          - 4.686035)
    
    if is_sun:
        wind_ms = max(wind_mph * 0.44704, 0.1)
        solar_rad = 800.0  
        tg_c = tc + 0.015 * solar_rad - 0.12 * wind_ms
        if tg_c < tc:
            tg_c = tc + 2.0
    else:
        tg_c = tc + 1.0
        
    if is_sun:
        wbgt_c = 0.7 * tw + 0.2 * tg_c + 0.1 * tc
    else:
        wbgt_c = 0.7 * tw + 0.3 * tg_c
        
    wbgt_f = (wbgt_c * 1.8) + 32
    return round(wbgt_f, 1)

# =====================================================================
# WEB AUTOMATION BACKEND ENGINE (STABILIZED & SPOOFED)
# =====================================================================
def run_browser_automation(hourly_data, weight, use_headed=False):
    tz_labels = {"-5": "Eastern Time", "-6": "Central Time", "-7": "Mountain Time", "-8": "Pacific Time", "-9": "Alaska", "-10": "Hawaii"}
    computed_results = []
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    st.session_state.fallback_active = False
    
    actual_headless = not use_headed
    if use_headed and not os.environ.get("DISPLAY"):
        st.warning("⚠️ **Headed Mode Display Warning**: No desktop display environment ($DISPLAY) was detected on this server. Running headlessly to protect application stability.")
        actual_headless = True
        
    try:
        with sync_playwright() as p:
            status_text.text("Launching browser context...")
            browser = p.chromium.launch(
                headless=actual_headless, 
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
            )
            
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="en-US"
            )
            
            page = context.new_page()
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
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
                    except:
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
                
                # --- DATA CLAMPING & NOTES GENERATION ---
                orig_temp = float(hour['temperature_f'])
                orig_rh   = int(hour['relative_humidity_percent'])
                orig_ws   = float(hour['wind_speed_mph'])
                orig_pres = float(hour['barometric_pressure_inhg'])

                safe_temp = max(min(orig_temp, 120.0), 32.0)
                safe_rh   = max(min(orig_rh, 100), 1)
                safe_ws   = max(min(orig_ws, 50.0), 0.0)
                safe_pres = max(min(orig_pres, 32.0), 25.0)

                notes_list = []
                if orig_temp < 32.0: notes_list.append(f"Air Temp rounded up to 32.0 °F")
                elif orig_temp > 120.0: notes_list.append(f"Air Temp rounded down to 120.0 °F")
                
                if orig_rh < 1: notes_list.append(f"RH rounded up to 1%")
                elif orig_rh > 100: notes_list.append(f"RH rounded down to 100%")

                if orig_ws < 0.0: notes_list.append(f"Wind Speed rounded up to 0.0 mph")
                elif orig_ws > 50.0: notes_list.append(f"Wind Speed rounded down to 50.0 mph")

                if orig_pres < 25.0: notes_list.append(f"Pressure rounded up to 25.0 inHg")
                elif orig_pres > 32.0: notes_list.append(f"Pressure rounded down to 32.0 inHg")

                notes_str = " | ".join(notes_list) if notes_list else "None"
                # ----------------------------------------
                
                try:
                    formatted_time = f"{hour['hour_24h']:02d}:00"
                    target_label = tz_labels.get(hour["tz_value"], "Eastern Time")
                    
                    target_frame.locator('input[name="dd"]').fill(str(hour["date_string_final"]))
                    target_frame.locator('input[name="tm"]').fill(formatted_time)
                    target_frame.locator('input[name="lat"]').fill(str(hour["latitude"]))
                    target_frame.locator('input[name="lon"]').fill(str(hour["longitude_absolute"]))
                    
                    # Pass the SAFE values into the browser automation
                    target_frame.locator('input[name="temp"]').fill(str(safe_temp))
                    target_frame.locator('input[name="rh"]').fill(str(safe_rh))
                    target_frame.locator('input[name="ws"]').fill(str(safe_ws))
                    target_frame.locator('input[name="pres"]').fill(str(safe_pres))
                    
                    try: 
                        target_frame.locator('select[name="tz"]').select_option(value=hour["tz_value"], timeout=100)
                    except: 
                        pass
                    try: 
                        target_frame.locator('select[name="tz"]').select_option(label=target_label, timeout=100)
                    except: 
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
                    sun_f = calculate_wbgt_meteorological_fallback(
                        hour['temperature_f'], hour['relative_humidity_percent'], hour['wind_speed_mph'], is_sun=True
                    )
                    shade_f = calculate_wbgt_meteorological_fallback(
                        hour['temperature_f'], hour['relative_humidity_percent'], hour['wind_speed_mph'], is_sun=False
                    )
                    if notes_str == "None":
                        notes_str = "Offline Stull Fallback Used"
                    else:
                        notes_str = notes_str + " | Offline Stull Fallback Used"
                    
                adjusted_watts = round((hour["base_watts"] * weight) / 154.0, 1)
                tlv_c = 56.7 - (11.5 * math.log10(adjusted_watts))
                al_c = 59.9 - (14.1 * math.log10(adjusted_watts))
                
                tlv_f = round((tlv_c * 1.8) + 32, 1)
                al_f = round((al_c * 1.8) + 32, 1)
                
                status = "Normal"
                if sun_f > tlv_f or shade_f > tlv_f: 
                    status = "BREACH: TLV"
                elif sun_f > al_f or shade_f > al_f: 
                    status = "WARNING: AL"
                
                computed_results.append({
                    "Date": hour["date_string_final"],
                    "Time": hour["time_display"], 
                    "Latitude": hour["latitude"],
                    "Longitude": hour["longitude"],
                    "Air_Temp_F": orig_temp, 
                    "Humidity_Pct": orig_rh, 
                    "Wind_Speed_Mph": orig_ws,
                    "Pressure_inHg": orig_pres,
                    "Sun_WBGT_F": sun_f, 
                    "Shade_WBGT_F": shade_f, 
                    "Workload": hour["workload_label"], 
                    "Adjusted_Watts": adjusted_watts, 
                    "ACGIH_TLV_F": tlv_f, 
                    "ACGIH_AL_F": al_f, 
                    "Safety_Status": status,
                    "Weather_Source": "Open-Meteo (Copernicus ERA5 Reanalysis)",
                    "Notes": notes_str
                })
                
            browser.close()
            
    except Exception as sys_err:
        st.session_state.fallback_active = True
        computed_results = []
        for index, hour in enumerate(hourly_data):
            # Evaluate clamping notes even on complete failure for export consistency
            orig_temp = float(hour['temperature_f'])
            orig_rh   = int(hour['relative_humidity_percent'])
            orig_ws   = float(hour['wind_speed_mph'])
            orig_pres = float(hour['barometric_pressure_inhg'])

            notes_list = []
            if orig_temp < 32.0: notes_list.append(f"Air Temp rounded up to 32.0 °F")
            elif orig_temp > 120.0: notes_list.append(f"Air Temp rounded down to 120.0 °F")
            
            if orig_rh < 1: notes_list.append(f"RH rounded up to 1%")
            elif orig_rh > 100: notes_list.append(f"RH rounded down to 100%")

            if orig_ws < 0.0: notes_list.append(f"Wind Speed rounded up to 0.0 mph")
            elif orig_ws > 50.0: notes_list.append(f"Wind Speed rounded down to 50.0 mph")

            if orig_pres < 25.0: notes_list.append(f"Pressure rounded up to 25.0 inHg")
            elif orig_pres > 32.0: notes_list.append(f"Pressure rounded down to 32.0 inHg")

            notes_str = " | ".join(notes_list) if notes_list else "None"
            
            if notes_str == "None":
                notes_str = "Offline Stull Fallback Used"
            else:
                notes_str = notes_str + " | Offline Stull Fallback Used"

            sun_f = calculate_wbgt_meteorological_fallback(
                orig_temp, orig_rh, orig_ws, is_sun=True
            )
            shade_f = calculate_wbgt_meteorological_fallback(
                orig_temp, orig_rh, orig_ws, is_sun=False
            )
            
            adjusted_watts = round((hour["base_watts"] * weight) / 154.0, 1)
            tlv_c = 56.7 - (11.5 * math.log10(adjusted_watts))
            al_c = 59.9 - (14.1 * math.log10(adjusted_watts))
            
            tlv_f = round((tlv_c * 1.8) + 32, 1)
            al_f = round((al_c * 1.8) + 32, 1)
            
            status = "Normal"
            if sun_f > tlv_f or shade_f > tlv_f: 
                status = "BREACH: TLV"
            elif sun_f > al_f or shade_f > al_f: 
                status = "WARNING: AL"
            
            computed_results.append({
                "Date": hour["date_string_final"],
                "Time": hour["time_display"], 
                "Latitude": hour["latitude"],
                "Longitude": hour["longitude"],
                "Air_Temp_F": orig_temp, 
                "Humidity_Pct": orig_rh, 
                "Wind_Speed_Mph": orig_ws,
                "Pressure_inHg": orig_pres,
                "Sun_WBGT_F": sun_f, 
                "Shade_WBGT_F": shade_f, 
                "Workload": hour["workload_label"], 
                "Adjusted_Watts": adjusted_watts, 
                "ACGIH_TLV_F": tlv_f, 
                "ACGIH_AL_F": al_f, 
                "Safety_Status": status,
                "Weather_Source": "Open-Meteo (Copernicus ERA5 Reanalysis)",
                "Notes": notes_str
            })

    progress_bar.progress(1.0)
    status_text.text("Processing operation completed successfully.")
    return computed_results

# =====================================================================
# MATPLOTLIB GRAPHICS COMPLIANCE GENERATOR
# =====================================================================
def generate_compliance_plot(results, weight):
    watts_range = np.linspace(100, 600, 500)
    tlv_curve_f = [(56.7 - (11.5 * math.log10(w))) * 1.8 + 32 for w in watts_range]
    al_curve_f = [(59.9 - (14.1 * math.log10(w))) * 1.8 + 32 for w in watts_range]
    
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.plot(watts_range, tlv_curve_f, color='crimson', linestyle='-', linewidth=2.5, label='ACGIH TLV (Acclimatized Limit)')
    ax.plot(watts_range, al_curve_f, color='darkorange', linestyle='--', linewidth=2.5, label='ACGIH Action Limit (Unacclimatized)')
    
    x_watts = [r["Adjusted_Watts"] for r in results]
    y_sun = [r["Sun_WBGT_F"] for r in results]
    y_shade = [r["Shade_WBGT_F"] for r in results]
    
    ax.scatter(x_watts, y_sun, color='red', marker='o', s=120, zorder=5, label='Hourly Exposure (Sun WBGT)')
    ax.scatter(x_watts, y_shade, color='blue', marker='s', s=100, zorder=5, label='Hourly Exposure (Shade WBGT)')
    
    for i, r in enumerate(results):
        ax.annotate(r["Time"], (x_watts[i], y_sun[i]), textcoords="offset points", xytext=(6, 5), fontsize=8, color='darkred', fontweight='bold')
        ax.annotate(r["Time"], (x_watts[i], y_shade[i]), textcoords="offset points", xytext=(6, -12), fontsize=8, color='darkblue')

    all_x, all_y = x_watts + x_watts, y_sun + y_shade
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)
    pad_x, pad_y = 15, 1.0
    if min_x == max_x: min_x -= 20; max_x += 20
        
    rect = patches.Rectangle((min_x - pad_x, min_y - pad_y), (max_x + pad_x) - (min_x - pad_x), (max_y + pad_y) - (min_y - pad_y),
                             linewidth=1.5, edgecolor='purple', linestyle=':', facecolor='purple', alpha=0.07, label='Shift Exposure Envelope Box')
    ax.add_patch(rect)
    
    for w, lbl in [(180, ' Light (180W)'), (300, ' Moderate (300W)'), (415, ' Heavy (415W)'), (520, ' Very Heavy (520W)')]:
        ax.axvline(x=w, color='gray', linestyle=':', alpha=0.4)
        ax.text(w, min(al_curve_f) - 3, lbl, fontsize=8, color='gray', alpha=0.7, rotation=90)

    ax.set_title(f"ACGIH Heat Stress Analytical Assessment Plot\nWorker Structural Weight: {weight} lbs", fontsize=12, fontweight='bold')
    ax.set_xlabel("Adjusted Metabolic Rate (Watts)", fontsize=11)
    ax.set_ylabel("Wet Bulb Globe Temperature Index (WBGT in °F)", fontsize=11)
    ax.set_xlim(90, 610)
    ax.set_ylim(min(al_curve_f) - 5, max(tlv_curve_f) + 5)
    ax.grid(True, linestyle=':', alpha=0.5)
    ax.legend(loc='upper right', framealpha=0.9)
    return fig

# =====================================================================
# UI / STREAMLIT
# =====================================================================
st.title("☀️ OSHA-WBGT & ACGIH Heat Stress Compliance Engine")
st.markdown("AI Agent: Automated localized WBGT reconstruction based on historic weather data from Open-Meteo. Designed by Andre Taylor to be used for regulatory threshold screening.")
st.divider()

# Configure Engine Sidebar Options
st.sidebar.subheader("Engine Configurations")
use_headed = st.sidebar.checkbox(
    "Open Visible Browser Mode (Headed)", 
    value=False, 
    help="Turn this on when running LOCALLY to see the browser window perform live data-entry automation. Keep unchecked on cloud environments."
)

api_key_env = os.environ.get("GEMINI_API_KEY", "")
if not api_key_env:
    api_key_input = st.sidebar.text_input("Enter Gemini API Key", type="password")
    if api_key_input:
        os.environ["GEMINI_API_KEY"] = api_key_input

# --- WIZARD STEP 1: PARSE USER INTENT & HISTORICAL WEATHER MATRIX ---
if st.session_state.step == 1:
    st.subheader("Step 1: Set Target Parameters & Profile Matrix")
    
    col1, col2 = st.columns([3, 1])
    with col1:
        user_prompt = st.text_area(
            "What location and date timeline do you need evaluated?",
            placeholder="e.g., Check weather parameters for Dallas, Texas on August 12th, 2025 from 8 AM to 4 PM.",
            help="Specify a clear location, a fixed date, and a start/end operational time window."
        )
    with col2:
        worker_weight = st.number_input("Employee Weight (lbs)", min_value=50.0, max_value=400.0, value=154.0, step=1.0)
    
    if st.button("Analyze Shift Timeline", type="primary"):
        if not os.environ.get("GEMINI_API_KEY"):
            st.error("Please supply a valid Gemini API Token to authorize query synthesis.")
        elif not user_prompt.strip():
            st.warning("Please type an engineering assessment request string.")
        else:
            with st.spinner("Synthesizing context parameters via Gemini Core Engine..."):
                try:
                    client = genai.Client()
                    response = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=user_prompt.strip(),
                        config=types.GenerateContentConfig(
                            system_instruction="Extract location, date (YYYY-MM-DD), and 24h clock constraints.",
                            response_mime_type="application/json",
                            response_schema=UserIntent
                        )
                    )
                    
                    clean_response_text = response.text.replace("```json", "").replace("```", "").strip()
                    intent = UserIntent.model_validate_json(clean_response_text)
                    
                    geo = geocode_address_native(intent.address)
                    if "error" in geo:
                        st.error(geo["error"])
                    else:
                        weather = fetch_weather_native(geo["latitude"], geo["longitude"], intent.date)
                        if "error" in weather or "hourly" not in weather:
                            st.error("Could not pull valid weather timeline matrices.")
                        else:
                            hourly = weather["hourly"]
                            times, temps, hums, winds, press = hourly["time"], hourly["temperature_2m"], hourly["relative_humidity_2m"], hourly["wind_speed_10m"], hourly["surface_pressure"]
                            
                            try: 
                                final_date_str = datetime.strptime(intent.date, "%Y-%m-%d").strftime("%m/%d/%Y")
                            except: 
                                final_date_str = intent.date
                            
                            tz_val = get_osha_tz_value(geo["longitude"])
                            active_rows = []
                            for i in range(len(times)):
                                hr_int = int(times[i].split("T")[1].split(":")[0])
                                if intent.start_hour_24h <= hr_int <= intent.end_hour_24h:
                                    ampm = "12:00 AM" if hr_int==0 else ("12:00 PM" if hr_int==12 else (f"{hr_int-12}:00 PM" if hr_int>12 else f"{hr_int}:00 AM"))
                                    active_rows.append({
                                        "date_string_final": final_date_str, "time_display": ampm, "hour_24h": hr_int,
                                        "latitude": geo["latitude"], 
                                        "longitude": geo["longitude"],
                                        "longitude_absolute": abs(geo["longitude"]), "tz_value": tz_val,
                                        "temperature_f": temps[i], "relative_humidity_percent": int(hums[i]), "wind_speed_mph": winds[i],
                                        "barometric_pressure_inhg": round(press[i] * 0.02953, 2)
                                    })
                            
                            if not active_rows:
                                st.error("No hours matched your operational shift boundaries.")
                            else:
                                st.session_state.final_hourly_rows = active_rows
                                st.session_state.worker_weight = worker_weight
                                st.session_state.step = 2
                                st.rerun()
                except Exception as ex:
                    st.error(f"Pipeline extraction system fault: {ex}")

# --- WIZARD STEP 2: DYNAMIC HOURLY WORKLOAD DESIGNER ---
elif st.session_state.step == 2:
    st.subheader("Step 2: Assign Hourly Worker Metabolism / Workloads")
    st.markdown("Select structural task categories matching specific operational shift hours below:")
    
    workload_options = {
        "Light (180W)": {"w": 180, "lbl": "Light"},
        "Moderate (300W)": {"w": 300, "lbl": "Moderate"},
        "Heavy (415W)": {"w": 415, "lbl": "Heavy"},
        "Very Heavy (520W)": {"w": 520, "lbl": "Very Heavy"}
    }
    
    selections = {}
    cols = st.columns(min(len(st.session_state.final_hourly_rows), 4))
    for idx, row in enumerate(st.session_state.final_hourly_rows):
        col_target = cols[idx % len(cols)]
        with col_target:
            selections[row["hour_24h"]] = st.selectbox(
                f"Hour: {row['time_display']}", 
                options=list(workload_options.keys()), 
                index=1, 
                key=f"sel_{row['hour_24h']}"
            )
            
    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("← Modify Location or Prompt Parameters"):
            st.session_state.step = 1
            st.rerun()
    with c2:
        if st.button("Run Scraper & Generate Analysis →", type="primary"):
            for row in st.session_state.final_hourly_rows:
                chosen = workload_options[selections[row["hour_24h"]]]
                row["workload_label"] = chosen["lbl"]
                row["base_watts"] = chosen["w"]
                
            with st.spinner("Executing calculations..."):
                results = run_browser_automation(
                    st.session_state.final_hourly_rows, 
                    st.session_state.worker_weight, 
                    use_headed=use_headed
                )
                
            if results:
                st.session_state.results = results
                st.session_state.step = 3
                st.rerun()
            else:
                st.error("No calculation arrays compiled successfully.")

# --- WIZARD STEP 3: INTERACTIVE REPORT VIEWER & EXPORT ---
elif st.session_state.step == 3:
    st.subheader("Step 3: Compliance Engineering Summary Analysis Output")
    
    if st.session_state.fallback_active:
        st.warning(
            "⚠️ **OSHA Website Protection / Playwright Fallback Active**: The system was unable to scrape the online OSHA calculator "
            "(due to cloud display restrictions, server blockades, or service downtime). To protect your analysis, the platform "
            "successfully estimated Wet Bulb Globe Temperatures (WBGT) offline utilizing standard meteorological formulas (Stull's Wet-Bulb equation)."
        )
    else:
        st.success("✅ Wet Bulb Globe Temperature (WBGT) data compiled successfully directly from the live OSHA host calculator.")
        
    fig = generate_compliance_plot(st.session_state.results, st.session_state.worker_weight)
    st.pyplot(fig)
    
    st.subheader("Raw Exposure Tracking Metrics Matrix")
    st.dataframe(st.session_state.results, use_container_width=True)
    
    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=list(st.session_state.results[0].keys()))
    writer.writeheader()
    writer.writerows(st.session_state.results)
    
    st.download_button(
        label="Download Compliance Report Spreadsheet (.CSV)",
        data=csv_buffer.getvalue(),
        file_name=f"Heat_Stress_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv"
    )
    
    st.divider()
    if st.button("🔄 Execute Fresh Inspection Run"):
        st.session_state.step = 1
        st.session_state.final_hourly_rows = None
        st.session_state.fallback_active = False
        st.rerun()
