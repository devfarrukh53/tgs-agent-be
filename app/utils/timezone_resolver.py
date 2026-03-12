"""
Resolve IANA timezone from city name (and optional country) using geopy + timezonefinder.
Used when user provides only city for scheduled-call timezone.
"""
from typing import Optional

from app.core.logger import logger


def resolve_timezone_from_city(city: str, country: Optional[str] = None) -> Optional[str]:
    """
    Resolve IANA timezone (e.g. 'Asia/Karachi') from city name and optional country.

    Uses geopy (Nominatim) for city -> (lat, lon) and timezonefinder for (lat, lon) -> timezone.
    Returns None if geocoding or timezone lookup fails.
    """
    if not (city or "").strip():
        return None
    try:
        from geopy.geocoders import Nominatim
        from timezonefinder import TimezoneFinder

        location_str = city.strip()
        if country and str(country).strip():
            location_str = f"{location_str}, {country.strip()}"

        geolocator = Nominatim(user_agent="tgs-agent-scheduler")
        tf = TimezoneFinder()

        coords = geolocator.geocode(location_str)
        if not coords:
            logger.warning(f"Timezone resolver: no coordinates for '{location_str}'")
            return None

        tz_name = tf.timezone_at(lng=coords.longitude, lat=coords.latitude)
        if tz_name:
            logger.info(f"Timezone resolver: '{location_str}' -> {tz_name}")
        return tz_name
    except Exception as e:
        logger.warning(f"Timezone resolver failed for '{city}' / '{country}': {e}")
        return None
