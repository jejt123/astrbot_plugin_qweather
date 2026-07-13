from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import jwt

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
    def __init__(
        self,
        api_key: str,
        api_host: str,
        timeout: int = 10,
        jwt_project_id: str = "",
        jwt_key_id: str = "",
        jwt_private_key: str = "",
        jwt_ttl_seconds: int = 900,
        log_level: str = "info",
    ):
        self.api_key = api_key.strip()
        self.api_host = api_host.rstrip("/").strip() or "https://devapi.qweather.com"
        self.timeout = max(1, timeout)
        self.jwt_project_id = jwt_project_id.strip()
        self.jwt_key_id = jwt_key_id.strip()
        self.jwt_private_key = self._normalize_private_key(jwt_private_key)
        self.jwt_ttl_seconds = max(60, min(int(jwt_ttl_seconds or 900), 86400))
        self._jwt_token = ""
        self._jwt_expires_at = 0
        self.log_level = self._normalize_log_level(log_level)
        self._location_cache: dict[str, str] = {}
        self._location_detail_cache: dict[str, dict[str, Any]] = {}

    async def lookup_location(self, keyword: str) -> str:
        location = await self.lookup_location_detail(keyword)
        location_id = str(location.get("id") or "").strip()
        if not location_id:
            raise QWeatherError(f"和风天气未返回有效位置 ID：{keyword}")
        return location_id

    async def lookup_location_detail(self, keyword: str) -> dict[str, Any]:
        keyword = keyword.strip()
        if not keyword:
            raise QWeatherError("城市或地址不能为空。")
        if keyword in self._location_detail_cache:
            return self._location_detail_cache[keyword]
        payload = await self._get_json("/geo/v2/city/lookup", {"location": keyword, "range": "cn"})
        locations = payload.get("location") or []
        if not locations:
            raise QWeatherError(f"未找到位置：{keyword}")
        location = locations[0]
        self._location_detail_cache[keyword] = location
        if location_id := str(location.get("id") or "").strip():
            self._location_cache[keyword] = location_id
        return location

    async def resolve_weather_location(self, keyword: str) -> str:
        coordinate = self._normalize_coordinate(keyword)
        if coordinate:
            return coordinate
        return await self.lookup_location(keyword)

    async def daily_weather(self, keyword: str) -> list[dict[str, Any]]:
        location = await self.resolve_weather_location(keyword)
        payload = await self._get_json("/v7/weather/3d", {"location": location})
        return payload.get("daily") or []

    async def hourly_weather(self, keyword: str) -> list[dict[str, Any]]:
        location = await self.resolve_weather_location(keyword)
        payload = await self._get_json("/v7/weather/24h", {"location": location})
        return payload.get("hourly") or []

    async def weather_warning(self, keyword: str) -> list[dict[str, Any]]:
        location = await self.resolve_weather_location(keyword)
        payload = await self._get_json("/v7/warning/now", {"location": location})
        return payload.get("warning") or []

    async def current_weather_alert(self, latitude: str, longitude: str) -> dict[str, Any]:
        return await self._get_json(f"/weatheralert/v1/current/{latitude}/{longitude}", {"localTime": "true"})

    async def resolve_alert_coordinate(self, keyword: str) -> tuple[str, str]:
        coordinate = self._normalize_coordinate(keyword)
        if coordinate:
            longitude, latitude = coordinate.split(",", 1)
            return latitude, longitude
        location = await self.lookup_location_detail(keyword)
        longitude = str(location.get("lon") or "").strip()
        latitude = str(location.get("lat") or "").strip()
        if not longitude or not latitude:
            raise QWeatherError(f"未能解析预警查询所需的经纬度：{keyword}")
        return latitude, longitude

    def _normalize_coordinate(self, value: str) -> str | None:
        match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", value or "")
        if not match:
            return None
        longitude = float(match.group(1))
        latitude = float(match.group(2))
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise QWeatherError("经纬度格式应为：经度,纬度，例如 116.41,39.92。")
        return f"{self._format_coordinate(longitude)},{self._format_coordinate(latitude)}"

    def _format_coordinate(self, value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".")

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers: dict[str, str] = {}
        auth_mode = self._auth_mode()
        if auth_mode == "jwt":
            headers["Authorization"] = f"Bearer {self._get_jwt_token()}"
            query = {**params, "lang": "zh", "unit": "m"}
        else:
            query = {**params, "key": self.api_key, "lang": "zh", "unit": "m"}
        url = f"{self.api_host}{path}?{urllib.parse.urlencode(query)}"
        self._log_info(f"QWeather request: auth_mode={auth_mode}, path={path}")
        self._log_debug("QWeather request: " + self._dump_log_data({"auth_mode": auth_mode, "method": "GET", "url": self._sanitize_url(url), "path": path, "params": self._sanitize_payload(query)}))
        return await asyncio.to_thread(self._blocking_get_json, url, headers)

    def _blocking_get_json(self, url: str, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        headers = {
            "User-Agent": "astrbot-plugin-qweather/0.1",
            "Accept-Encoding": "gzip, identity",
        }
        headers.update(extra_headers or {})
        request = urllib.request.Request(
            url,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(self._decode_response_body(response.read(), response.headers))
                self._log_info(f"QWeather response: code={data.get('code')}, url={self._sanitize_url(url)}")
                self._log_debug("QWeather response: " + self._dump_log_data({"url": self._sanitize_url(url), "status": getattr(response, "status", None), "data": self._sanitize_payload(data)}))
        except urllib.error.HTTPError as exc:
            self._log_warn(f"QWeather HTTP error: status={exc.code}, url={self._sanitize_url(url)}")
            raise QWeatherError(f"和风天气 HTTP 错误：{exc.code}") from exc
        except urllib.error.URLError as exc:
            self._log_warn(f"QWeather request failed: reason={exc.reason}, url={self._sanitize_url(url)}")
            raise QWeatherError(f"和风天气请求失败：{exc.reason}") from exc
        except UnicodeDecodeError as exc:
            raise QWeatherError("和风天气返回了无法解码的数据。") from exc
        except json.JSONDecodeError as exc:
            raise QWeatherError("和风天气返回了无法解析的数据。") from exc
        code = str(data.get("code", ""))
        if code and code != "200":
            self._log_warn(f"QWeather API error code: code={code}, url={self._sanitize_url(url)}, data={self._dump_log_data(self._sanitize_payload(data))}")
            raise QWeatherError(f"和风天气返回错误码：{code}")
        return data

    def _decode_response_body(self, body: bytes, headers: Any) -> str:
        encoding = ""
        if headers:
            encoding = str(headers.get("Content-Encoding", "")).lower()
        if "gzip" in encoding or body.startswith(b"\x1f\x8b"):
            body = gzip.decompress(body)
        return body.decode("utf-8")

    def _auth_mode(self) -> str:
        jwt_values = [self.jwt_project_id, self.jwt_key_id, self.jwt_private_key]
        if all(jwt_values):
            return "jwt"
        if any(jwt_values):
            raise QWeatherError("JWT 配置不完整，请同时填写 Project ID、Credential ID 和 Private Key；或清空 JWT 配置以使用 API Key。")
        if self.api_key:
            return "api_key"
        raise QWeatherError("尚未在插件配置页面填写和风天气 JWT 或 API Key。")

    def _get_jwt_token(self) -> str:
        now = int(time_module.time())
        if self._jwt_token and now < self._jwt_expires_at - 60:
            return self._jwt_token
        expires_at = now + self.jwt_ttl_seconds
        try:
            token = jwt.encode(
                {"sub": self.jwt_project_id, "iat": now, "exp": expires_at},
                self.jwt_private_key,
                algorithm="EdDSA",
                headers={"kid": self.jwt_key_id},
            )
        except Exception as exc:
            raise QWeatherError(f"JWT 生成失败，请检查 Project ID、Credential ID 和 Private Key：{exc}") from exc
        self._jwt_token = token if isinstance(token, str) else token.decode("utf-8")
        self._jwt_expires_at = expires_at
        return self._jwt_token

    def _normalize_private_key(self, value: str) -> str:
        return (value or "").strip().replace("\\n", "\n")

    def _normalize_log_level(self, value: str) -> str:
        level = (value or "info").strip().lower()
        return level if level in {"debug", "info", "warn"} else "info"

    def _should_log(self, level: str) -> bool:
        order = {"debug": 10, "info": 20, "warn": 30}
        return order[level] >= order[self.log_level]

    def _log_debug(self, message: str) -> None:
        if self._should_log("debug"):
            logger.debug(message)

    def _log_info(self, message: str) -> None:
        if self._should_log("info"):
            logger.info(message)

    def _log_warn(self, message: str) -> None:
        if self._should_log("warn"):
            logger.warning(message)

    def _sanitize_url(self, url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        safe_pairs = [(key, "***" if self._is_sensitive_key(key) else value) for key, value in query]
        safe_query = urllib.parse.urlencode(safe_pairs)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, safe_query, parsed.fragment))

    def _sanitize_payload(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ("***" if self._is_sensitive_key(str(key)) else self._sanitize_payload(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize_payload(item) for item in value]
        return value

    def _is_sensitive_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(word in lowered for word in ["key", "token", "authorization", "jwt", "private", "secret"])

    def _dump_log_data(self, value: Any) -> str:
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= 2000 else text[:2000] + "...<truncated>"


@register("astrbot_plugin_qweather", "OpenAI", "和风天气通勤建议插件", "0.1.6")
class QWeatherPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = QWeatherClient(
            api_key=self._conf("qweather_api_key"),
            api_host=self._conf("qweather_api_host", "https://devapi.qweather.com"),
            timeout=int(self._conf("request_timeout_seconds", 10) or 10),
            jwt_project_id=self._conf("qweather_jwt_project_id"),
            jwt_key_id=self._conf("qweather_jwt_key_id"),
            jwt_private_key=self._conf("qweather_jwt_private_key"),
            jwt_ttl_seconds=int(self._conf("qweather_jwt_ttl_seconds", 900) or 900),
            log_level=self._conf("qweather_log_level", "info"),
        )
        self.warning_response_cache_minutes = max(1, int(self._conf("warning_response_cache_minutes", 10) or 10))
        self.warning_state_ttl_hours = 48
        self.warning_state_path = Path(__file__).with_name("warning_state.json")

    @filter.command("天气")
    async def weather(self, event: AstrMessageEvent, city: str | None = None):
        """按当前时间自动查询今日/明日天气、天气简报、穿衣和生活建议。"""
        try:
            location = (city or self._conf("default_city")).strip()
            if not location:
                yield event.plain_result("请在插件配置页面设置默认城市，或使用：/天气 北京")
                return
            daily = await self.client.daily_weather(location)
            warnings = await self._safe_warnings(location)
            switch_time = self._parse_time_of_day(self._conf("weather_day_switch_time", "18:00"))
            draft = self._build_weather_payload(location, daily, warnings, datetime.now(), switch_time)
            fallback = self._format_weather(draft)
            text = await self._polish_with_model(event, "weather", draft, fallback)
            yield event.plain_result(text)
        except ValueError as exc:
            yield event.plain_result(f"天气日期切换时间配置有误：{exc}")
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
            "   根据插件配置的日期切换时间，查询指定城市今日或明日详细天气、天气简报、穿衣和生活建议。\n"
            "   示例：/天气 北京\n\n"
            "2. /天气\n"
            "   查询插件配置页面中的默认城市。\n\n"
            "3. /通勤\n"
            "   根据当前时间自动判断输出今日/明日上班、下班天气和交通方式建议。\n\n"
            "4. /预警\n"
            "   手动查询当前生效天气预警；没有预警时也会回复。\n\n"
            "5. /预警检查\n"
            "   定时检查入口，仅在有新增或更新预警时输出。\n\n"
            "需要先在插件配置页面填写：和风天气 API Key、API Host、默认城市、家地址、公司地址、"
            "上下班时间段、可选通勤方式。"
        )

    @filter.command("预警")
    async def warning(self, event: AstrMessageEvent):
        """手动查询当前生效天气预警。"""
        try:
            state = self._load_warning_state()
            self._cleanup_warning_state(state)
            result = await self._collect_warning_alerts(state, force_refresh=False)
            self._save_warning_state(state)
            yield event.plain_result(self._format_warning_query_result(result))
        except Exception as exc:
            logger.exception("/预警 执行失败")
            yield event.plain_result(f"天气预警查询失败：{exc}")

    @filter.command("预警检查")
    async def warning_check(self, event: AstrMessageEvent):
        """定时插件使用：仅在有新增或更新预警时输出。"""
        try:
            state = self._load_warning_state()
            self._cleanup_warning_state(state)
            result = await self._collect_warning_alerts(state, force_refresh=False)
            new_alerts = self._find_new_warning_alerts(result["alerts"], state)
            if not new_alerts:
                self._save_warning_state(state)
                return
            self._mark_warning_alerts_seen(new_alerts, state)
            self._save_warning_state(state)
            yield event.plain_result(self._format_warning_check_result(new_alerts, result.get("attributions", [])))
        except Exception as exc:
            logger.warning(f"/预警检查 执行失败，已静默忽略：{exc}")

    def _conf(self, key: str, default: Any = "") -> Any:
        try:
            return self.config.get(key, default)
        except AttributeError:
            return self.config[key] if key in self.config else default

    async def _collect_warning_alerts(self, state: dict[str, Any], force_refresh: bool = False) -> dict[str, Any]:
        locations = self._warning_locations()
        if not locations:
            raise QWeatherError("请先在插件配置页面设置默认城市、家地址或公司地址，用于查询天气预警。")

        now = datetime.now()
        all_alerts: dict[str, dict[str, Any]] = {}
        attributions: list[dict[str, Any]] = []
        checked_locations: list[str] = []
        failures: list[str] = []

        for label, location in locations:
            try:
                latitude, longitude = await self.client.resolve_alert_coordinate(location)
                cache_key = f"{latitude},{longitude}"
                payload = self._get_warning_cached_response(state, cache_key, now)
                if force_refresh or payload is None:
                    payload = await self.client.current_weather_alert(latitude, longitude)
                    self._set_warning_cached_response(state, cache_key, payload, now)
                checked_locations.append(label)
                attributions.extend(payload.get("metadata", {}).get("attributions") or [])
                for alert in payload.get("alerts") or []:
                    normalized = self._normalize_warning_alert(alert, label)
                    key = normalized["dedupe_key"]
                    if key not in all_alerts:
                        all_alerts[key] = normalized
                    elif label not in all_alerts[key]["locations"]:
                        all_alerts[key]["locations"].append(label)
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                logger.warning(f"天气预警位置查询失败：{label}，{exc}")

        return {
            "alerts": list(all_alerts.values()),
            "attributions": self._unique_attributions(attributions),
            "checked_locations": checked_locations,
            "failures": failures,
        }

    def _warning_locations(self) -> list[tuple[str, str]]:
        candidates = [
            ("家", str(self._conf("home_coordinates") or "").strip() or str(self._conf("home_location") or "").strip()),
            ("公司", str(self._conf("work_coordinates") or "").strip() or str(self._conf("work_location") or "").strip()),
            ("默认城市", str(self._conf("default_city") or "").strip()),
        ]
        locations: list[tuple[str, str]] = []
        seen: set[str] = set()
        for label, location in candidates:
            if not location or location in seen:
                continue
            locations.append((label, location))
            seen.add(location)
        return locations

    def _load_warning_state(self) -> dict[str, Any]:
        if not self.warning_state_path.exists():
            return {"response_cache": {}, "seen_alerts": {}}
        try:
            data = json.loads(self.warning_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"response_cache": {}, "seen_alerts": {}}
        return {
            "response_cache": data.get("response_cache") or {},
            "seen_alerts": data.get("seen_alerts") or {},
        }

    def _save_warning_state(self, state: dict[str, Any]) -> None:
        self.warning_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _cleanup_warning_state(self, state: dict[str, Any]) -> None:
        now = datetime.now()
        cache = state.setdefault("response_cache", {})
        for key, item in list(cache.items()):
            expires_at = self._parse_datetime(item.get("expires_at"))
            if not expires_at or expires_at <= now:
                cache.pop(key, None)

        seen_alerts = state.setdefault("seen_alerts", {})
        cutoff = now - timedelta(hours=self.warning_state_ttl_hours)
        for key, item in list(seen_alerts.items()):
            keep_until = self._parse_datetime(item.get("keep_until"))
            last_notified = self._parse_datetime(item.get("last_notified"))
            if (keep_until and keep_until <= now) or (not keep_until and (not last_notified or last_notified <= cutoff)):
                seen_alerts.pop(key, None)

    def _get_warning_cached_response(self, state: dict[str, Any], cache_key: str, now: datetime) -> dict[str, Any] | None:
        item = state.setdefault("response_cache", {}).get(cache_key)
        if not item:
            return None
        expires_at = self._parse_datetime(item.get("expires_at"))
        if not expires_at or expires_at <= now:
            state["response_cache"].pop(cache_key, None)
            return None
        return item.get("payload")

    def _set_warning_cached_response(self, state: dict[str, Any], cache_key: str, payload: dict[str, Any], now: datetime) -> None:
        expires_at = now + timedelta(minutes=self.warning_response_cache_minutes)
        state.setdefault("response_cache", {})[cache_key] = {
            "fetched_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "payload": payload,
        }

    def _normalize_warning_alert(self, alert: dict[str, Any], location_label: str) -> dict[str, Any]:
        alert_id = str(alert.get("id") or "").strip()
        headline = str(alert.get("headline") or alert.get("title") or "天气预警").strip()
        fingerprint = self._warning_fingerprint(alert)
        dedupe_key = alert_id or fingerprint
        return {
            "dedupe_key": dedupe_key,
            "fingerprint": fingerprint,
            "id": alert_id,
            "headline": headline,
            "sender": alert.get("senderName") or alert.get("sender") or "",
            "issued_time": alert.get("issuedTime") or "",
            "expire_time": alert.get("expireTime") or "",
            "severity": self._warning_named_value(alert.get("severity")),
            "color": self._warning_named_value(alert.get("color")),
            "event": self._warning_named_value(alert.get("eventType")),
            "message_type": self._warning_named_value(alert.get("messageType")),
            "description": alert.get("description") or "",
            "instruction": alert.get("instruction") or "",
            "locations": [location_label],
        }

    def _warning_fingerprint(self, alert: dict[str, Any]) -> str:
        source = json.dumps(
            {
                "headline": alert.get("headline") or alert.get("title"),
                "messageType": alert.get("messageType"),
                "severity": alert.get("severity"),
                "color": alert.get("color"),
                "issuedTime": alert.get("issuedTime"),
                "expireTime": alert.get("expireTime"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _warning_named_value(self, value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("name") or value.get("code") or value.get("text") or "").strip()
        return str(value or "").strip()

    def _find_new_warning_alerts(self, alerts: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
        seen = state.setdefault("seen_alerts", {})
        new_alerts = []
        for alert in alerts:
            previous = seen.get(alert["dedupe_key"])
            if not previous or previous.get("fingerprint") != alert["fingerprint"]:
                new_alerts.append(alert)
        return new_alerts

    def _mark_warning_alerts_seen(self, alerts: list[dict[str, Any]], state: dict[str, Any]) -> None:
        now = datetime.now()
        seen = state.setdefault("seen_alerts", {})
        for alert in alerts:
            expire_time = self._parse_datetime(alert.get("expire_time"))
            keep_until = expire_time + timedelta(hours=24) if expire_time else now + timedelta(hours=self.warning_state_ttl_hours)
            seen[alert["dedupe_key"]] = {
                "fingerprint": alert["fingerprint"],
                "headline": alert["headline"],
                "last_notified": now.isoformat(),
                "expire_time": alert.get("expire_time") or "",
                "keep_until": keep_until.isoformat(),
            }

    def _format_warning_query_result(self, result: dict[str, Any]) -> str:
        alerts = result.get("alerts") or []
        if not alerts:
            if result.get("failures") and not result.get("checked_locations"):
                return "天气预警查询失败：" + "；".join(result["failures"])
            lines = ["✅ 当前暂无生效天气预警"]
            if result.get("checked_locations"):
                lines.append("查询位置：" + "、".join(result["checked_locations"]))
            if result.get("failures"):
                lines.append("部分位置查询失败：" + "；".join(result["failures"]))
            return "\n".join(lines)
        return self._format_warning_alerts("⚠️ 当前生效天气预警", alerts, result.get("attributions", []), include_details=True)

    def _format_warning_check_result(self, alerts: list[dict[str, Any]], attributions: list[dict[str, Any]]) -> str:
        return self._format_warning_alerts("⚠️ 新增或更新天气预警", alerts, attributions, include_details=False)

    def _format_warning_alerts(self, title: str, alerts: list[dict[str, Any]], attributions: list[dict[str, Any]], include_details: bool) -> str:
        lines = [title, ""]
        for index, alert in enumerate(alerts, 1):
            lines.append(f"{index}. {alert['headline']}")
            if alert.get("locations"):
                lines.append("影响位置：" + "、".join(alert["locations"]))
            if alert.get("sender"):
                lines.append(f"发布单位：{alert['sender']}")
            if alert.get("issued_time"):
                lines.append(f"发布时间：{alert['issued_time']}")
            if alert.get("expire_time"):
                lines.append(f"失效时间：{alert['expire_time']}")
            level_parts = [part for part in [alert.get("severity"), alert.get("color"), alert.get("event")] if part]
            if level_parts:
                lines.append("级别/类型：" + " / ".join(level_parts))
            if include_details and alert.get("description"):
                lines.append("说明：" + self._compact_text(alert["description"], 160))
            if include_details and alert.get("instruction"):
                lines.append("建议：" + self._compact_text(alert["instruction"], 160))
            lines.append("")
        attribution_text = self._format_attributions(attributions)
        if attribution_text:
            lines.append(attribution_text)
        return "\n".join(lines).strip()

    def _compact_text(self, value: str, limit: int) -> str:
        text = re.sub(r"\s+", " ", value or "").strip()
        return text if len(text) <= limit else text[:limit] + "..."

    def _unique_attributions(self, attributions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique = []
        seen = set()
        for item in attributions:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def _format_attributions(self, attributions: list[dict[str, Any]]) -> str:
        names = []
        for item in attributions:
            name = item.get("name") or item.get("title") or item.get("url")
            if name:
                names.append(str(name))
        if not names:
            return ""
        return "数据来源：" + "、".join(dict.fromkeys(names))

    def _parse_datetime(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    async def _safe_warnings(self, location: str) -> list[dict[str, Any]]:
        try:
            return await self.client.weather_warning(location)
        except Exception as exc:
            logger.warning(f"天气预警查询失败，已忽略：{exc}")
            return []

    def _build_weather_payload(
        self,
        location: str,
        daily: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        now: datetime,
        switch_time: time,
    ) -> dict[str, Any]:
        if not daily:
            raise QWeatherError("和风天气没有返回未来天气数据。")
        target_index = 1 if now.time().replace(second=0, microsecond=0) >= switch_time else 0
        if target_index >= len(daily):
            target_index = len(daily) - 1
        target_day = daily[target_index]
        summary_days = daily[target_index:target_index + 3]
        target_label = "明日天气" if target_index == 1 else "今日天气"
        summary_label = "未来天气简报" if target_index == 1 else "最近 3 天天气"
        return {
            "location": location,
            "target": target_label,
            "target_date": target_day.get("fxDate", ""),
            "target_day": target_day,
            "summary_label": summary_label,
            "next_three_days": summary_days,
            "warnings": warnings,
            "rule_advice": self._weather_rule_advice(target_day, summary_days, warnings),
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
        home_weather_location = str(self._conf("home_coordinates") or "").strip() or home
        work_weather_location = str(self._conf("work_coordinates") or "").strip() or work
        sections: list[dict[str, Any]] = []
        if scene.include_morning:
            sections.append(
                await self._build_commute_section(
                    "上班",
                    home,
                    work,
                    home_weather_location,
                    scene.target_date,
                    morning,
                )
            )
        if scene.include_evening:
            sections.append(
                await self._build_commute_section(
                    "下班",
                    work,
                    home,
                    work_weather_location,
                    scene.target_date,
                    evening,
                )
            )
        return {
            "scene": scene.title,
            "target_date": scene.target_date.isoformat(),
            "home_location": home,
            "work_location": work,
            "home_weather_location": home_weather_location,
            "work_weather_location": work_weather_location,
            "commute_methods": self._conf("commute_methods"),
            "sections": sections,
        }

    async def _build_commute_section(
        self,
        name: str,
        origin: str,
        destination: str,
        weather_location: str,
        target_date: date,
        time_range: TimeRange,
    ) -> dict[str, Any]:
        hourly = await self.client.hourly_weather(weather_location)
        matched = self._match_hourly(hourly, target_date, time_range)
        return {
            "name": name,
            "origin": origin,
            "destination": destination,
            "weather_location": weather_location,
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
        day = payload["target_day"]
        lines = [f"📍 {payload['location']} · {payload['target']}", ""]
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
        lines += ["", "👕 穿衣建议：" + advice["穿衣"], "☔ 出行建议：" + advice["带伞"], "🏃 生活建议：" + advice["洗车"] + advice["户外"], "", f"📅 {payload['summary_label']}"]
        for item in payload["next_three_days"]:
            lines.append(f"{item.get('fxDate', '-')}: {item.get('textDay', '-')}/{item.get('textNight', '-')}，{item.get('tempMin', '-')}℃ - {item.get('tempMax', '-')}℃")
        return "\n".join(lines)

    def _format_commute(self, payload: dict[str, Any]) -> str:
        lines = [f"🚦 {payload['scene']}通勤建议", f"日期：{payload['target_date']}", f"可选方式：{payload['commute_methods']}", ""]
        for section in payload["sections"]:
            lines.append(f"{section['name']} {section['time_range']}")
            lines.append(f"路线：{section['origin']} → {section['destination']}")
            if section.get("weather_location") and section["weather_location"] != section["origin"]:
                lines.append(f"天气参考位置：{section['weather_location']}")
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

    def _parse_time_of_day(self, value: str) -> time:
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
        if not match:
            raise ValueError("时间格式应为 HH:MM，例如 18:00。")
        hour, minute = map(int, match.groups())
        return time(hour, minute)

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
