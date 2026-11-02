"""扫描件与图片型课件的文字层补全。

教材场景里最常见的一类 PDF 是老师把 PPT 直接导成图片再合成 PDF：每页一整张
960×540 的位图，PyMuPDF 一个文字行都提不出来。`extract_ir` 以前遇到这种情况
会静默返回空 IR，看着像"解析成功、只是文件没内容"。

这里只做兜底：**有文字层的页一律走原生提取**。原生提取带真实字号、字重和
准确的 bbox，OCR 给的坐标是猜出来的，质量差一截，不该抢原生层的活。

已知边界：公式页识别质量很差（`ν=1/T` 会变成 `v=1/T`，`u=λ/T` 会变成
`u===v`）。行内公式、上下标、根式基本不可用，别指望拿 OCR 结果去算数。
"""
from __future__ import annotations

import json
from pathlib import Path

_ENGINE = None

DEFAULT_DPI = 200
#: 行高换算成"字号"时的经验系数。OCR 的 bbox 比真实字身略高，
#: 乘一下让众数更接近正文字号，标题的相对关系不受影响。
_HEIGHT_RATIO = 0.86


def _engine():
    global _ENGINE
    if _ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的明确提示
            raise RuntimeError(
                "缺少 OCR 依赖。装一次即可：pip install rapidocr-onnxruntime"
            ) from exc
        _ENGINE = RapidOCR()
    return _ENGINE


def _render(page, dpi: int):
    """整页渲染成 numpy 数组交给 OCR（RapidOCR 对 ndarray 支持最稳）。"""
    import numpy as np

    pm = page.get_pixmap(dpi=dpi)
    buf = np.frombuffer(pm.samples, dtype=np.uint8)
    return buf.reshape(pm.height, pm.width, pm.n), pm.width, pm.height


def ocr_page(page, dpi: int = DEFAULT_DPI):
    """识别一页，返回与 `pdf._page_lines` 同构的 [(text, size, bold, bbox), ...]。

    坐标会从渲染像素换算回 PDF 点（乘 72/dpi），这样后续的双栏排序、行序检查
    和原生提取的行落在同一套坐标系里，不用分两套逻辑。

    bold 一律给 False：OCR 认不出字重，硬猜只会让标题判定跑偏。
    """
    img, _, _ = _render(page, dpi)
    result, _ = _engine()(img)
    scale = 72.0 / dpi
    lines = []
    for box, text, _score in result or []:
        clean = (text or "").strip()
        if not clean:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        size = (y1 - y0) * scale * _HEIGHT_RATIO
        lines.append((clean, round(size, 2), False,
                      (x0 * scale, y0 * scale, x1 * scale, y1 * scale)))
    return lines


def ocr_page_cached(page, doc_id: str, pno: int, cache_dir: Path | None = None,
                    dpi: int = DEFAULT_DPI):
    """带磁盘缓存的 OCR。一页约 10 秒，整份课件几十页，不缓存没法反复调试。"""
    if cache_dir is None:
        return ocr_page(page, dpi=dpi)

    target = Path(cache_dir) / doc_id / f"p{pno}.json"
    if target.exists():
        data = json.loads(target.read_text(encoding="utf-8"))
        return [(t, s, b, tuple(bb)) for t, s, b, bb in data]

    lines = ocr_page(page, dpi=dpi)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    return lines
