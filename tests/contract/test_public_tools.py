"""Provider wire contracts, failure boundaries and local date handling."""

import json
from datetime import datetime

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.config import Settings
from app.contracts import DomainError, Principal
from app.tools.qweather import QWeatherAdapter
from app.tools.schemas import WeatherInput
from app.tools.web_search import WebSearchAdapter, WebSearchInput

STAMP = datetime.fromisoformat("2026-11-22T23:30:00+00:00")
GEO = {"code": "200", "location": [{"id": "tokyo", "name": "Tokyo", "country": "Japan",
        "tz": "Asia/Tokyo", "lat": "35.69", "lon": "139.69"}]}
ATTRIBUTIONS = {"attributions": ["https://developer.qweather.com/attribution.html"]}


def context():
    return RunContext(Principal(user_id="test"), "conversation", "turn", 0, locale="en-US")


def settings():
    return Settings(_env_file=None, weather_mode="real", weather_base_url="https://fixture.qweatherapi.com",
                    weather_api_key="test-weather-key", web_search_api_key="test-search-key")


@pytest.mark.parametrize("units,expected", [("metric", 20), ("imperial", 68)])
async def test_qweather_current_header_units_attribution(monkeypatch, units, expected):
    monkeypatch.setattr("app.tools.qweather.now", lambda: STAMP)
    paths = []

    def backend(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["X-QW-Api-Key"] == "test-weather-key"
        assert "key" not in request.url.params and "authorization" not in request.headers
        if len(paths) == 1:
            return httpx.Response(200, json={**GEO, "location": [*GEO["location"], {**GEO["location"][0], "id": "district", "name": "District"}]})
        assert request.url.params["localTime"] == "true"
        return httpx.Response(200, json={"validTime": STAMP.isoformat(), "condition": {"text": "Clear"},
            "temperature": {"value": 20, "unit": "°C"}, "metadata": ATTRIBUTIONS})

    ctx = context()
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        result = await QWeatherAdapter(settings(), client).invoke(WeatherInput(place="Tokyo", units=units), ctx)
    assert paths == ["/geo/v2/city/lookup", "/weather/v1/current/35.69/139.69"]
    assert result["temperature"]["value"] == expected and not result["stale"]
    assert result["source"]["attributions"] == ATTRIBUTIONS["attributions"]
    assert ctx.cards[0]["place"]["timezone"] == "Asia/Tokyo"


async def test_qweather_forecast_uses_location_date_and_range(monkeypatch):
    monkeypatch.setattr("app.tools.qweather.now", lambda: STAMP)

    def backend(request):
        if request.url.path.endswith("lookup"):
            return httpx.Response(200, json=GEO)
        assert request.url.path == "/weather/v1/daily/35.69/139.69" and request.url.params["days"] == "10"
        return httpx.Response(200, json={"metadata": ATTRIBUTIONS, "days": [{
            "forecastStartTime": "2026-11-24T00:00:00+09:00", "temperatureMin": {"value": 10, "unit": "°C"},
            "temperatureMax": {"value": 21, "unit": "°C"}, "daytime": {"condition": {"text": "Rain"}},
            "nighttime": {"condition": {"text": "Cloudy"}},
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        result = await QWeatherAdapter(settings(), client).invoke(WeatherInput(place="Tokyo", date="tomorrow"), context())
    assert result["valid_at"].startswith("2026-11-24") and result["temperature"] is None
    assert result["temperature_min"]["value"] == 10 and result["temperature_max"]["value"] == 21
    assert "Rain" in result["condition"] and "Cloudy" in result["condition"]


@pytest.mark.parametrize("change", ["temperature", "time", "source", "unit"])
async def test_qweather_malformed_current_data_is_not_spoken(monkeypatch, change):
    monkeypatch.setattr("app.tools.qweather.now", lambda: STAMP)
    payload = {"validTime": STAMP.isoformat(), "temperature": {"value": 20, "unit": "°C"},
               "condition": {"text": "Clear"}, "metadata": ATTRIBUTIONS}
    if change == "temperature":
        payload["temperature"] = {"value": "NaN", "unit": "°C"}
    elif change == "time":
        payload["validTime"] = "2026-11-22T23:30:00"
    elif change == "source":
        payload["metadata"] = {"attributions": ["javascript:alert(1)"]}
    else:
        payload["temperature"] = {"value": 20, "unit": "K"}

    def backend(request):
        return httpx.Response(200, json=GEO if request.url.path.endswith("lookup") else payload)

    ctx = context()
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        with pytest.raises(DomainError, match="TOOL_BAD_RESPONSE"):
            await QWeatherAdapter(settings(), client).invoke(WeatherInput(place="Tokyo"), ctx)
    assert not ctx.cards


async def test_qweather_old_observation_is_marked_stale(monkeypatch):
    monkeypatch.setattr("app.tools.qweather.now", lambda: STAMP)

    def backend(request):
        return httpx.Response(200, json=GEO if request.url.path.endswith("lookup") else {
            "validTime": "2026-11-22T20:00:00Z", "temperature": {"value": 20, "unit": "°C"},
            "condition": {"text": "Clear"}, "metadata": ATTRIBUTIONS,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        result = await QWeatherAdapter(settings(), client).invoke(WeatherInput(place="Tokyo"), context())
    assert result["stale"]


@pytest.mark.parametrize("geo", [{"code": "404"}, {**GEO, "location": GEO["location"] * 2}])
async def test_qweather_unknown_or_ambiguous_place_never_guessed(geo):
    requests = []

    def backend(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=geo)

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        result = await QWeatherAdapter(settings(), client).invoke(WeatherInput(place="Springfield"), context())
    assert result["status"] == "needs_clarification" and len(requests) == 1


@pytest.mark.parametrize("target", ["2026-11-22", "2026-12-03"])
async def test_qweather_out_of_range_never_requests_weather(monkeypatch, target):
    monkeypatch.setattr("app.tools.qweather.now", lambda: STAMP)
    paths = []

    def backend(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=GEO)

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        with pytest.raises(DomainError) as error:
            await QWeatherAdapter(settings(), client).invoke(WeatherInput(place="Tokyo", date=target), context())
    assert error.value.code == "WEATHER_DATE_OUT_OF_RANGE" and paths == ["/geo/v2/city/lookup"]


@pytest.mark.parametrize("adapter,args", [(QWeatherAdapter, WeatherInput(place="Tokyo")),
                                        (WebSearchAdapter, WebSearchInput(query="public standards"))])
@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_public_provider_errors_are_bounded_and_do_not_echo_credentials(adapter, args, status):
    requests = []

    def backend(request):
        requests.append(request)
        return httpx.Response(status, text="secret upstream details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        with pytest.raises(DomainError) as error:
            await adapter(settings(), client).invoke(args, context())
    assert len(requests) == 1 and "secret upstream details" not in str(error.value)


@pytest.mark.parametrize("freshness,time_range", [(None, None), ("pd", "day"), ("pw", "week"), ("pm", "month"), ("py", "year")])
async def test_tavily_sources_and_time_ranges(freshness, time_range):
    def backend(request):
        assert str(request.url) == "https://api.tavily.com/search" and request.method == "POST"
        assert request.headers["Authorization"] == "Bearer test-search-key"
        body = json.loads(request.content)
        assert body.get("time_range") == time_range and body["max_results"] == 3
        assert body["include_answer"] is False and body["include_raw_content"] is False
        assert "api_key" not in body
        return httpx.Response(200, json={"results": [
            {"title": "<b>Standard</b>", "url": "https://example.org/standard", "content": "<i>Public</i> &amp; useful"},
            {"title": "Unsafe", "url": "javascript:alert(1)", "content": "untrusted"},
        ], "answer": "unrequested model output"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        result = await WebSearchAdapter(settings(), client).invoke(WebSearchInput(query="public standards", freshness=freshness), context())
    assert len(result.sources) == 1 and result.sources[0].excerpt == "Public & useful"


@pytest.mark.parametrize("payload", [{}, {"results": {}}, {"results": None}])
async def test_tavily_invalid_response_is_not_a_no_hit(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(DomainError, match="TOOL_BAD_RESPONSE"):
            await WebSearchAdapter(settings(), client).invoke(WebSearchInput(query="public"), context())


@pytest.mark.parametrize("origin", ["http://fixture.qweatherapi.com", "https://evil.example", "https://fixture.qweatherapi.com/a",
                                  "https://fixture.qweatherapi.com?key=secret", "https://u:p@fixture.qweatherapi.com"])
def test_qweather_credentials_only_go_to_dedicated_https_host(origin):
    with pytest.raises(ValueError, match="API Host"):
        Settings(_env_file=None, weather_base_url=origin)
