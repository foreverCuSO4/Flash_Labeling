# 高吞吐推理后端（Ascend NPU）

为自动标注管道服务的独立推理后端：OM + pyACL，单卡多进程 + 动态攒批，
HTTP 接口。本文件记录环境、用法与实测数字。

## 硬件与栈

- 8× Ascend 910B3（65GB HBM/卡），Kunpeng-920 aarch64 / 192 核
- CANN 8.2.RC1：`/usr/local/Ascend/ascend-toolkit/`，ATC 编译 + pyACL 推理
- 服务代码 python3.14（项目 `.venv`）；pyACL `acl.so` 可直接在 3.14 import

## 模型

- 输入 `images [1,384,640,3]` float32 **NHWC**，0-255 像素值，streaming 原生
  batch=1；**/255 归一化与 BGR→RGB 通道翻转都在图内**，喂原始 uint8 帧即可
- 输出 `output0 [B,5040,21]`；列语义（2026-09-04 用模型所有者样例图
  目视验证确认）：
  - `cols 0-7`：**4 个角点 (x,y)**，640×384 像素，顺序 左上→左下→右下→右上
    （旋转四边形；DFL 每坐标独立 10-bin 解码，80=8×10）
  - `cols 8-11`：4 类 sigmoid 分数（检出为 one-hot 形态）
  - `cols 12-20`：**9 路次级标签** sigmoid（每检出恰有 1 个亮起，ombo
    为子类/属性分类头；具体 9 类名称待项目侧对照类别表确认）
- OM 产物在 `data/om_models/`：b1 / b8 / b16 / b32
  （b8/b16/b32 通过改写 ONNX batch 维 + 修补 5 个内嵌 batch=1 的 reshape
  常量生成，脚本逻辑见 git 历史；**精度校验 cos=1.000000** 与逐帧等价）

## ATC 编译环境（坑）

CANN 8.2 的 TBE 要求 **python ≤3.11 + numpy<2 + distutils**。本机项目
venv 是 python3.14（numpy 2.x，且 3.12+ 移除了 distutils），直接用会报
`op type Conv2D is not found in this op store`（误判为算子不支持，
真因是 `Unable to import impl.conv2d, reason: No module named 'distutils'`）。

做法：单独建 ATC 专用环境

```bash
uv venv --python 3.11 .venv-atc
uv pip install --python .venv-atc/bin/python "numpy==1.26.4" onnx attrs \
    decorator sympy cffi scipy psutil protobuf onnxruntime

source /usr/local/Ascend/ascend-toolkit/set_env.sh
export PATH=$HOME/.local/share/uv/python/cpython-3.11.16-linux-aarch64-gnu/bin:$PATH
export PYTHONPATH=$PWD/.venv-atc/lib/python3.11/site-packages:$PYTHONPATH
atc --model=<onnx> --framework=5 --output=<out> --soc_version=Ascend910B3 \
    --input_shape="images:N,384,640,3" --precision_mode=allow_fp32_to_fp16
```

## 架构

```
  HTTP (uvicorn, :8787)
    └── InferPool                      主进程
          ├── ShmRing 64×11.8MB slots  帧数据共享内存（生产者写，worker 读）
          ├── in_q / out_q             控制与结果消息
          └── worker × R/卡（默认 4）  每进程独占 context，绑一张卡
                 ├── 攒批: max_batch=16 或 5ms 窗口，尾批 pad
                 ├── OmModel b16 + b8（按批量选档）
                 └── 执行后 filter_rows(0.05, top100) 只回传命中行
```

关键数字（640×384 帧）：

| 环节 | 单帧 | 说明 |
|---|---|---|
| SMK 纯 execute (b16) | 0.54ms | 1850 fps/卡 |
| + H2D/D2H 拷贝 | ~1.25ms | execute+memcpy 单线程 20ms/b16 |
| 完整管线 (6 副本) | 0.56ms | **1790 fps/卡**，见下基准 |

