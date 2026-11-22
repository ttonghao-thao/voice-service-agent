"""Map QWeather GeoAPI and Weather v1 into the existing weather tool contract."""

from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.contracts import DomainError, now
from app.tools.adapters import bounded_json
from app.tools.schemas import Place, Temperature, WeatherClarification, WeatherInput, WeatherResponse
from pydantic import BaseModel, Field, TypeAdapter


class Location(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    adm1: str = ""
    country: str = Field(min_length=1)
    tz: str
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)


class QWeatherAdapter:
    input_model = WeatherInput
    output_adapter = TypeAdapter(WeatherResponse | WeatherClarification)

    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def get(self, path, params):
        data = await bounded_json(
            self.client, "GET", self.settings.weather_base_url.rstrip("/") + path,
            retry=False, limit=65536, params=params,
            headers={"X-QW-Api-Key": self.settings.weather_api_key.get_secret_value()},
            error_map={
                401: ("TOOL_NOT_CONFIGURED", "QWeather credentials are invalid.", 503, False),
                403: ("TOOL_NOT_CONFIGURED", "QWeather access is unavailable.", 503, False),
                429: ("TOOL_RATE_LIMITED", "QWeather rate limit reached.", 503, False),
            },
        )
        if not isinstance(data, dict):
            raise DomainError("TOOL_BAD_RESPONSE", "Invalid QWeather response.", 502)
        code = data.get("code")
        if code is not None and code not in {"200", "404"}:
            raise DomainError("TOOL_UNAVAILABLE", "QWeather could not complete this request.", 502)
        return data

    async def invoke(self, args, ctx):
        if self.settings.weather_mode == "mock":
            return WeatherClarification(status="needs_clarification", message="Live weather is not enabled.", is_mock=True).model_dump()
        if not self.settings.weather_base_url or not self.settings.weather_api_key.get_secret_value():
            raise DomainError("TOOL_NOT_CONFIGURED", "Configure the QWeather API Host and API key.", 503)
        chinese = ctx.locale == "zh-CN"
        geo = await self.get("/geo/v2/city/lookup", {
            "location": args.place, "lang": "zh" if chinese else "en", "number": 10,
        })
        rows = [] if geo.get("code") == "404" else geo.get("location")
        if not isinstance(rows, list) or len(rows) > 10:
            raise DomainError("TOOL_BAD_RESPONSE", "Invalid QWeather locations.", 502)
        locations = [Location.model_validate(row) for row in rows]
        exact = [row for row in locations if row.name.casefold() == args.place.casefold() or row.id == args.place]
        if len(exact) == 1:
            locations = exact
        # GeoAPI supplies a localized country name, not an ISO country code.
        places = [Place(id=row.id, name=", ".join(dict.fromkeys(filter(None, (row.name, row.adm1, row.country)))),
                        country_code="", timezone=row.tz) for row in locations]
        if len(places) != 1:
            return WeatherClarification(
                status="needs_clarification", message="请确认城市和所在国家或地区。" if chinese else "Please confirm the city and country or region.",
                places=places,
            ).model_dump()
        location, place = locations[0], places[0]
        zone = ZoneInfo(place.timezone)
        stamp = now()
        today = stamp.astimezone(zone).date()
        if args.date in (None, "today", "今天"):
            target = today
        elif args.date in ("tomorrow", "明天"):
            target = today + timedelta(days=1)
        else:
            target = date.fromisoformat(args.date)
        if not today <= target <= today + timedelta(days=9):
            raise DomainError("WEATHER_DATE_OUT_OF_RANGE", "QWeather supports today and the next nine days.", 422)
        params = {"lang": "zh" if chinese else "en", "localTime": "true"}
        kind = "current" if target == today else "forecast"
        path = f"/weather/v1/{'current' if kind == 'current' else 'daily'}/{location.lat}/{location.lon}"
        if kind == "forecast":
            params["days"] = 10
        data = await self.get(path, params)
        try:
            if kind == "current":
                valid = datetime.fromisoformat(data["validTime"].replace("Z", "+00:00"))
                if not valid.tzinfo or (valid - stamp).total_seconds() > 300:
                    raise ValueError("Invalid weather time")
                stale = (stamp - valid).total_seconds() > 3600
                fields = {"temperature": self.temperature(data["temperature"], args.units)}
                condition = data["condition"]["text"]
            else:
                matches = []
                for row in data["days"]:
                    start = datetime.fromisoformat(row["forecastStartTime"].replace("Z", "+00:00"))
                    if not start.tzinfo:
                        raise ValueError("Forecast requires an explicit timezone")
                    if start.astimezone(zone).date() == target:
                        matches.append(row)
                if len(matches) != 1:
                    raise DomainError("WEATHER_DATE_OUT_OF_RANGE", "The requested date has no forecast.", 422)
                row = matches[0]
                valid = datetime.combine(target, time(), zone)
                stale = False
                fields = {"temperature_min": self.temperature(row["temperatureMin"], args.units),
                          "temperature_max": self.temperature(row["temperatureMax"], args.units)}
                day, night = row["daytime"]["condition"]["text"], row["nighttime"]["condition"]["text"]
                condition = f"白天：{day}；夜间：{night}" if chinese else f"Day: {day}; night: {night}"
            if not isinstance(condition, str) or not condition.strip():
                raise ValueError("Missing weather conditions")
            urls = data["metadata"]["attributions"]
            if not isinstance(urls, list) or not urls or len(urls) > 10:
                raise ValueError("Missing weather attribution")
            for url in urls:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or len(url) > 1500:
                    raise ValueError("Invalid weather attribution")
            result = WeatherResponse(
                status="ok", place=place, kind=kind, valid_at=valid.isoformat(), fetched_at=stamp.isoformat(),
                condition=condition, source={"provider": "QWeather", "is_mock": False, "attributions": urls},
                stale=stale, **fields,
            ).model_dump(mode="json")
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError("TOOL_BAD_RESPONSE", "QWeather returned incomplete weather data.", 502) from exc
        ctx.slots.update(weather_place=result["place"], weather_units=args.units, weather_last_date=target.isoformat())
        ctx.cards.append({"type": "weather", **result})
        return result

    @staticmethod
    def temperature(data, units):
        if data["unit"] not in {"°C", "°F"}:
            raise ValueError("Unsupported temperature unit")
        value = Temperature(value=data["value"], unit=data["unit"][-1]).value
        if units == "imperial":
            value = value * 9 / 5 + 32 if data["unit"] == "°C" else value
        elif data["unit"] == "°F":
            value = (value - 32) * 5 / 9
        return Temperature(value=round(value, 2), unit="F" if units == "imperial" else "C")
