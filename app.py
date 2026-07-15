import os
import subprocess
import streamlit as st
import time
import math
import csv
import io
import requests
import json
from datetime import datetime
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from playwright.sync_api import sync_playwright
import matplotlib.subplots as plt
import matplotlib.patches as patches
import numpy as np

# =====================================================================
# UTILITIES
# =====================================================================
@st.cache_resource
def install_browser_engine():
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
    except Exception as e:
        st.error(f"Browser engine warning: {e}")

install_browser_engine()

# --- Sanitization Helper ---
def clean_gemini_response(text):
    """Removes markdown backticks and whitespace to ensure valid JSON."""
    return text.replace("```json", "").replace("```", "").strip()

# =====================================================================
# APP LOGIC
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
    if not address or not address.strip():
        return {"error": "Address is empty."}
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
# BROWSER AUTOMATION (BASELINE LOGIC)
# =====================================================================
def run_browser_automation(hourly_data, weight):
    tz_labels = {"-5": "Eastern Time", "-6": "Central Time", "-7": "Mountain Time", "-8": "Pacific Time", "-9": "Alaska", "-10": "Hawaii"}
    computed_results = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context()
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.goto("https://www.osha.gov/heat-exposure/wbgt-calculator")
        
        target_frame = page
        for frame in page.frames:
            try:
                frame.locator('input[name="temp"]').wait_for(state="attached", timeout=2000)
                target_frame = frame
                break
            except: continue
                
        total_rows = len(hourly_data)
        for index, hour in enumerate(hourly_data):
            status_text.text(f"Scraping OSHA Calculator for hour: {hour['time_display']} ({index+1}/{total_rows})...")
            progress_bar.progress((index) / total_rows)
            try:
                formatted_time = f"{hour['hour_24h']:02d}:00"
                target_label = tz_labels.get(hour["tz_value"], "Eastern Time")
                
                target_frame.locator('input[name="dd"]').fill(str(hour["date_string_final"]))
                target_frame.locator('input[name="tm"]').fill(formatted_time)
                target_frame.locator('input[name="lat"]').fill(str(hour["latitude"]))
                target_frame.locator('input[name="lon"]').fill(str(hour["longitude_absolute"]))
                target_frame.locator('input[name="temp"]').fill(str(hour['temperature_f']))
                target_frame.locator('input[name="rh"]').fill(str(hour['relative_humidity_percent']))
                target_frame.locator('input[name="ws"]').fill(str(hour['wind_speed_mph']))
                target_frame.locator('input[name="pres"]').fill(str(hour['barometric_pressure_inhg']))
                
                try: target_frame.locator('select[name="tz"]').select_option(value=hour["tz_value"], timeout=100)
                except: pass
                
                time.sleep(0.05)
                target_frame.locator('input[value="Submit"]').click()
                
                sun_wbgt, shade_wbgt = "---", "---"
                for _ in range(40):  
                    time.sleep(0.05)
                    live_sun_val = target_frame.locator('input[name="wbgt_sun"]').input_value()
                    if live_sun_val and live_sun_val != "---" and live_sun_val.strip() != "":
                        sun_wbgt = live_sun_val.strip()
                        shade_wbgt = target_frame.locator('input[name="wbgt_shade"]').input_value().strip()
                        break
                
                sun_f = float(sun_wbgt.split("/")[1].replace("F","").strip()) if "/" in sun_wbgt else 0.0
                shade_f = float(shade_wbgt.split("/")[1].replace("F","").strip()) if "/" in shade_wbgt else 0.0
                
                adjusted_watts = round((hour["base_watts"] * weight) / 154.0, 1)
                tlv_f = round(((56.7 - (11.5 * math.log10(adjusted_watts))) * 1.8) + 32, 1)
                al_f = round(((59.9 - (14.1 * math.log10(adjusted_watts))) * 1.8) + 32, 1)
                
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
                st.error(f"Error extracting row data for hour {hour['time_display']}: {e}")
        browser.close()
    return computed_results