设计要点：
- 帧传输走共享内存槽（737KB/帧不 pickle 入队）；结果在 worker 侧做
  阈值+TopK 过滤后只回命中行（~100×84B/帧），两侧 IPC 都不成瓶颈
- 单卡多 worker 进程重叠 DMA 拷贝/host 后处理与 NPU 计算
- 背压：shm 槽位有限（4×worker 数），打满即阻塞生产端

## 服务

```bash
scripts/run_infer_server.sh          # 默认 INFER_DEVICES=0, INFER_REPLICAS=4, :8787
```

- `GET /health` / `GET /stats`
- `POST /infer` body=npz(`frames` uint8 [n,384,640,3])，
  query `raw=1|0`、`conf`、`topk`；返回 JSON 检测列表或 raw npz
- `POST /infer_jpeg` multipart 图片，cv2 解码 resize 后同上

## 实测基准

| 场景 | 配置 | 吞吐 |
|---|---|---|
| 纯 execute 冒烟 | b1/b8/b16/b32 单模型串行 | 439 / 1647 / 1850 / 1937 fps |
| 池内 decode 模式 | 1 卡 × {1,2,3,4,6} worker | 533 / 886 / 1111 / 1512 / 1790 fps |
| 5 分钟长跑 | 1 卡 × 6 worker，~50 万帧，4 并发 submit | **1760 fps，errors=0**，HBM 全程稳定 9.4GB（无泄漏） |
| 视频端到端 | cv2 解 720p30 + resize + 推理 | ~166 fps（解码单线程瓶颈） |
| HTTP /infer | 4 并发 npz×64 帧，errors=0 | **662 fps**（npz 编解码 + HTTP 开销） |

并发注意：shm 槽数 = 4×worker 数（默认 24）。并发 submit 数 × 每请求
槽需求不宜超过槽总量太多，否则 semaphore 队头阻塞会放大延迟
（bench 中 16 客户端 × 满槽整批提交出现明显退化；4 并发 × 256 帧健康）。
生产调用建议：chunk 64、in-flight ≤ 8（`inference/video.py` 的默认形态）。

CPU 侧单 Pipeline 节拍（b16）：拷贝+cast 5.5ms，execute+D2H 20ms，
filter 3.6ms —— 故多副本重叠是必要的。NPU 纯算 b32 收益仅 +5%，
采用 b16 为主档、b8 处理小批次。

视频解码瓶颈说明：单线程 cv2 软解 ~166fps 即上限；上传即扫的生产路径
应开 N 个解码进程沿时间轴切片（每片一个 VideoCapture seek），
192 核足够吃满 8 卡 × 1790fps × 多视频并发。

## 与标注 app 的集成（auto-labeling）

标注 app **不**在进程内加载 pyACL，只经 HTTP 调用本服务：
- `app/detector.py`：`INFER_URL`（默认 `http://127.0.0.1:8787`）→
  `POST /infer`（npz uint8 [n,384,640,3]，JSON 返回）；服务不可达抛
  `DetectorUnavailable` → auto job failed、brush 503，web app 不受影响
- `INFER_MODEL_TAG` 写入缓存 meta 便于溯源
- 缓存：`data/cache/det/<project_id>/<stored_name>.npz`（0.05/top100 行，
  原子 tmp+rename 写入）
- 启动顺序：先 `scripts/run_infer_server.sh`，再启动 app（app 只在用到
  时才连服务，顺序无硬依赖）

## 待办 / 已知项

1. 分类输出由 `cols8-11` 的 4 路前缀类别与 `cols12-20` 的 9 路板型类别组成，
   最终类别序号为 `前缀索引 × 9 + 板型索引`；角点仍需在更多真实场景复核
2. ATC 只支持 `--input_format NCHW/ND/NCDHW`（ONNX 无 NHWC 选项），本
   模型图内自带 Transpose，无碍
3. `data/uploads` 为空，视频链路只有合成 mp4 验证；真实视频需复测
4. DVPP 硬解未启用（910B3 视频解码能力待验证，属后续优化）
