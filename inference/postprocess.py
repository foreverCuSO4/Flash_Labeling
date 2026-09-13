"""output0 [N, 5040, 21] 的解码。

列语义（2026-09-13 结合线上缓存和项目类别确认，见 docs/inference_backend.md）：
  cols 0-7    : 4 个角点 (x,y)，像素坐标（640×384 空间），顺序
                左上 TL → 左下 BL → 右下 BR → 右上 TR（旋转四边形）
  cols 8-11   : 4 路前缀类别 B/R/N/P 的 sigmoid 分数
  cols 12-20  : 9 路板型类别 G/1/2/3/4/5/O/Bs/Bb 的 sigmoid 分数
  最终类别    : argmax(cols 8-11) * 9 + argmax(cols 12-20)

worker 内默认执行 filter_rows（阈值 + TopK），把每帧 423KB 原始输出压缩为
k×84B 的行集合再跨进程传输；行 → 业务 dict 的转换在调用方进程完成。
"""
import numpy as np

CORNERS = slice(0, 8)
CLS = slice(8, 12)
AUX = slice(12, 21)
IM_W, IM_H = 640.0, 384.0
SCORE_COLS = CLS


def filter_rows(raw: np.ndarray,
                conf_thresh: float = 0.05,
                topk: int = 100) -> np.ndarray:
    """raw: (5040, 21) → 保留行 (k, 21)，按 score 降序。"""
    scores = raw[:, SCORE_COLS].max(axis=1)
    keep = np.flatnonzero(scores >= conf_thresh)
    if keep.size == 0:
        return np.empty((0, 21), dtype=np.float32)
    if keep.size > topk:
        keep = keep[np.argpartition(scores[keep], -topk)[-topk:]]
    keep = keep[np.argsort(-scores[keep])]
    return raw[keep].astype(np.float32, copy=False)


def score_of(row: np.ndarray) -> float:
    return float(row[SCORE_COLS].max())


def corners_norm(row: np.ndarray) -> list[list[float]]:
    """4 角点归一化 [(x,y) × 4]，TL→BL→BR→TR，裁剪到 [0,1]。"""
    pts = row[CORNERS].reshape(4, 2).astype(np.float64)
    pts[:, 0] = np.clip(pts[:, 0] / IM_W, 0.0, 1.0)
    pts[:, 1] = np.clip(pts[:, 1] / IM_H, 0.0, 1.0)
    return pts.tolist()


def row_to_dict(row: np.ndarray) -> dict:
    """一行 (21,) → 业务 dict。"""
    cls = row[CLS]
    aux = row[AUX]
    prefix_label = int(np.argmax(cls))
    board_type_label = int(np.argmax(aux))
    return {
        "corners": corners_norm(row),
        "score": score_of(row),
        "label": prefix_label * len(aux) + board_type_label,
        "prefix_label": prefix_label,
        "board_type_label": board_type_label,
        "cls_scores": [float(v) for v in cls],
        "aux_scores": [float(v) for v in aux],
    }


def rows_to_dicts(rows: np.ndarray) -> list[dict]:
    return [row_to_dict(r) for r in rows]


def decode_batch(raw_batch: np.ndarray,
                 conf_thresh: float = 0.05,
                 topk: int = 100) -> list[np.ndarray]:
    return [filter_rows(f, conf_thresh, topk) for f in raw_batch]
