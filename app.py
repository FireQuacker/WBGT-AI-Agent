import os
import subprocess
import streamlit as st

# =====================================================================
# ONE-TIME PLAYWRIGHT INSTALLER
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
import json
import requests
from datetime import datetime
from google import genai
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
    query_params = {"q": address.strip(), "format": "json", "limit": 1}
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

# =====================================================================
# WEB AUTOMATION BACKEND ENGINE (FIXED IFRAME HANDLING)
# =====================================================================
def run_browser_automation(hourly_data, weight):
    tz_labels = {"-5": "Eastern Time", "-6": "Central Time", "-7": "Mountain Time", "-8": "Pacific Time", "-9": "Alaska", "-10": "Hawaii"}
    computed_results = []
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        
        page = context.new_page()
        target_url = "https://www.osha.gov/heat-exposure/wbgt-calculator"
        status_text.text("Connecting to OSHA computation host...")
        
        try:
            page.goto(target_url, wait_until="networkidle", timeout=30000)
            
            # Locate the frame containing the actual calculator form
            target_frame = None
            for frame in page.frames:
                if "wbgt-calculator" in frame.url or frame.name == "calculator-frame":
                    target_frame = frame
                    break
            
            # Fallback if no specific subframe matches
            if not target_frame:
                target_frame = page
                
        except Exception as e:
            st.error(f"🚨 Page load exception: {e}")
            browser.close()
            return []
                
        total_rows = len(hourly_data)
        for index, hour in enumerate(hourly_data):
            status_text.text(f"Processing hour: {hour['time_display']} ({index+1}/{total_rows})...")
            progress_bar.progress(index / total_rows)
            
            try:
                formatted_time = f"{hour['hour_24h']:02d}:00"
                target_label = tz_labels.get(hour["tz_value"], "Eastern Time")
                
                # Fill the calculator fields inside the target frame context
                target_frame.locator('#dd').fill(str(hour["date_string_final"]))
                target_frame.locator('#tm').fill(formatted_time)
                target_frame.locator('#lat').fill(str(hour["latitude"]))
                target_frame.locator('#lon').fill(str(hour["longitude_absolute"]))
                target_frame.locator('#temp').fill(str(hour['temperature_f']))
                target_frame.locator('#rh').fill(str(hour['relative_humidity_percent']))
                target_frame.locator('#ws').fill(str(hour['wind_speed_mph']))
                target_frame.locator('#pres').fill(str(hour['barometric_pressure_inhg']))
                
                try: target_frame.locator('#tz').select_option(value=hour["tz_value"], timeout=500)
                except Exception: pass
                
                time.sleep(0.1)
                target_frame.locator('#sub').click()
                
                # Wait for values to compute
                sun_wbgt, shade_wbgt = "---", "---"
                for _ in range(40):  
                    time.sleep(0.1)
                    live_sun_val = target_frame.locator('#wbgt_sun').input_value()
                    if live_sun_val and live_sun_val != "---" and live_sun_val.strip() != "":
                        sun_wbgt = live_sun_val.strip()
                        shade_wbgt = target_frame.locator('#wbgt_shade').input_value().strip()
                        break
                
                sun_f = float(sun_wbgt.split("/")[1].replace("F","").strip()) if "/" in sun_wbgt else 0.0
                shade_f = float(shade_wbgt.split("/")[1].replace("F","").strip()) if "/" in shade_wbgt else 0.0
                
                adjusted_watts = round((hour["base_watts"] * weight) / 154.0, 1)
                tlv_c = 56.7 - (11.5 * math.log10(adjusted_watts))
                al_c = 59.9 - (14.1 * math.log10(adjusted_watts))
                
                tlv_f = round((tlv_c * 1.8) + 32, 1)
                al_f = round((al_c * 1.8) + 32, 1)
                
                status = "Normal"
                if sun_f > tlv_f or shade_f > tlv_f: status = "BREACH: TLV"
                elif sun_f > al_f or shade_f > al_f: status = "WARNING: AL"
                
                computed_results.append({
                    "Time": hour["time_display"], "Air_Temp": f"{hour['temperature_f']}°F", 
                    "Humidity": f"{hour['relative_humidity_percent']}%", "Sun_WBGT_F": sun_f, 
                    "Shade_WBGT_F": shade_f, "Workload": hour["workload_label"], 
                    "Adjusted_Watts": adjusted_watts, "ACGIH_TLV_F": tlv_f, 
                    "ACGIH_AL_F": al_f, "Safety_Status": status
                })
            except Exception as e:
                st.error(f"Error on row {hour['time_display']}: {e}")
                
        browser.close()
        progress_bar.progress(1.0)
        
    return computed_results

# =====================================================================
# MATPLOTLIB GRAPHICS COMPLIANCE GENERATOR
# =====================================================================
def generate_compliance_plot(results, weight):
    watts_range = np.linspace(100, 600, 500)
    tlv_curve_f = [(56.7 - (11.5 * math.log10(w))) * 1.8 + 32 for w in watts_range]
    al_curve_f = [(59.9 - (14.1 * math.log10(w))) * 1.8 + 32 for w in watts_range]
    
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.plot(watts_range, tlv_curve_f, color='crimson', label='ACGIH TLV')
    ax.plot(watts_range, al_curve_f, color='darkorange', linestyle='--', label='ACGIH Action Limit')
    
    x_watts = [r["Adjusted_Watts"] for r in results]
    y_sun = [r["Sun_WBGT_F"] for r in results]
    y_shade = [r["Shade_WBGT_F"] for r in results]
    
    ax.scatter(x_watts, y_sun, color='red', marker='o', s=120, label='Sun WBGT')
    ax.scatter(x_watts, y_shade, color='blue', marker='s', s=100, label='Shade WBGT')
    
    for i, r in enumerate(results):
        ax.annotate(r["Time"], (x_watts[i], y_sun[i]), textcoords="offset points", xytext=(5,5), fontsize=8)

    ax.set_title(f"ACGIH Heat Stress Plot (Worker Weight: {weight} lbs)")
    ax.set_xlabel("Adjusted Metabolic Rate (Watts)")
    ax.set_ylabel("WBGT (°F)")
    ax.grid(True, linestyle=':', alpha=0.5)
    ax.legend()
    return fig

