"""高德地图地理服务：POI 地理编码 + 两点通行时间估算（行程规划师专用）。

设计原则：
- 无 AMAP_API_KEY 或任何调用失败一律返回 None，规划层自动降级为纯 LLM 排线，绝不阻断；
- 数据合规：只上传景点名与酒店文本做查询，不上传任何采集到的内容数据；
- 进程内缓存：同一任务里重复查询（规划阶段会两两算矩阵）不重复消耗配额。
"""
import math

import httpx

from config import AMAP_API_KEY

_BASE = "https://restapi.amap.com/v3"

# 进程内缓存（任务级生命周期足够，无需持久化）
_geo_cache: dict[tuple[str, str], dict | None] = {}
_time_cache: dict[tuple[str, str], tuple[int, str] | None] = {}


def available() -> bool:
    """是否具备高德能力（决定是否走真实通行时间，还是降级纯 LLM）。"""
    return bool(AMAP_API_KEY)


def geocode_poi(name: str, city: str) -> dict | None:
    """关键词搜索 POI：返回 {"name", "location": "lng,lat", "address", "adname"}。

    未配置 Key、无结果或异常都返回 None（调用方按降级处理）。
    """
    if not available():
        return None
    key = (name, city)
    if key in _geo_cache:
        return _geo_cache[key]
    try:
        r = httpx.get(
            f"{_BASE}/place/text",
            params={
                "key": AMAP_API_KEY,
                "keywords": name,
                "city": city,
                "citylimit": "true",
                "offset": 1,
                "extensions": "base",
            },
            timeout=10,
        )
        data = r.json()
        if data.get("status") == "1" and data.get("pois"):
            p = data["pois"][0]
            out = {
                "name": p.get("name") or name,
                "location": p.get("location") or "",
                "address": p.get("address") if isinstance(p.get("address"), str) else "",
                "adname": p.get("adname") if isinstance(p.get("adname"), str) else "",
            }
            if out["location"]:
                _geo_cache[key] = out
                return out
    except Exception:
        pass
    _geo_cache[key] = None
    return None


def distance_km(loc1: str, loc2: str) -> float | None:
    """两个 'lng,lat' 坐标串的球面距离（km）；解析失败返回 None。纯函数，独立可测。"""
    try:
        lng1, lat1 = (float(x) for x in loc1.split(","))
        lng2, lat2 = (float(x) for x in loc2.split(","))
    except (ValueError, AttributeError):
        return None
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def travel_time(origin_loc: str, dest_loc: str, city: str = "") -> tuple[int, str] | None:
    """两点通行时间：直线距离 < 2km 查步行，否则查公交（含换乘）。

    返回 (分钟, 方式描述)，如 (35, "公交")；无 Key / 解析失败 / 无路线返回 None。
    """
    if not available():
        return None
    key = (origin_loc, dest_loc)
    if key in _time_cache:
        return _time_cache[key]
    result: tuple[int, str] | None = None
    d = distance_km(origin_loc, dest_loc)
    try:
        if d is not None and d < 2.0:
            r = httpx.get(
                f"{_BASE}/direction/walking",
                params={"key": AMAP_API_KEY, "origin": origin_loc, "destination": dest_loc},
                timeout=10,
            )
            data = r.json()
            paths = (data.get("route") or {}).get("paths") or []
            if data.get("status") == "1" and paths:
                result = (max(1, round(int(paths[0]["duration"]) / 60)), "步行")
        else:
            r = httpx.get(
                f"{_BASE}/direction/transit/integrated",
                params={
                    "key": AMAP_API_KEY,
                    "origin": origin_loc,
                    "destination": dest_loc,
                    "city": city or "全国",
                    "cityd": city or "全国",
                },
                timeout=10,
            )
            data = r.json()
            transits = (data.get("route") or {}).get("transits") or []
            if data.get("status") == "1" and transits:
                result = (max(1, round(int(transits[0]["duration"]) / 60)), "公交")
    except Exception:
        result = None
    _time_cache[key] = result
    return result