# =====================================================================
# MATPLOTLIB GRAPHICS COMPLIANCE GENERATOR
# =====================================================================
def generate_compliance_plot(results, weight):
    import matplotlib.pyplot as plt
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
st.set_page_config(page_title="OSHA Heat Stress Dashboard", layout="wide")
st.title("☀️ OSHA-WBGT & ACGIH Heat Stress Compliance Engine")
st.markdown("Automated localized microclimate timeline extraction.")
st.divider()

if "step" not in st.session_state: st.session_state.step = 1

api_key_input = st.sidebar.text_input("Enter Gemini API Key", type="password")
if api_key_input: os.environ["GEMINI_API_KEY"] = api_key_input

if st.session_state.step == 1:
    col1, col2 = st.columns([3, 1])
    with col1:
        user_prompt = st.text_area("Location/Date timeline:", placeholder="e.g., Dallas, Texas, August 12th, 2025, 8 AM to 4 PM.")
    with col2:
        worker_weight = st.number_input("Employee Weight (lbs)", value=154.0)
    
    if st.button("Analyze Shift"):
        if not os.environ.get("GEMINI_API_KEY"): st.error("Missing API Key.")
        else:
            with st.spinner("Processing..."):
                try:
                    client = genai.Client()
                    response = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=user_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction="Extract details from the request. Return ONLY a valid raw JSON object matching this structure: {\"address\": \"string\", \"date\": \"YYYY-MM-DD\", \"start_hour_24h\": int, \"end_hour_24h\": int}",
                            response_mime_type="application/json",
                            response_schema=UserIntent
                        )
                    )
                    
                    # Apply the string cleaner to strip markdown blocks
                    clean_text = clean_gemini_response(response.text)
                    intent = UserIntent.model_validate_json(clean_text)
                    
                    geo = geocode_address_native(intent.address)
                    if "error" in geo: st.error(geo["error"])
                    else:
                        weather = fetch_weather_native(geo["latitude"], geo["longitude"], intent.date)
                        if "hourly" not in weather: st.error("Weather data failed.")
                        else:
                            hourly = weather["hourly"]
                            times, temps, hums, winds, press = hourly["time"], hourly["temperature_2m"], hourly["relative_humidity_2m"], hourly["wind_speed_10m"], hourly["surface_pressure"]
                            
                            try: final_date_str = datetime.strptime(intent.date, "%Y-%m-%d").strftime("%m/%d/%Y")
                            except: final_date_str = intent.date
                            
                            tz_val = get_osha_tz_value(geo["longitude"])
                            active_rows = []
                            for i in range(len(times)):
                                hr_int = int(times[i].split("T")[1].split(":")[0])
                                if intent.start_hour_24h <= hr_int <= intent.end_hour_24h:
                                    ampm = "12:00 AM" if hr_int==0 else ("12:00 PM" if hr_int==12 else (f"{hr_int-12}:00 PM" if hr_int>12 else f"{hr_int}:00 AM"))
                                    active_rows.append({
                                        "date_string_final": final_date_str, "time_display": ampm, "hour_24h": hr_int,
                                        "latitude": geo["latitude"], "longitude_absolute": abs(geo["longitude"]), "tz_value": tz_val,
                                        "temperature_f": temps[i], "relative_humidity_percent": int(hums[i]), "wind_speed_mph": winds[i],
                                        "barometric_pressure_inhg": round(press[i] * 0.02953, 2)
                                    })
                            
                            if not active_rows: st.error("No hours matched your operational shift boundaries.")
                            else:
                                st.session_state.final_hourly_rows = active_rows
                                st.session_state.worker_weight = worker_weight
                                st.session_state.step = 2
                                st.rerun()
                except Exception as ex:
                    st.error(f"System fault: {ex}")

elif st.session_state.step == 2:
    st.subheader("Step 2: Assign Hourly Worker Metabolism / Workloads")
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
                
            with st.spinner("Launching Headless Playwright Context on Cloud Server Core..."):
                results = run_browser_automation(st.session_state.final_hourly_rows, st.session_state.worker_weight)
                
            if results:
                st.session_state.results = results
                st.session_state.step = 3
                st.rerun()
            else:
                st.error("No calculation arrays compiled successfully.")

elif st.session_state.step == 3:
    st.subheader("Step 3: Compliance Engineering Summary Analysis Output")
    
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
        st.rerun()
