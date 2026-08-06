import streamlit as st
import requests
import time
import urllib.parse

# ---------------------------------------------------------
# HELPER FUNCTIONS (Geocoding & Assessment Logic)
# ---------------------------------------------------------
def geocode_address(street, city, state, zip_code):
    """Fetches coordinates for a given address using free OpenStreetMap Nominatim API."""
    query = f"{street}, {city}, {state} {zip_code}"
    safe_query = urllib.parse.quote(query)
    url = f"https://nominatim.openstreetmap.org/search?q={safe_query}&format=json&limit=1"
    headers = {'User-Agent': 'OSHA-WBGT-Assessment-Tool/1.0'}
    
    try:
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        if data:
            return data[0]['display_name'], float(data[0]['lat']), float(data[0]['lon'])
        return None, None, None
    except Exception as e:
        st.error(f"Geocoding error: {e}")
        return None, None, None

def reverse_geocode(lat, lon):
    """Fetches an address for given coordinates using free OpenStreetMap Nominatim API."""
    url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json"
    headers = {'User-Agent': 'OSHA-WBGT-Assessment-Tool/1.0'}
    
    try:
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        if 'display_name' in data:
            return data['display_name']
        return "Remote Site (No official address found for these coordinates)"
    except Exception as e:
        st.error(f"Reverse geocoding error: {e}")
        return "Error retrieving address"

def run_pro_model_assessment(lat, lon):
    """Executes the Pro Model occupational exposure assessment."""
    # This function simulates the Pro Model processing time and outputs
    time.sleep(2) 
    return {
        "estimated_wbgt_f": 86.4,
        "risk_level": "High Risk",
        "work_rest_cycle": "45 minutes work / 15 minutes rest",
        "hydration_guideline": "1 cup (8 oz) of water every 20 minutes"
    }

# ---------------------------------------------------------
# SESSION STATE INITIALIZATION
# ---------------------------------------------------------
if "location_fetched" not in st.session_state:
    st.session_state.location_fetched = False
if "location_confirmed" not in st.session_state:
    st.session_state.location_confirmed = False
if "resolved_address" not in st.session_state:
    st.session_state.resolved_address = ""
if "resolved_coords" not in st.session_state:
    st.session_state.resolved_coords = None
if "assessment_results" not in st.session_state:
    st.session_state.assessment_results = None

def reset_location_state():
    """Resets the confirmation and assessment states when location inputs change."""
    st.session_state.location_fetched = False
    st.session_state.location_confirmed = False
    st.session_state.assessment_results = None
    st.session_state.resolved_address = ""
    st.session_state.resolved_coords = None

# ---------------------------------------------------------
# MAIN APP UI
# ---------------------------------------------------------
st.set_page_config(page_title="OSHA WBGT Exposure Assessment", layout="wide")

st.title("OSHA WBGT Exposure Assessment Tool")
st.markdown("Assess environmental heat stress risks utilizing the updated Pro Model.")
st.markdown("---")

# 1. LOCATION INPUT SECTION
st.subheader("1. Location Entry")
input_method = st.radio(
    "Select Location Input Method:", 
    ["Standard Address (City, State, Zip)", "Exact GPS Coordinates (Remote Sites)"],
    on_change=reset_location_state
)

if input_method == "Standard Address (City, State, Zip)":
    col1, col2, col3, col4 = st.columns([3, 2, 1, 1])
    with col1:
        street_input = st.text_input("Street Address", value="7339 State Road", on_change=reset_location_state)
    with col2:
        city_input = st.text_input("City", value="Philadelphia", on_change=reset_location_state)
    with col3:
        state_input = st.text_input("State", value="PA", on_change=reset_location_state)
    with col4:
        zip_input = st.text_input("Zip Code", value="19136", on_change=reset_location_state)
        
    if st.button("Retrieve Location", type="primary"):
        with st.spinner("Geocoding address..."):
            addr, lat, lon = geocode_address(street_input, city_input, state_input, zip_input)
            if addr and lat and lon:
                st.session_state.resolved_address = addr
                st.session_state.resolved_coords = (lat, lon)
                st.session_state.location_fetched = True
                st.session_state.location_confirmed = False
            else:
                st.error("Could not resolve this address. Please check your inputs or try GPS coordinates.")

elif input_method == "Exact GPS Coordinates (Remote Sites)":
    col1, col2 = st.columns(2)
    with col1:
        lat_input = st.number_input("Latitude", value=40.033703, format="%.6f", on_change=reset_location_state)
    with col2:
        lon_input = st.number_input("Longitude", value=-75.029837, format="%.6f", on_change=reset_location_state)
        
    if st.button("Retrieve Location", type="primary"):
        with st.spinner("Pinging map for coordinates..."):
            addr = reverse_geocode(lat_input, lon_input)
            st.session_state.resolved_address = addr
            st.session_state.resolved_coords = (lat_input, lon_input)
            st.session_state.location_fetched = True
            st.session_state.location_confirmed = False

# 2. ADDRESS CONFIRMATION SECTION
if st.session_state.location_fetched and not st.session_state.location_confirmed:
    st.markdown("---")
    st.subheader("2. Confirm Location")
    st.warning("⚠️ Please verify that the retrieved location is correct before running the exposure assessment.")
    
    st.markdown(f"**Retrieved Address / Area:** {st.session_state.resolved_address}")
    st.markdown(f"**Exact Coordinates:** {st.session_state.resolved_coords[0]}, {st.session_state.resolved_coords[1]}")
    
    if st.button("✅ Confirm Address and Proceed", type="primary"):
        st.session_state.location_confirmed = True
        st.rerun()

# 3. PRO MODEL EXPOSURE ASSESSMENT SECTION
if st.session_state.location_confirmed:
    st.markdown("---")
    st.subheader("3. Pro Model Exposure Assessment")
    st.success(f"Location Confirmed: {st.session_state.resolved_coords[0]}, {st.session_state.resolved_coords[1]}")
    
    if st.button("Run Exposure Assessment (Pro Model)", type="primary"):
        with st.spinner("Running Pro Model calculations..."):
            lat, lon = st.session_state.resolved_coords
            results = run_pro_model_assessment(lat, lon)
            st.session_state.assessment_results = results
            
    if st.session_state.assessment_results:
        res = st.session_state.assessment_results
        
        st.markdown("### Assessment Results")
        col1, col2 = st.columns(2)
        
        with col1:
            st.metric("Estimated WBGT", f"{res['estimated_wbgt_f']} °F", res['risk_level'], delta_color="inverse")
            
        with col2:
            st.info(f"**Work/Rest Cycle:**\n\n{res['work_rest_cycle']}")
            st.info(f"**Hydration Guideline:**\n\n{res['hydration_guideline']}")
            
        if st.button("Start New Assessment"):
            reset_location_state()
            st.rerun()
