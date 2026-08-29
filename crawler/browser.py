"""浏览器会话管理：DrissionPage 启动/接管 Chromium 内核浏览器（Chrome 或 Edge）。

合规要点：
- 以真实浏览器、普通用户身份访问，只读取登录后页面上可见的内容；
- 不做签名破解、不做任何风控绕过；
- 登录态保存在项目内独立的 browser_profile 目录，请始终使用采集专用小号。
"""
import os
import time

from DrissionPage import ChromiumPage, ChromiumOptions

from config import BROWSER_PROFILE_DIR, HEADLESS

_BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _pick_browser() -> str | None:
    for path in _BROWSER_CANDIDATES:
        if os.path.exists(path):
            return path
    return None  # 交给 DrissionPage 自动查找


def create_page() -> ChromiumPage:
    """启动浏览器。固定端口 + 独立用户数据目录，保证扫码一次、登录态长期有效。"""
    co = ChromiumOptions()
    browser = _pick_browser()
    if browser:
        co.set_browser_path(browser)
    co.set_user_data_path(str(BROWSER_PROFILE_DIR))
    # 固定调试端口。注意不能用 auto_port()——它会为每次启动分配临时用户目录，
    # 覆盖上面的登录态保存目录，导致每次运行都要重新扫码。
    co.set_local_port(9333)
    if HEADLESS:
        # 不推荐：首次扫码登录需要可见窗口
        co.headless()
    return ChromiumPage(co)


def is_logged_in(page: ChromiumPage) -> bool:
    """以 sessionid cookie 是否存在判断登录态（需当前页面在抖音域名下）。"""
    try:
        return any(c.get("name") == "sessionid" for c in page.cookies())
    except Exception:
        return False


def ensure_login(page: ChromiumPage, timeout: int = 300) -> bool:
    """确保抖音登录态。首次运行时打印提示，等待用户在浏览器窗口扫码。"""
    if is_logged_in(page):
        return True
    page.get("https://www.douyin.com")
    if is_logged_in(page):
        return True

    print(f"\n>>> 未检测到抖音登录态。请在弹出的浏览器窗口中用【采集专用小号】扫码登录（最长等待 {timeout} 秒）…")
    print(">>> 若页面没有自动弹出登录框，请手动点击右上角\"登录\"。")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        if is_logged_in(page):
            print(">>> 登录成功，登录态已保存到 browser_profile/，下次运行无需重复扫码。")
            return True
    return False
