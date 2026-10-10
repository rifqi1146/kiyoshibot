import asyncio
import contextlib
import html
import aiohttp

from telegram import Update
from telegram.ext import ContextTypes
from utils.http import get_http_session
from utils.rich_msg import Report, edit_rich

WEATHER_SPIN_FRAMES = ["🌤", "⛅", "🌥", "☁️", "🌦", "🌈"]

def _weather_code_to_text(code: int) -> str:
    mapping = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Depositing rime fog",
        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",
        56: "Light freezing drizzle",
        57: "Dense freezing drizzle",
        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",
        66: "Light freezing rain",
        67: "Heavy freezing rain",
        71: "Slight snow fall",
        73: "Moderate snow fall",
        75: "Heavy snow fall",
        77: "Snow grains",
        80: "Slight rain showers",
        81: "Moderate rain showers",
        82: "Violent rain showers",
        85: "Slight snow showers",
        86: "Heavy snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm with slight hail",
        99: "Thunderstorm with heavy hail",
    }
    return mapping.get(code, "Unknown")

def _wind_dir_from_degrees(deg) -> str:
    if deg is None:
        return "N/A"
    try:
        value = float(deg)
    except Exception:
        return "N/A"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = int((value + 11.25) // 22.5) % 16
    return dirs[idx]

def _format_location(loc: dict) -> str:
    parts = [loc.get("name"), loc.get("admin1"), loc.get("country")]
    return ", ".join(str(x) for x in parts if x)

def _to_int(value, default: int = -1) -> int:
    try:
        return int(value)
    except Exception:
        return default

async def _safe_edit(message, text: str):
    try:
        await message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            return

async def _spinner(message, city: str, stop_event: asyncio.Event):
    i = 0
    while not stop_event.is_set():
        frame = WEATHER_SPIN_FRAMES[i % len(WEATHER_SPIN_FRAMES)]
        await _safe_edit(message, f"{frame} Fetching weather for <b>{html.escape(city.title())}</b>...\nPlease wait...")
        i += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass

async def _resolve_location(city: str) -> dict:
    session = await get_http_session()
    async with session.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={
            "name": city,
            "count": 1,
            "language": "en",
            "format": "json",
        },
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Geocoding API returned HTTP {resp.status}")
        data = await resp.json(content_type=None)
    results = data.get("results") or []
    if not results:
        raise ValueError("Location not found")
    return results[0]

async def _fetch_weather(city: str) -> tuple[dict, dict]:
    location = await _resolve_location(city)
    session = await get_http_session()
    async with session.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "current": ",".join([
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "weather_code",
                "cloud_cover",
                "wind_speed_10m",
                "wind_direction_10m",
                "is_day",
            ]),
            "daily": "sunrise,sunset",
            "timezone": "auto",
            "forecast_days": 1,
        },
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Weather API returned HTTP {resp.status}")
        data = await resp.json(content_type=None)
    return location, data

async def weather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    city = " ".join(context.args or []).strip()
    if not city:
        return await msg.reply_text("Example: <code>/weather jakarta</code>", parse_mode="HTML")
    status_msg = await msg.reply_text(f"🌤 Fetching weather for <b>{html.escape(city.title())}</b>...", parse_mode="HTML")
    stop_event = asyncio.Event()
    spin_task = asyncio.create_task(_spinner(status_msg, city, stop_event))
    try:
        location, data = await _fetch_weather(city)
        current = data.get("current", {}) or {}
        current_units = data.get("current_units", {}) or {}
        daily = data.get("daily", {}) or {}
        weather_code = _to_int(current.get("weather_code"))
        weather_desc = _weather_code_to_text(weather_code)
        temp = current.get("temperature_2m", "N/A")
        temp_unit = current_units.get("temperature_2m", "°C")
        feels = current.get("apparent_temperature", "N/A")
        feels_unit = current_units.get("apparent_temperature", "°C")
        humidity = current.get("relative_humidity_2m", "N/A")
        humidity_unit = current_units.get("relative_humidity_2m", "%")
        wind_speed = current.get("wind_speed_10m", "N/A")
        wind_speed_unit = current_units.get("wind_speed_10m", "km/h")
        wind_dir = _wind_dir_from_degrees(current.get("wind_direction_10m"))
        cloud = current.get("cloud_cover", "N/A")
        cloud_unit = current_units.get("cloud_cover", "%")
        sunrise = (daily.get("sunrise") or ["N/A"])[0]
        sunset = (daily.get("sunset") or ["N/A"])[0]
        observed_time = current.get("time", "N/A")
        tz_abbr = data.get("timezone_abbreviation") or data.get("timezone") or "Local"
        location_name = _format_location(location)

        stop_event.set()
        spin_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await spin_task

        rep = Report(f"🌤 Weather — {location_name}", observed_time, aside="Data via Open-Meteo")
        rep.add("🔎 Current", [
            ("Condition", weather_desc),
            ("Temperature", f"{temp}{temp_unit}"),
            ("Feels like", f"{feels}{feels_unit}"),
            ("Humidity", f"{humidity}{humidity_unit}"),
            ("Cloud Cover", f"{cloud}{cloud_unit}"),
        ])
        rep.add("💨 Wind", [
            ("Speed", f"{wind_speed} {wind_speed_unit}"),
            ("Direction", wind_dir),
        ])
        rep.add("🌅 Sun", [
            ("Sunrise", sunrise),
            ("Sunset", sunset),
        ])
        rep.add("🕒 Observation", [
            ("Observed", observed_time),
            ("Timezone", tz_abbr),
        ])
        rich_html, plain_html = rep.build()
        await edit_rich(status_msg.get_bot(), status_msg.chat_id, status_msg.message_id, rich_html, plain_html)
    except ValueError as e:
        stop_event.set()
        spin_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await spin_task
        await _safe_edit(status_msg, f"📍 {html.escape(str(e))}")
    except asyncio.TimeoutError:
        stop_event.set()
        spin_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await spin_task
        await _safe_edit(status_msg, "Request timed out. Try again later.")
    except Exception as e:
        stop_event.set()
        spin_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await spin_task
        await _safe_edit(status_msg, f"Couldn't fetch weather data.\n\n<code>{html.escape(str(e))}</code>")
            