"""本地语音转写（faster-whisper）：视频下载 -> 转文字 -> 即删文件。

合规约束：视频文件仅用于本地分析转写，转写完成立即删除，不存储、不分发；
模型在本地推理，音频数据不出本机。

镜像与协议说明：模型下载走 hf-mirror 并禁用 Xet 协议（国内网络实测可用）。
"""
import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import time
from pathlib import Path

from config import ASR_MODEL_SIZE, DATA_DIR

_model = None  # 进程内懒加载，避免每次调用都载入模型


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        _model = WhisperModel(ASR_MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


def _download(url: str, dest_dir: Path) -> Path:
    import httpx

    path = dest_dir / f"tmp_video_{int(time.time() * 1000)}.mp4"
    with httpx.stream(
        "GET",
        url,
        headers={"Referer": "https://www.douyin.com/", "User-Agent": "Mozilla/5.0"},
        timeout=120,
        follow_redirects=True,
    ) as resp:
        resp.raise_for_status()
        expected = int(resp.headers.get("content-length") or 0)
        with open(path, "wb") as f:
            for chunk in resp.iter_bytes(256 * 1024):
                f.write(chunk)
    size = path.stat().st_size
    if expected and size != expected:
        # CDN 连接不稳定时会静默截断：不完整的 MP4 无法解码，必须重下
        path.unlink(missing_ok=True)
        raise IOError(f"下载不完整：{size}/{expected} 字节")
    return path


def _is_decodable(video_path: Path) -> bool:
    """快速校验：MP4 魔数 + 至少含一条音频/视频流（截断文件会解码不出任何流）。"""
    try:
        if b"ftyp" not in video_path.read_bytes()[:12]:
            return False
        import av

        with av.open(str(video_path)) as container:
            return len(container.streams.audio) > 0 or len(container.streams.video) > 0
    except Exception:
        return False


def transcribe_file(video_path: Path) -> str:
    segments, _info = _get_model().transcribe(str(video_path), language="zh", vad_filter=True)
    return "".join(s.text.strip() for s in segments)


def transcribe_from_url(url: str, retries: int = 3) -> str | None:
    """下载 -> 校验 -> 转写 -> 删除临时文件。CDN 截断自动重下，最终失败返回 None。"""
    tmp_dir = DATA_DIR / "asr_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path: Path | None = None
    try:
        for attempt in range(retries):
            try:
                path = _download(url, tmp_dir)
                if not _is_decodable(path):
                    raise IOError("视频文件无法解码（疑似截断）")
                text = transcribe_file(path)
                if text:
                    return text
                raise IOError("转写结果为空")
            except Exception:
                if path is not None:
                    path.unlink(missing_ok=True)
                    path = None
                if attempt == retries - 1:
                    return None
                time.sleep(2 * (attempt + 1))
        return None
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
