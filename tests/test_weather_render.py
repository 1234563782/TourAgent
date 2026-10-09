import json
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from business_travel.graph import finalize_business_reply
from business_travel.weather import render_weather


class WeatherRenderTests(unittest.TestCase):
    def test_current_tool_values_replace_invented_wind_scale(self):
        result = {"status": "ok", "source": "fixture", "forecasts": [{
            "date": "2099-09-09", "mintempC": "22", "maxtempC": "26", "hourly": [{
                "time": "900", "tempC": "25", "windspeedKmph": "22", "chanceofrain": "14",
                "weatherDesc": [{"value": "Overcast"}]}]}]}
        response = finalize_business_reply([
            HumanMessage(content="天气"), ToolMessage(name="query_weather", tool_call_id="test",
                                                    content=json.dumps(result))],
            AIMessage(content="晴天，3级风，最高40度"))
        self.assertIn("09:00", response.content)
        self.assertIn("22 千米/小时", response.content)
        self.assertIn("Overcast", response.content)
        self.assertNotIn("3级", response.content)
        self.assertNotIn("40度", response.content)

    def test_missing_weather_does_not_become_forecast(self):
        for result in ({}, {"status": "error"}, {"status": "ok", "forecasts": []}):
            self.assertIn("不能确认", render_weather(result))

    def test_old_weather_not_reused_for_new_turn(self):
        answer = AIMessage(content="请说明日期")
        response = finalize_business_reply([
            ToolMessage(name="query_weather", tool_call_id="old", content='{"status":"error"}'),
            HumanMessage(content="下周呢")], answer)
        self.assertEqual(response.content, answer.content)
