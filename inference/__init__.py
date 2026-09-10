"""高吞吐 NPU 推理后端（Ascend OM + pyACL，多卡 worker 池 + 动态攒批）。"""

__all__ = ["postprocess", "pool", "worker", "acl_engine"]
