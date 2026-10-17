"""Risk requests must select the correct tool and supply its real schema's arguments."""
import os

import pytest
from google.adk.models import LlmRequest
from google.adk.tools import FunctionTool
from google.genai import types

os.environ.setdefault("CARGOMIND_OFFLINE", "1")


@pytest.mark.parametrize(("prompt", "tool", "args"), [
    ("Mumbai to Pune risk?", "check_route_disruptions", {"origin": "Mumbai", "destination": "Pune"}),
    ("Risk from Mumbai to Pune?", "check_route_disruptions", {"origin": "Mumbai", "destination": "Pune"}),
    ("Weather Mumbai to Pune?", "check_route_disruptions", {"origin": "Mumbai", "destination": "Pune"}),
    ("Any weather risk on Mumbai → Pune today?", "check_route_disruptions", {"origin": "Mumbai", "destination": "Pune"}),
    ("Any weather risk in Chennai?", "get_weather_risk", {"city": "Chennai"}),
    ("Risk in Chennai?", "get_weather_risk", {"city": "Chennai"}),
    ("Mumbai → Pune distance", "get_route", {"origin": "Mumbai", "destination": "Pune", "avoid_tolls": False}),
    ("Mumbai -> Pune", "get_route", {"origin": "Mumbai", "destination": "Pune", "avoid_tolls": False}),
])
def test_route_risk_policy_uses_city_count_and_real_tool_schemas(prompt, tool, args):
    from cargomind.agent import route_risk_agent
    from cargomind.tools import geo, planning, weather

    tools = [geo.get_route, geo.optimize_multi_stop_route, geo.geocode,
             weather.get_weather_risk, weather.check_route_disruptions, planning.quote_carriers]
    req = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        tools_dict={fn.__name__: FunctionTool(fn) for fn in tools},
    )
    offline = route_risk_agent.model.chain[-1]
    resp = offline._respond(req)
    fc = resp.content.parts[0].function_call
    assert fc is not None and fc.name == tool
    assert fc.args == args
