from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@dataclass(frozen=True)
class TimeRange:
    start: time
    end: time


@dataclass(frozen=True)
class CommuteScene:
    title: str
    target_date: date
    include_morning: bool
    include_evening: bool
    morning_label: str = "上班"
    evening_label: str = "下班"


class QWeatherError(Exception):
    """Raised when QWeather returns an error or an invalid payload."""


class QWeatherClient:
    def __init__(self, api_key: str, api_host: str, timeout: int = 10):
        self.api_key = api_key.strip()
        self.api_host = api_host.rstrip("/").strip() or "https://devapi.qweather.com"
        self.timeout = max(1, timeout)
        self._location_cache: dict[str, str] = {}

    async def lookup_location(self, keyword: str) -> str:
        keyword = keyword.strip()
        if not keyword:
            raise QWeatherError("城市或地址不能为空。")
        if keyword in self._location_cache:
            return self._location_cache[keyword]
        payload = await self._get_json("/geo/v2/city/lookup", {"location": keyword, "range": "cn"})
        locations = payload.get("location") or []
        if not locations:
            raise QWeatherError(f"未找到位置：{keyword}")
        location_id = str(locations[0].get("id") or "").strip()
        if not location_id:
            raise QWeatherError(f"和风天气未返回有效位置 ID：{keyword}")
        self._location_cache[keyword] = location_id
        return location_id

    async def daily_weather(self, keyword: str) -> list[dict[str, Any]]:
        location_id = await self.lookup_location(keyword)
        payload = await self._get_json("/v7/weather/3d", {"location": location_id})
        return payload.get("daily") or []

    async def hourly_weather(self, keyword: str) -> list[dict[str, Any]]:
        location_id = await self.lookup_location(keyword)
        payload = await self._get_json("/v7/weather/24h", {"location": location_id})
        return payload.get("hourly") or []

    async def weather_warning(self, keyword: str) -> list[dict[str, Any]]:
        location_id = await self.lookup_location(keyword)
        payload = await self._get_json("/v7/warning/now", {"location": location_id})
        return payload.get("warning") or []

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise QWeatherError("尚未在插件配置页面填写和风天气 API Key。")
        query = {**params, "key": self.api_key, "lang": "zh", "unit": "m"}
        url = f"{self.api_host}{path}?{urllib.parse.urlencode(query)}"
        return await asyncio.to_thread(self._blocking_get_json, url)

    def _blocking_get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": "astrbot-plugin-qweather/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise QWeatherError(f"和风天气 HTTP 错误：{exc.code}") from exc
        except urllib.error.URLError as exc:
            raise QWeatherError(f"和风天气请求失败：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise QWeatherError("和风天气返回了无法解析的数据。") from exc
        code = str(data.get("code", ""))
        if code and code != "200":
            raise QWeatherError(f"和风天气返回错误码：{code}")
        return data


@register("astrbot_plugin_qweather", "OpenAI", "和风天气通勤建议插件", "0.1.0")
class QWeatherPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = QWeatherClient(
            api_key=self._conf("qweather_api_key"),
            api_host=self._conf("qweather_api_host", "https://devapi.qweather.com"),
            timeout=int(self._conf("request_timeout_seconds", 10) or 10),
        )

    @filter.command("天气")
    async def weather(self, event: AstrMessageEvent, city: str | None = None):
        """查询明日天气、未来三天简报、穿衣和生活建议。"""
        try:
            location = (city or self._conf("default_city")).strip()
            if not location:
                yield event.plain_result("请在插件配置页面设置默认城市，或使用：/天气 北京")
                return
            daily = await self.client.daily_weather(location)
            warnings = await self._safe_warnings(location)
            draft = self._build_weather_payload(location, daily, warnings)
            fallback = self._format_weather(draft)
            text = await self._polish_with_model(event, "weather", draft, fallback)
            yield event.plain_result(text)
        except QWeatherError as exc:
            yield event.plain_result(f"天气查询失败：{exc}")
        except Exception as exc:  # AstrBot should return user-friendly failures for command handlers.
            logger.exception("/天气 执行失败")
            yield event.plain_result(f"天气查询失败：{exc}")

    @filter.command("通勤")
    async def commute(self, event: AstrMessageEvent):
        """根据当前时间智能输出通勤天气和交通方式建议。"""
        missing = self._missing_commute_config()
        if missing:
            yield event.plain_result("通勤配置不完整，请先在插件配置页面填写：" + "、".join(missing))
            return
        try:
            morning = self._parse_time_range(self._conf("morning_commute_time"))
            evening = self._parse_time_range(self._conf("evening_commute_time"))
            self._validate_commute_ranges(morning, evening)
            scene = self._determine_commute_scene(datetime.now(), morning, evening)
            payload = await self._build_commute_payload(scene, morning, evening)
            fallback = self._format_commute(payload)
            text = await self._polish_with_model(event, "commute", payload, fallback)
            yield event.plain_result(text)
        except ValueError as exc:
            yield event.plain_result(f"通勤时间配置有误：{exc}")
        except QWeatherError as exc:
            yield event.plain_result(f"通勤天气查询失败：{exc}")
        except Exception as exc:
            logger.exception("/通勤 执行失败")
            yield event.plain_result(f"通勤建议生成失败：{exc}")

    @filter.command("天气帮助")
    async def weather_help(self, event: AstrMessageEvent):
        """查看天气插件可用命令。"""
        yield event.plain_result(
            "🌤️ 和风天气插件帮助\n\n"
            "可用命令：\n"
            "1. /天气 [城市]\n"
            "   查询指定城市明日详细天气、未来 3 天简报、穿衣和生活建议。\n"
            "   示例：/天气 北京\n\n"
            "2. /天气\n"
            "   查询插件配置页面中的默认城市。\n\n"
            "3. /通勤\n"
            "   根据当前时间自动判断输出今日/明日上班、下班天气和交通方式建议。\n\n"
            "需要先在插件配置页面填写：和风天气 API Key、API Host、默认城市、家地址、公司地址、"
            "上下班时间段、可选通勤方式。"
        )

    def _conf(self, key: str, default: Any = "") -> Any:
        try:
            return self.config.get(key, default)
        except AttributeError:
            return self.config[key] if key in self.config else default

    async def _safe_warnings(self, location: str) -> list[dict[str, Any]]:
        try:
            return await self.client.weather_warning(location)
        except Exception as exc:
            logger.warning(f"天气预警查询失败，已忽略：{exc}")
            return []

    def _build_weather_payload(self, location: str, daily: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> dict[str, Any]:
        if not daily:
            raise QWeatherError("和风天气没有返回未来天气数据。")
        tomorrow = daily[1] if len(daily) > 1 else daily[0]
        three_days = daily[:3]
        return {
            "location": location,
            "target": "明日天气",
            "tomorrow": tomorrow,
            "next_three_days": three_days,
            "warnings": warnings,
            "rule_advice": self._weather_rule_advice(tomorrow, three_days, warnings),
        }

    def _weather_rule_advice(self, day: dict[str, Any], three_days: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> dict[str, str]:
        temp_min = self._to_int(day.get("tempMin"))
        temp_max = self._to_int(day.get("tempMax"))
        pop = self._to_int(day.get("precip")) or self._to_int(day.get("pop"))
        weather_text = f"{day.get('textDay', '')}{day.get('textNight', '')}"
        rain_like = any(word in weather_text for word in ["雨", "雪", "阵雨", "雷阵雨"])
        if temp_min <= 0:
            outfit = "厚羽绒服、围巾、手套等保暖装备。"
        elif temp_min <= 8:
            outfit = "羽绒服或厚大衣，早晚注意保暖。"
        elif temp_min <= 15:
            outfit = "毛衣/卫衣搭配外套，适合分层穿搭。"
        elif temp_min <= 22:
            outfit = "长袖或薄外套，白天热时可适当减衣。"
        elif temp_max <= 28:
            outfit = "短袖或薄衬衫即可，早晚可备薄外套。"
        else:
            outfit = "清凉透气衣物，注意补水和防晒。"
        umbrella = "强烈建议带伞。" if pop >= 60 or rain_like else "可带折叠伞备用。" if pop >= 30 else "一般不需要带伞。"
        car_wash = "未来 3 天有降水，不建议洗车。" if any("雨" in f"{d.get('textDay','')}{d.get('textNight','')}" for d in three_days) else "近期降水风险不高，可以考虑洗车。"
        outdoor = "有天气预警，户外活动请谨慎安排。" if warnings else "可根据体感安排轻度户外活动。"
        return {"穿衣": outfit, "带伞": umbrella, "洗车": car_wash, "户外": outdoor}

    async def _build_commute_payload(self, scene: CommuteScene, morning: TimeRange, evening: TimeRange) -> dict[str, Any]:
        home = self._conf("home_location")
        work = self._conf("work_location")
        sections: list[dict[str, Any]] = []
        if scene.include_morning:
            sections.append(await self._build_commute_section("上班", home, work, scene.target_date, morning))
        if scene.include_evening:
            sections.append(await self._build_commute_section("下班", work, home, scene.target_date, evening))
        return {
            "scene": scene.title,
            "target_date": scene.target_date.isoformat(),
            "home_location": home,
            "work_location": work,
            "commute_methods": self._conf("commute_methods"),
            "sections": sections,
        }

    async def _build_commute_section(self, name: str, origin: str, destination: str, target_date: date, time_range: TimeRange) -> dict[str, Any]:
        hourly = await self.client.hourly_weather(origin)
        matched = self._match_hourly(hourly, target_date, time_range)
        return {
            "name": name,
            "origin": origin,
            "destination": destination,
            "time_range": self._format_range(time_range),
            "weather": matched,
            "rule_summary": self._commute_rule_summary(matched),
        }

    def _match_hourly(self, hourly: list[dict[str, Any]], target_date: date, time_range: TimeRange) -> list[dict[str, Any]]:
        matched = []
        for item in hourly:
            fx_time = self._parse_fx_time(item.get("fxTime", ""))
            if not fx_time or fx_time.date() != target_date:
                continue
            current = fx_time.time().replace(minute=0, second=0, microsecond=0)
            start_hour = time_range.start.replace(minute=0, second=0, microsecond=0)
            end_hour = time_range.end.replace(minute=0, second=0, microsecond=0)
            if start_hour <= current <= end_hour:
                matched.append(item)
        return matched[:4]

    def _commute_rule_summary(self, hours: list[dict[str, Any]]) -> str:
        if not hours:
            return "未匹配到该时间段逐小时天气，请参考相邻时间。"
        texts = "".join(str(h.get("text", "")) for h in hours)
        pops = [self._to_int(h.get("pop")) for h in hours]
        winds = [self._to_int(h.get("windScale")) for h in hours]
        max_pop = max(pops or [0])
        max_wind = max(winds or [0])
        if any(word in texts for word in ["雨", "雪", "阵雨", "雷阵雨"]) or max_pop >= 60:
            return "降水风险较高，优先考虑公共交通或打车，骑行和步行需谨慎。"
        if max_wind >= 5:
            return "风力偏大，骑行体验较差，注意防风。"
        return "天气对通勤影响不大，可按平时习惯选择出行方式。"

    def _determine_commute_scene(self, now: datetime, morning: TimeRange, evening: TimeRange) -> CommuteScene:
        today = now.date()
        now_time = now.time().replace(second=0, microsecond=0)
        if now_time < morning.start:
            return CommuteScene("今日上班 + 今日下班", today, True, True)
        if morning.start <= now_time <= morning.end:
            return CommuteScene("当前上班时段 + 今日下班", today, True, True, "当前上班")
        if morning.end < now_time < evening.start:
            return CommuteScene("今日下班", today, False, True)
        if evening.start <= now_time <= evening.end:
            return CommuteScene("当前下班时段", today, False, True, evening_label="当前下班")
        return CommuteScene("明日上班 + 明日下班", today + timedelta(days=1), True, True)

    async def _polish_with_model(self, event: AstrMessageEvent, kind: str, payload: dict[str, Any], fallback: str) -> str:
        if not self._conf("enable_model_polish", True):
            return fallback
        provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            return fallback
        prompt = self._model_prompt(kind, payload, fallback)
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt="你是一个简洁可靠的天气和通勤建议助手。", session_id=None),
                timeout=int(self._conf("model_timeout_seconds", 45) or 45),
            )
            text = re.sub(r"<think>[\s\S]*?</think>", "", getattr(response, "completion_text", "") or "").strip()
            return text or fallback
        except Exception as exc:
            logger.warning(f"模型整理失败，使用模板输出：{exc}")
            return fallback

    def _model_prompt(self, kind: str, payload: dict[str, Any], fallback: str) -> str:
        task = "天气摘要" if kind == "weather" else "通勤建议"
        return (
            f"请基于以下 JSON 数据整理一份适合聊天框发送的{task}。\n"
            "要求：\n"
            "1. 不要编造 JSON 中没有的天气事实。\n"
            "2. 中文输出，结构清晰，适当使用 emoji，但不要太长。\n"
            "3. 保留关键温度、天气、降水概率/风险、通勤方式或生活建议。\n"
            "4. 如果数据不足，请自然说明。\n\n"
            f"JSON 数据：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            f"模板兜底文案可参考：\n{fallback}"
        )

    def _format_weather(self, payload: dict[str, Any]) -> str:
        day = payload["tomorrow"]
        lines = [f"📍 {payload['location']} · 明日天气", ""]
        lines += [
            f"天气：{day.get('textDay', '-')}/{day.get('textNight', '-')}",
            f"气温：{day.get('tempMin', '-')}℃ - {day.get('tempMax', '-')}℃",
            f"降水概率：{day.get('pop', day.get('precip', '-'))}%",
            f"湿度：{day.get('humidity', '-')}%",
            f"风力：{day.get('windDirDay', '-')} {day.get('windScaleDay', '-')}级",
        ]
        if payload.get("warnings"):
            lines.append("⚠️ 预警：" + "；".join(w.get("title", "天气预警") for w in payload["warnings"][:2]))
        advice = payload["rule_advice"]
        lines += ["", "👕 穿衣建议：" + advice["穿衣"], "☔ 出行建议：" + advice["带伞"], "🏃 生活建议：" + advice["洗车"] + advice["户外"], "", "📅 未来 3 天"]
        for item in payload["next_three_days"]:
            lines.append(f"{item.get('fxDate', '-')}: {item.get('textDay', '-')}/{item.get('textNight', '-')}，{item.get('tempMin', '-')}℃ - {item.get('tempMax', '-')}℃")
        return "\n".join(lines)

    def _format_commute(self, payload: dict[str, Any]) -> str:
        lines = [f"🚦 {payload['scene']}通勤建议", f"日期：{payload['target_date']}", f"可选方式：{payload['commute_methods']}", ""]
        for section in payload["sections"]:
            lines.append(f"{section['name']} {section['time_range']}")
            lines.append(f"路线：{section['origin']} → {section['destination']}")
            if section["weather"]:
                desc = []
                for hour in section["weather"]:
                    dt = self._parse_fx_time(hour.get("fxTime", ""))
                    label = dt.strftime("%H:%M") if dt else hour.get("fxTime", "-")
                    desc.append(f"{label} {hour.get('text', '-')} {hour.get('temp', '-')}℃ 降水{hour.get('pop', '-')}%")
                lines.append("天气：" + "；".join(desc))
            else:
                lines.append("天气：未匹配到该时间段逐小时预报。")
            lines.append("建议：" + section["rule_summary"])
            lines.append("")
        return "\n".join(lines).strip()

    def _missing_commute_config(self) -> list[str]:
        fields = {
            "家地址": "home_location",
            "公司地址": "work_location",
            "上班时间段": "morning_commute_time",
            "下班时间段": "evening_commute_time",
            "可选通勤方式": "commute_methods",
        }
        return [name for name, key in fields.items() if not str(self._conf(key, "")).strip()]

    def _parse_time_range(self, value: str) -> TimeRange:
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", value or "")
        if not match:
            raise ValueError("时间段格式应为 HH:MM-HH:MM，例如 08:00-09:00。")
        h1, m1, h2, m2 = map(int, match.groups())
        return TimeRange(time(h1, m1), time(h2, m2))

    def _validate_commute_ranges(self, morning: TimeRange, evening: TimeRange) -> None:
        if not (morning.start < morning.end < evening.start < evening.end):
            raise ValueError("请确保：上班开始 < 上班结束 < 下班开始 < 下班结束，且不跨天。")

    def _format_range(self, time_range: TimeRange) -> str:
        return f"{time_range.start.strftime('%H:%M')}-{time_range.end.strftime('%H:%M')}"

    def _parse_fx_time(self, value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _to_int(self, value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0
