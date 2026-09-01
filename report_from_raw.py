"""从已采集的原始 JSON 直接生成攻略报告（不重新爬取）。

用法：
    python report_from_raw.py data/raw/西湖_20260828_233200.json

用途：采集与报告解耦——同一批数据可以反复调整提示词重新生成报告，
也是后续"景点知识库"复用采集结果的雏形。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from config import REPORT_DIR
from core.models import Comment, VideoItem
from pipeline.extract import extract_points
from pipeline.render import render_report
from pipeline.report import synthesize_report
from pipeline.verify import annotate_confidence


def main():
    if len(sys.argv) < 2:
        print("用法: python report_from_raw.py <data/raw/xxx.json>")
        sys.exit(1)

    raw_path = Path(sys.argv[1])
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    items = [
        VideoItem(
            video_id=d["video_id"],
            url=d["url"],
            description=d.get("description", ""),
            tags=d.get("tags", []),
            like_count=d.get("like_count"),
            publish_time=d.get("publish_time"),
            transcript=d.get("transcript", ""),
            comments=[Comment(**c) for c in d.get("comments", [])],
        )
        for d in raw
    ]
    keyword = raw_path.stem.split("_")[0]
    print(f"载入 {len(items)} 条视频（{sum(len(i.comments) for i in items)} 条评论），开始生成报告…")

    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t0 = _time.time()
    all_points = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(extract_points, it): it for it in items}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                pts = fut.result()
                all_points.extend(pts)
                print(f"  {it.video_id}: 提取 {len(pts)} 条要点")
            except Exception as e:
                print(f"  {it.video_id}: 提取失败 {e}")
    print(f"  提取耗时 {_time.time() - t0:.1f} 秒")

    t0 = _time.time()
    print("交叉验证与置信度标注…")
    all_points = annotate_confidence(all_points)
    body = synthesize_report(keyword, all_points)
    out = REPORT_DIR / f"{keyword}_{datetime.now():%Y%m%d_%H%M%S}.md"
    out.write_text(render_report(keyword, body, items, all_points), encoding="utf-8")
    print(f"  汇总生成耗时 {_time.time() - t0:.1f} 秒")
    print(f"✔ 报告已生成：{out}")


if __name__ == "__main__":
    main()
