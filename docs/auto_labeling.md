# 对自动标注的一些想法。
我现在已经有了一个可以识别我想识别的目标的模型。
我希望这个模型可以做这两件事：
1. 从视频中自动选取有标注价值的帧。具体方式是：对整个视频跑一遍识别，选择一个比较低的置信度阈值(例如0.2)，然后对于所有识别出目标的帧，做向前向后的膨胀(例如各膨胀3s)。去并集得到"有标注价值的时间段"。在这个时间段内以10fps做采样。得到待标注数据集。
2. 在标注过程中，标注者不需要手动点各个关键点。而是引入笔刷的概念，以一个光标为中心圆形区域为笔刷。点击后，如果区域内有被模型识别出的目标，那么就直接标注上去。这一步由于有了区域范围先验，因此可以设一个极低的置信度阈值。

---

## 实现状态（已完成初版，依赖推理后端运行中）

**1. 自动选帧（上传页 Advanced sampling → "Model auto-scan"）**

- 参数：`conf`（命中阈值，默认 0.2）、`dilate_s`（前后膨胀秒数，默认 3）、
  `sample_fps`（段内采样，默认 10）、`scan_stride`（隔帧扫描，默认 1）
- 管道（`app/video.py::extract_frames_auto`）：pass1 全片并行解码逐帧推理
  →`app/autolabel.py::compute_windows` 膨胀并集 → pass2 仅对窗口重解码、
  按网格写 JPEG 注册为普通 Image
- 每个抽取帧的检测结果（conf floor 0.05，top100 行）落盘缓存：
  `data/cache/det/<project_id>/<stored_name>.npz`（`app/detector.py`）
- 推理服务不可达时 job 明确 failed（`inference service not reachable`），
  不动现有 motion 采样路径

**2. 笔刷（标注页，快捷键 B，半径滑条）**

- 点击 → `POST /api/images/{id}/brush {x, y, r}` → 命中行取最高分：
  返回归一化框（`app/routers/autolabel.py`）
- 判定口径：模型实例**中心（4 角点质心）**落入笔刷圆（图像像素域，抗宽高比）
- 模型行语义（2026-09-04 样例图目视确认）：`cols0-7 = 4 角点 (x,y)`
  TL→BL→BR→TR 旋转四边形；`cols8-11` = 4 类 sigmoid；`cols12-20` = 9 路
  次级标签（待定名）
- 笔刷当前吸附**外接轴对齐框**（Annotation 模型是 bbox）；类别 = 标注者
  当前选中类；pose 项目吸附框后照常进入关键点放置；segment 项目不启用
- 缓存命中直接作答；普通上传图片首击按需单帧推理并落缓存

**待办**：
- 笔刷可升级为直接吸附 4 角点（segment 项目可落成 4 点多边形，pose 项目
  可映射为 4 关键点实例）——Annotation 模型已支持 polygon，待确认项目
  里"角点"的落法（polygon vs 4 keypoints）后再加
- 9 路次级标签与项目类别的映射配置