# =====================================================================
# STREAMLIT USER INTERFACE VIEW
# =====================================================================
st.title("☀️ OSHA-WBGT & ACGIH Heat Stress Compliance Dashboard")
st.divider()

# API Key Sidebar setup from your working design
api_key = os.environ.get("GEMINI_API_KEY", "")
if not api_key:
    api_key = st.sidebar.text_input("Enter Gemini API Key", type="password")

# --- STEP 1 ---
if st.session_state.step == 1:
    st.subheader("Step 1: Set Target Parameters")
    
    col1, col2 = st.columns([3, 1])
    with col1:
        user_prompt = st.text_area(
            "Enter location and shift window details:",
            value="Check weather parameters for Austin, Texas on July 10th, 2025 from 9 AM to 3 PM."
        )
    with col2:
        worker_weight = st.number_input("Employee Weight (lbs)", value=154.0)
    
    if st.button("Analyze Shift Timeline", type="primary"):
        if not api_key:
            st.error("Please provide a Gemini API Key.")
        else:
            with st.spinner("Parsing input instructions..."):
                try:
                    # Pure, standard text generation text string approach
                    client = genai.Client(api_key=api_key)
                    prompt_instructions = (
                        f"Extract details from this request: '{user_prompt}'.\n"
                        f"Return ONLY a valid raw JSON object matching this structure:\n"
                        f'{{"address": "string", "date": "YYYY-MM-DD", "start_hour_24h": int, "end_hour_24h": int}}'
                    )
                    
                    response = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=prompt_instructions
                    )
                    
                    # Clean the raw text manually to prevent JSON parsing faults
                    clean_text = response.text.replace("```json", "").replace("```", "").strip()
                    data = json.loads(clean_text)
                    
                    geo = geocode_address_native(data["address"])
                    if "error" in geo:
                        st.error(geo["error"])
                    else:
                        weather = fetch_weather_native(geo["latitude"], geo["longitude"], data["date"])
                        if "hourly" in weather:
                            hourly = weather["hourly"]
                            times = hourly["time"]
                            
                            tz_val = get_osha_tz_value(geo["longitude"])
                            active_rows = []
                            
                            for i in range(len(times)):
                                hr_int = int(times[i].split("T")[1].split(":")[0])
                                if data["start_hour_24h"] <= hr_int <= data["end_hour_24h"]:
                                    ampm = f"{hr_int}:00"
                                    active_rows.append({
                                        "date_string_final": datetime.strptime(data["date"], "%Y-%m-%d").strftime("%m/%d/%Y"),
                                        "time_display": ampm, "hour_24h": hr_int,
                                        "latitude": geo["latitude"], "longitude_absolute": abs(geo["longitude"]), "tz_value": tz_val,
                                        "temperature_f": hourly["temperature_2m"][i], 
                                        "relative_humidity_percent": int(hourly["relative_humidity_2m"][i]), 
                                        "wind_speed_mph": hourly["wind_speed_10m"][i],
                                        "barometric_pressure_inhg": round(hourly["surface_pressure"][i] * 0.02953, 2)
                                    })
                            
                            st.session_state.final_hourly_rows = active_rows
                            st.session_state.worker_weight = worker_weight
                            st.session_state.step = 2
                            st.rerun()
                except Exception as ex:
                    st.error(f"Failed to process your input query: {ex}")

# --- STEP 2 ---
elif st.session_state.step == 2:
    st.subheader("Step 2: Assign Hourly Workloads")
    
    workload_options = {
        "Light (180W)": {"w": 180, "lbl": "Light"},
        "Moderate (300W)": {"w": 300, "lbl": "Moderate"},
        "Heavy (415W)": {"w": 415, "lbl": "Heavy"},
        "Very Heavy (520W)": {"w": 520, "lbl": "Very Heavy"}
    }
    
    selections = {}
    for row in st.session_state.final_hourly_rows:
        selections[row["hour_24h"]] = st.selectbox(
            f"Workload for hour {row['time_display']}", 
            options=list(workload_options.keys()), index=1
        )
            
    if st.button("Run OSHA Verification", type="primary"):
        for row in st.session_state.final_hourly_rows:
            chosen = workload_options[selections[row["hour_24h"]]]
            row["workload_label"] = chosen["lbl"]
            row["base_watts"] = chosen["w"]
            
        with st.spinner("Automating calculation across OSHA web engine..."):
            results = run_browser_automation(st.session_state.final_hourly_rows, st.session_state.worker_weight)
            
        if results:
            st.session_state.results = results
            st.session_state.step = 3
            st.rerun()

# --- STEP 3 ---
elif st.session_state.step == 3:
    st.subheader("Step 3: Analytical Compliance Summary")
    
    fig = generate_compliance_plot(st.session_state.results, st.session_state.worker_weight)
    st.pyplot(fig)
    
    st.dataframe(st.session_state.results, use_container_width=True)
    
    if st.button("Start Over"):
        st.session_state.step = 1
        st.rerun()
