"""CPU ONNX Runtime adapter for project-uploaded models.

Uploaded models must accept 640x384 float32 image tensors in NHWC or NCHW
layout and return output0 shaped [N, 5040, 21], matching the built-in model.
The built-in model continues to use the Ascend NPU path.
"""
from pathlib import Path

import numpy as np

from inference.postprocess import filter_rows


class OnnxModelError(RuntimeError):
    pass


class OnnxModel:
    def __init__(self, path: Path):
        try:
            import onnxruntime as ort
        except ImportError as e:
            raise OnnxModelError("ONNX Runtime is not installed") from e
        try:
            self.session = ort.InferenceSession(
                str(path), providers=["CPUExecutionProvider"])
        except Exception as e:
            raise OnnxModelError(f"cannot load ONNX model: {e}") from e
        inputs = self.session.get_inputs()
        if len(inputs) != 1 or len(inputs[0].shape) != 4:
            raise OnnxModelError("model must have one 4D image input")
        self.input_name = inputs[0].name
        shape = inputs[0].shape
        if shape[-1] == 3:
            self.layout = "NHWC"
        elif shape[1] == 3:
            self.layout = "NCHW"
        else:
            raise OnnxModelError("model input must be NHWC or NCHW with 3 channels")
        self.batch_size = shape[0] if isinstance(shape[0], int) else None

    def _prepare(self, frames: np.ndarray) -> np.ndarray:
        arr = np.ascontiguousarray(frames, dtype=np.float32)
        if arr.ndim != 4 or arr.shape[1:] != (384, 640, 3):
            raise OnnxModelError("frames must have shape [N, 384, 640, 3]")
        return arr if self.layout == "NHWC" else arr.transpose(0, 3, 1, 2)

    def _run(self, batch: np.ndarray) -> np.ndarray:
        try:
            output = self.session.run(None, {self.input_name: batch})[0]
        except Exception as e:
            raise OnnxModelError(f"ONNX inference failed: {e}") from e
        output = np.asarray(output, dtype=np.float32)
        if output.ndim != 3 or output.shape[1:] != (5040, 21):
            raise OnnxModelError(
                f"model output must have shape [N, 5040, 21], got {output.shape}")
        return output

    def infer_raw(self, frames: np.ndarray) -> np.ndarray:
        arr = self._prepare(frames)
        target = self.batch_size or len(arr)
        if target <= 0:
            return np.empty((0, 5040, 21), dtype=np.float32)
        outputs = []
        for start in range(0, len(arr), target):
            chunk = arr[start:start + target]
            count = len(chunk)
            if count < target:
                pad = np.repeat(chunk[-1:], target - count, axis=0)
                chunk = np.concatenate([chunk, pad], axis=0)
            outputs.append(self._run(chunk)[:count])
        return np.concatenate(outputs) if outputs else np.empty(
            (0, 5040, 21), dtype=np.float32)

    def infer_rows(self, frames: np.ndarray) -> list[np.ndarray]:
        return [filter_rows(frame, 0.05, 100) for frame in self.infer_raw(frames)]
