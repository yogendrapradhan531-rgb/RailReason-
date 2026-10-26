"""
Real weather lookup via OpenWeatherMap, keyed by station lat/lng (which
fetch_schedule.py now captures from RailRadar's timetable data).

This replaces the synthetic "winter + night hours = maybe fog" heuristic
with an actual current weather reading at the train's location — a real
signal instead of a guess.

Setup:
  1. Free signup: https://openweathermap.org/api (no card needed)
  2. Get your API key from https://home.openweathermap.org/api_keys
     (note: new keys can take up to ~10 minutes to activate)
  3. Set OPENWEATHER_API_KEY env var, or hardcode below.

Free tier: 1,000 calls/day, 60 calls/minute — much more generous than
RailRadar's, so this isn't the bottleneck.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

OWM_BASE = "https://api.openweathermap.org/data/2.5/weather"
API_KEY = os.environ.get("OPENWEATHER_API_KEY")

FOG_CONDITIONS = {"Fog", "Mist", "Haze", "Smoke"}
HEAVY_RAIN_KEYWORDS = ("heavy", "extreme", "violent")


class WeatherError(Exception):
    pass


def get_weather_at(lat: float, lon: float) -> dict:
    """
    Returns:
      {
        "condition": "Fog" | "Rain" | "Clear" | ... (OpenWeatherMap's 'main' field),
        "description": "light rain" (human readable),
        "visibility_km": float or None,
        "is_foggy": bool,
        "is_heavy_rain": bool,
        "temp_c": float,
      }
    """
    if not API_KEY:
        raise WeatherError(
            "No API key set. Set OPENWEATHER_API_KEY env var, or hardcode it in weather_client.py."
        )

    params = {"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"}
    try:
        resp = requests.get(OWM_BASE, params=params, timeout=10)
    except requests.RequestException as e:
        raise WeatherError(f"Network error calling OpenWeatherMap: {e}") from e

    if resp.status_code == 401:
        raise WeatherError("Unauthorized — check OPENWEATHER_API_KEY (new keys take ~10 min to activate).")
    resp.raise_for_status()
    body = resp.json()

    weather = (body.get("weather") or [{}])[0]
    condition = weather.get("main", "Unknown")
    description = weather.get("description", "")
    visibility_m = body.get("visibility")  # meters, OpenWeatherMap caps this at 10000
    visibility_km = (visibility_m / 1000.0) if visibility_m is not None else None

    is_foggy = condition in FOG_CONDITIONS or (visibility_km is not None and visibility_km < 1.0)
    is_heavy_rain = condition == "Rain" and any(k in description.lower() for k in HEAVY_RAIN_KEYWORDS)

    return {
        "condition": condition,
        "description": description,
        "visibility_km": visibility_km,
        "is_foggy": is_foggy,
        "is_heavy_rain": is_heavy_rain,
        "temp_c": body.get("main", {}).get("temp"),
    }


# ============================================================
# Quick manual test
# ============================================================

if __name__ == "__main__":
    # New Delhi coordinates, as a default sanity check
    lat, lon = 28.6139, 77.2090
    print(f"Fetching weather at ({lat}, {lon})...")
    try:
        w = get_weather_at(lat, lon)
        print(w)
    except WeatherError as e:
        print(f"FAILED: {e}")