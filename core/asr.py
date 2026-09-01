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


def _audio_ok(video_path: Path) -> bool:
    """ASR 前提校验：MP4 魔数 + 确实含有音轨（抖音音视频分离，纯视频流不能转写；
    截断损坏的文件也解码不出音轨）。"""
    try:
        if b"ftyp" not in video_path.read_bytes()[:12]:
            return False
        import av

        with av.open(str(video_path)) as container:
            return len(container.streams.audio) > 0
    except Exception:
        return False


def transcribe_file(video_path: Path) -> str:
    # 注意：不要改用 BatchedInferencePipeline——实测 CPU int8 下 batched 反而病态卡死
    # （5 分钟音频 >10 分钟无输出），逐段推理约 2 分钟稳定完成。提速靠 transcribe_items 并行。
    segments, _info = _get_model().transcribe(str(video_path), language="zh", vad_filter=True)
    return "".join(s.text.strip() for s in segments)


def _attempt(url: str, tmp_dir: Path) -> str:
    """单个地址：下载 -> 音轨校验 -> 转写 -> 即删。失败抛异常。"""
    path = _download(url, tmp_dir)
    try:
        if not _audio_ok(path):
            raise IOError("该地址不含音轨（纯视频流，换下一个地址）")
        text = transcribe_file(path)
        if not text:
            raise IOError("转写结果为空")
        return text
    finally:
        path.unlink(missing_ok=True)


def _cleanup_stale_tmp(max_age_hours: float = 1.0) -> None:
    """清理异常退出遗留的临时视频文件（正常流程即用即删，这是兜底）。"""
    try:
        tmp_dir = DATA_DIR / "asr_tmp"
        if not tmp_dir.exists():
            return
        cutoff = time.time() - max_age_hours * 3600
        for p in tmp_dir.glob("tmp_video_*.mp4"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except Exception:
        pass


def transcribe_from_url(urls: str | list[str], per_url_retries: int = 2) -> str | None:
    """依次尝试候选地址（自动跳过纯视频流与损坏文件），任一成功即返回文本。

    全部失败返回 None，不阻塞采集主流程。
    """
    if isinstance(urls, str):
        urls = [urls]
    tmp_dir = DATA_DIR / "asr_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_tmp()
    for url in urls:
        for attempt in range(per_url_retries):
            try:
                t0 = time.time()
                text = _attempt(url, tmp_dir)
                print(f"      [ASR] 转写 {len(text)} 字（下载+转写 {time.time() - t0:.0f} 秒）")
                return text
            except Exception:
                if attempt == per_url_retries - 1:
                    break
                time.sleep(2)
    return None


def transcribe_items(items: list, workers: int = 2, progress=None) -> None:
    """并行转写一批已捕获媒体地址的视频（下载+转写不占用浏览器）。

    progress(done, total, item, text) 用于外部打日志；转写结果写入 item.transcript。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    todo = [it for it in items if it.play_urls and not it.transcript]
    if not todo:
        return
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(transcribe_from_url, it.play_urls): it for it in todo}
        for fut in as_completed(futures):
            it = futures[fut]
            text = fut.result() or ""
            it.transcript = text
            done += 1
            if progress:
                progress(done, len(todo), it, text)
