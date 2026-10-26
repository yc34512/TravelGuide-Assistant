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

from config import (REQUEST_DELAY_MAX, REQUEST_DELAY_MIN, SESSION_DETAIL_BUDGET,
                    SOURCE_DOUYIN_ENABLED)

# 会话级详情页导航预算（测试/运行时可通过 base.NAV_DETAIL_BUDGET 覆盖）
NAV_DETAIL_BUDGET = SESSION_DETAIL_BUDGET


class SourceDisabled(RuntimeError):
    """UGC 数据源未启用（开源默认关闭）。service 层捕获后降级为缓存/LLM 基线或给出友好提示。"""


UGC_ENABLE_NOTICE = (
    "已启用抖音 UGC 数据源（SOURCE_DOUYIN_ENABLED=true）。请确认：仅用于个人/研究用途、"
    "遵守抖音用户协议、不用于商业批量采集；频控为合规底线请勿调低；本项目不含且不支持任何"
    "签名破解/验证码绕过技术。启用即视为使用者自行承担责任（详见 README「合规与免责」）。"
)

_noticed = False          # 免责告知每进程只打印一次
# 两个维度必须分开（本轮 S0 修正）：
# - _session_stopped 属于浏览器/账号会话：验证码一旦触发，跨任务持续有效，
#   只有人工确认风控已解除后显式 clear_account_risk() 才清除；
# - _nav_count 属于当前任务：任务 A 正在采集时，任务 B 的初始化不得把 A 的计数清零。
_session_stopped = False
_nav_count = 0
_active_task = None       # 当前持有导航预算的任务 token；None 表示空闲


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
    """标记浏览器/账号会话触发验证码风控：后续搜索/采集立即返回空，不再发新请求。

    这是**会话级**状态：任务 A 触发后，任务 B 开始也不会自动解除。只有人工确认
    风控已平息（例如等待 20~30 分钟）后调用 clear_account_risk() 才恢复。"""
    global _session_stopped
    _session_stopped = True


def session_stopped() -> bool:
    return _session_stopped


def clear_account_risk() -> None:
    """人工确认风控已解除后，显式清除会话熔断。定时器/新任务不得自动调用。"""
    global _session_stopped
    _session_stopped = False


def bump_navigation() -> int:
    """记一次详情页导航（当前任务预算的唯一计数口，在导航发生后调用）。"""
    global _nav_count
    _nav_count += 1
    return _nav_count


def nav_count() -> int:
    return _nav_count


def nav_budget() -> int:
    return NAV_DETAIL_BUDGET


def nav_budget_exhausted() -> bool:
    """会话级导航预算是否用尽。

    抖音按"同一会话连续 5~6 次视频页导航"判风控，而一次任务会连续跑
    攻略层 / 候选验证 / 逐点调研 / 餐厅调研（同一个登录会话）——所以预算按会话共享：
    用尽即主动停手，走"稍后重跑补齐（命中缓存）"路径，不去撞验证码（撞了会加重风控）。
    """
    return _nav_count >= NAV_DETAIL_BUDGET


def reset_session(task_id=None) -> bool:
    """开始一个任务的导航预算；**不清除**账号会话的验证码熔断。

    由任务入口（research / trip / heatrefresh / CLI）调用，不再由每次采集调用——
    否则一次任务里的逐点采集会各自清零预算，等于变相重试，必然撞上验证码。

    task_id 用于区分并发任务：
    - 当前没有任务持有预算，或就是同一任务：返回 True（新任务从 0 开始）；
    - 另一任务仍持有预算：不重置，返回 False。调用方应等待/拒绝，而不能把
      正在采集的任务 A 的计数清空。没有 task_id 的旧调用（CLI/测试）按显式
      重新开始处理，但仍不清除会话熔断。
    """
    global _nav_count, _active_task
    token = None if task_id is None else str(task_id)
    if token is None:
        _nav_count = 0
        _active_task = "__anonymous__"
        return True
    if _active_task is not None and _active_task != token:
        return False
    if _active_task is None:
        _nav_count = 0
    _active_task = token
    return True


def end_task(task_id=None) -> None:
    """任务收口：释放导航预算所有权。不重置计数、不清除会话熔断。"""
    global _active_task
    if _active_task is None:
        return
    if task_id is None or _active_task == str(task_id):
        _active_task = None


def active_task() -> str | None:
    return _active_task


def session_state() -> dict:
    """可观测状态：任务预算与账号会话熔断分开报告。"""
    return {"task_id": _active_task, "nav_count": _nav_count,
            "nav_budget": NAV_DETAIL_BUDGET, "budget_exhausted": nav_budget_exhausted(),
            "account_stopped": _session_stopped}
