"""数据源闸门与合规护栏（开源合规架构核心）。

五支柱设计：
1. 平台中立的分析内核：pipeline/* 只消费统一模型（VideoItem/Comment/InfoPoint），不感知数据来源；
2. UGC 采集源（抖音）插件化 + 默认关闭：opt-in，使用者自担合规责任（见 UGC_ENABLE_NOTICE）；
3. 合规护栏默认开启：频控/去媒体/脱敏为底线，启用 UGC 源前强制校验（assert_guardrails_for_ugc）；
4. 规范免责声明：启用时一次性告知（require_ugc_source(log=...) 打印 UGC_ENABLE_NOTICE）；
5. 绝不碰绕过技术：captcha_detected 只用于"发现风控→停止采集"，不做任何验证码/签名绕过。

本地知识库缓存（已采过的数据）不受闸门限制：关闭源 = 不发起新的平台采集，仍可读取旧缓存。
"""
from __future__ import annotations

from config import REQUEST_DELAY_MAX, REQUEST_DELAY_MIN, SOURCE_DOUYIN_ENABLED


class SourceDisabled(RuntimeError):
    """UGC 数据源未启用（开源默认关闭）。service 层捕获后降级为缓存/LLM 基线或给出友好提示。"""


UGC_ENABLE_NOTICE = (
    "已启用抖音 UGC 数据源（SOURCE_DOUYIN_ENABLED=true）。请确认：仅用于个人/研究用途、"
    "遵守抖音用户协议、不用于商业批量采集；频控为合规底线请勿调低；本项目不含且不支持任何"
    "签名破解/验证码绕过技术。启用即视为使用者自行承担责任（详见 README「合规与免责」）。"
)

_noticed = False          # 免责告知每进程只打印一次
_session_stopped = False  # 本会话是否已触发验证码风控（触发后停止一切现采）


def douyin_enabled() -> bool:
    """抖音 UGC 适配器是否启用（默认 False）。"""
    return SOURCE_DOUYIN_ENABLED


def require_ugc_source(log=None) -> None:
    """采集入口闸门。未启用抛 SourceDisabled（附启用指引）；启用则校验护栏并打印一次免责告知。"""
    if not SOURCE_DOUYIN_ENABLED:
        raise SourceDisabled(
            "抖音 UGC 数据源默认关闭（开源合规设计）。如需启用：在 .env 设置 SOURCE_DOUYIN_ENABLED=true，"
            "并阅读 README「合规与免责」——启用即表示仅个人/研究用途、遵守平台 ToS、使用者自担责任。"
            "未启用时不发起任何平台采集，行程/攻略走缓存 + LLM 基线（kernel-only）。"
        )
    assert_guardrails_for_ugc()
    global _noticed
    if log is not None and not _noticed:
        log("ℹ️ " + UGC_ENABLE_NOTICE)
        _noticed = True


def assert_guardrails_for_ugc() -> None:
    """启用 UGC 源前的合规校验：频控不得被禁用（合规底线），否则拒绝启用。"""
    if REQUEST_DELAY_MIN <= 0 and REQUEST_DELAY_MAX <= 0:
        raise RuntimeError(
            "合规护栏：启用抖音数据源时频控不可为 0（REQUEST_DELAY_MIN/MAX 均≤0）。"
            "频控是平台风控与合规底线，请恢复默认（2.5~5.0 秒）后再启用。"
        )


# ---- 验证码风控：只检测 + 停止，绝不绕过 ----

def captcha_detected(page) -> bool:
    """检测抖音验证码中间页（title 含"验证码"或页面含 TTGCaptcha）。仅用于停止采集。"""
    try:
        if "验证码" in (page.title or ""):
            return True
        return ("TTGCaptcha" in (page.html or "")) or ("验证码中间页" in (page.html or ""))
    except Exception:
        return False


def stop_session() -> None:
    """标记本会话触发验证码风控：后续搜索/采集立即返回空，不再发新请求（避免加重风控）。"""
    global _session_stopped
    _session_stopped = True


def session_stopped() -> bool:
    return _session_stopped


def reset_session() -> None:
    """新浏览器会话开始时清零风控停止标记（每次 _crawl 持有独立会话）。"""
    global _session_stopped
    _session_stopped = False
