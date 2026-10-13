# ComfyUI ROB3000 启动报错修复 · 精简版

> **一句话定位**：两个高频崩溃的**根因级**修复 —— `WinError 1114 / c10.dll 初始化失败`（缺一个 DLL，复制即修）、`llama-TE 反推节点报错`（装对轮子 + 补一个环境变量）。
>
> 本文件为**免费分享的精简版**（约完整版 1/3）。完整版（会员）含：一键修复 Python 脚本、交叉测试诊断脚本、PE 导入表解析器、轮子离线备份。

---

## 案例 A：`OSError: [WinError 1114]` / `Error loading "c10.dll"`

### 一句话根因

Windows standalone 版 ComfyUI 的 `python_embeded\` 根目录**缺 `msvcp140.dll`**（Python 3.13 必需的 VC++ 运行库组件）。`torch/lib/c10.dll` 运行时依赖它，缺失即导致**任何 torch DLL 加载失败**。

> ⚠️ **这不是 torch 版本问题，不要盲目降级 torch。**
> 本案最初误判为 torch 2.12 不兼容，整包换成 2.11 仍报同样的错，才锁定是运行时缺文件。**定位后用户原始 2.12.0 配置完整还原，最终只需补一个 DLL。**

### 修复（一条命令）

```bash
cp -v "E:\AIbao\ComfyUI_SF2607\python_embeded\msvcp140.dll" \
      "E:\AIbao\comfyui_sf2608\ComfyUI_ROB3000\python_embeded\msvcp140.dll"
```

源二选一：① 另一个正常的 ComfyUI 包 ② 本项目的离线备份 `comfyui_fix_assets\msvcp140.dll`。

### 验证

```bash
"<报错包>\python_embeded\python.exe" -c "import ctypes; ctypes.CDLL(r'<报错包>\python_embeded\Lib\site-packages\torch\lib\c10.dll'); print('c10.dll OK')"
"<报错包>\python_embeded\python.exe" -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

通过后双击 `run_nvidia_gpu.bat` 即可正常使用。

### 诊断方法论：交叉测试（报错不典型时用）

用两个包的 python 分别加载两个包的 c10.dll：

| 测试 | SF python→SF c10 | SF python→ROB c10 | ROB python→SF c10 | ROB python→ROB c10 |
|------|:---:|:---:|:---:|:---:|
| 结果 | ✅ | ✅ | ❌ | ❌ |

→ **结论：问题在 ROB 自己的 `python_embeded` 运行时缺文件，与 torch 无关。**

再对比两个 `python_embeded\` 根目录的 dll 列表，找出缺失项：
```bash
comm -13 /tmp/rob.txt /tmp/sf.txt   # SF 有而 ROB 缺的 → 重点嫌疑
```

---

## 案例 B：comfyUI-llama-TE 反推节点报错（V1.1）

### 触发症状
- `RuntimeError: 未检测到 llama-cpp-python` / `No module named 'llama_cpp'`
- `当前 llama-cpp-python 不支持 Qwen3VLChatHandler / Qwen35ChatHandler`
- 反推节点能跑但极慢，日志全是 `layer N assigned to device CPU`

### 环境
`E:\AIbao\comfyui_sf2608\ComfyUI_ROB3000`，python_embeded **Python 3.13.12**，torch **2.12.0+cu130**，RTX 4060 Ti。

### 修复流程

**1. 装 JamePeng 社区分支轮子**（官方 abetlen 版缺 Qwen3VL/Qwen35 handler）
```bash
PY="E:\AIbao\comfyui_sf2608\ComfyUI_ROB3000\python_embeded\python.exe"
"$PY" -m pip install --no-deps --force-reinstall \
  "llama_cpp_python-0.3.48+cu130-cp313-cp313-win_amd64.whl"
```
来源 `github.com/JamePeng/llama-cpp-python/releases` tag `v0.3.48-cu130-win-20260821`。
**cu 版本必须和包内 torch 的 cu 对齐**（torch cu130 → 选 cu130 轮子，这样 `cublas64_13.dll` 可直接复用 `torch\lib` 里的）。

**2. 关键坑：分裂构建 → CUDA 后端不会自动注册**
JamePeng 轮子的 llama.dll（仅 3.5MB）不直接链接 `ggml-cuda.dll`，靠 ggml 动态后端加载器，而它**默认只扫 exe 目录和当前目录** → 模型全跑 CPU。必须在**导入 llama_cpp 之前**设置：

```python
os.environ["GGML_BACKEND_PATH"] = r"...\site-packages\llama_cpp\lib\ggml-cuda.dll"
```
⚠️ 指向 **DLL 文件本身**，不是目录（指目录会报 `load_backend: failed to load`，且 Windows 端 `dl_error()` 恒为空串，看不出原因）。

**3. 关键坑：cublas 依赖靠 PATH 而不是 `add_dll_directory`**
ggml 用裸 `LoadLibraryW` 加载（不认 `os.add_dll_directory`），`cublas64_13.dll` 必须能从 **PATH** 找到。llama-TE 节点的 `_准备llama_windows_dll搜索路径()` 本来就会把 `torch\lib` 前插到 PATH，正好覆盖。

**4. 节点补丁** — 在 `nodes.py` 的 `_准备llama_windows_dll搜索路径()` 末尾加：
```python
for lib_dir in candidates:
    cuda_backend = lib_dir / "ggml-cuda.dll"
    if cuda_backend.is_file():
        os.environ.setdefault("GGML_BACKEND_PATH", str(cuda_backend))
        break
```

**5. 验证**
```bash
"$PY" -c "import os; os.environ['GGML_BACKEND_PATH']=r'...\llama_cpp\lib\ggml-cuda.dll'; \
import llama_cpp; print(llama_cpp.llama_supports_gpu_offload())"
```
用小模型加载 `n_gpu_layers=-1`，日志出现 `assigned to device CUDA0` 即成功。装完重启 ComfyUI 生效。

---

## 踩坑清单

| 坑 | 说明 |
|---|---|
| 不要降级 torch | 根因是缺 DLL，降级 torch 白折腾还要还原 |
| bat 在 Git Bash 下假报错 | `run_nvidia_gpu.bat` 用 `%~dp0` 取路径，Git Bash 解析失败报"系统找不到指定的路径"。属模拟环境问题，不影响本机双击。验证请直接调 `python_embeded\python.exe -s ComfyUI\main.py` |
| System32 的 msvcp140.dll 不一定可靠 | torch 的加载搜索顺序未必命中 / 版本不匹配。**直接放进 `python_embeded\` 根目录最稳** |
| 备份 DLL | 首次修复后把 `msvcp140.dll` 复制到 `comfyui_fix_assets\` 作离线备份，下次可不依赖对照包 |
| `gpu_offload: False` ≠ 没 CUDA | 可能是后端没注册（本案例）。用真实模型加载日志判定 |
| ctypes.CDLL 能加载 ≠ LoadLibraryW 能加载 | 查 DLL 依赖会假阳性，需手写 PE 导入表解析 |

---

> **精简版说明**：保留两个案例的完整根因、命令与关键坑（最耗时定位的部分）。完整版另含一键修复脚本与交叉测试诊断脚本，可免手动操作。
