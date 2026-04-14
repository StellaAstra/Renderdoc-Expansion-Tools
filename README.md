# RenderDoc 扩展工具集 — 技术原理解析

> 本文档深入解析 **Texture Exporter（贴图导出扩展）** 和 **Model Extractor（模型提取扩展）** 的内部实现原理，涵盖架构设计、数据流、核心算法和关键技术细节。

---

## 目录

- [一、RenderDoc 扩展机制](#一renderdoc-扩展机制)
  - [1.1 扩展加载原理](#11-扩展加载原理)
  - [1.2 扩展注册接口](#12-扩展注册接口)
  - [1.3 回放线程模型](#13-回放线程模型)
- [二、安装脚本原理](#二安装脚本原理)
- [三、Texture Exporter 原理解析](#三texture-exporter-原理解析)
  - [3.1 整体架构](#31-整体架构)
  - [3.2 贴图资源枚举](#32-贴图资源枚举)
  - [3.3 事件贴图关联收集](#33-事件贴图关联收集)
  - [3.4 贴图保存与格式处理](#34-贴图保存与格式处理)
  - [3.5 特殊贴图类型处理](#35-特殊贴图类型处理)
  - [3.6 UI 面板与停靠窗口](#36-ui-面板与停靠窗口)
- [四、Model Extractor 原理解析](#四model-extractor-原理解析)
  - [4.1 整体架构](#41-整体架构)
  - [4.2 GPU 管线数据提取原理](#42-gpu-管线数据提取原理)
  - [4.3 顶点属性语义识别](#43-顶点属性语义识别)
  - [4.4 索引缓冲区读取](#44-索引缓冲区读取)
    - [4.4.1 baseVertex 偏移](#441-basevertex-偏移)
    - [4.4.2 索引重映射（Index Remapping）](#442-索引重映射index-remapping)
  - [4.5 顶点缓冲区读取与解码](#45-顶点缓冲区读取与解码)
    - [4.5.1 R10G10B10A2 打包格式支持](#451-r10g10b10a2-打包格式支持)
    - [4.5.2 分量数截取与填充](#452-分量数截取与填充)
  - [4.6 UV 多通道与 Unpack 机制](#46-uv-多通道与-unpack-机制)
  - [4.7 坐标系变换与数据清洗](#47-坐标系变换与数据清洗)
    - [4.7.1 DX 左手→右手坐标系转换](#471-dx-左手右手坐标系转换)
    - [4.7.2 法线压缩编码检测](#472-法线压缩编码检测)
  - [4.8 导出格式实现细节](#48-导出格式实现细节)
- [五、核心数据结构](#五核心数据结构)
- [六、API 兼容性处理](#六api-兼容性处理)
- [七、错误处理与容错机制](#七错误处理与容错机制)
- [八、v2.1 修复记录](#八v21-修复记录)

---

## 一、RenderDoc 扩展机制

### 1.1 扩展加载原理

RenderDoc 的扩展系统基于 Python 模块热加载机制。每个扩展是一个包含 `__init__.py` 和 `extension.json` 的独立目录：

```
extension_name/
├── __init__.py       # Python 主模块，必须定义 register() 和 unregister()
└── extension.json    # 扩展元数据（名称、版本、作者、描述）
```

**加载流程：**

```
RenderDoc 启动
  └─> 扫描扩展目录（%APPDATA%/qrenderdoc/extensions/）
      └─> 读取 extension.json 获取元信息
          └─> 用户在 Manage Extensions 中启用扩展
              └─> import __init__ 模块
                  └─> 调用 register(version, ctx) 函数
                      └─> 扩展注册菜单项、面板等
```

`extension.json` 的作用是声明扩展元信息，RenderDoc 据此在管理界面中展示扩展列表。真正的逻辑全部在 `__init__.py` 中。

### 1.2 扩展注册接口

扩展必须暴露三个全局对象：

```python
extiface_version = 0          # 接口版本号，当前固定为 0

def register(version, ctx):   # 扩展加载时调用
    """
    version: RenderDoc 扩展接口版本
    ctx: CaptureContext 对象 — 扩展与 RenderDoc 交互的核心入口
    """
    pass

def unregister():             # 扩展卸载时调用
    pass
```

`CaptureContext`（简称 `ctx`）是扩展与 RenderDoc 主程序的桥梁，提供以下关键能力：

| 方法 / 属性                 | 作用                                        |
| ----------------------- | ----------------------------------------- |
| `ctx.Extensions()`      | 获取扩展管理器，用于注册菜单、创建 UI、弹出对话框                |
| `ctx.Replay()`          | 获取回放控制器代理，通过 `BlockInvoke()` 将操作调度到回放线程执行 |
| `ctx.CurEvent()`        | 获取当前选中的事件 ID（Event ID）                    |
| `ctx.IsCaptureLoaded()` | 检查是否已加载 Capture 文件                        |
| `ctx.AddDockWindow()`   | 注册停靠窗口                                    |
| `ctx.RaiseDockWindow()` | 将已存在的停靠窗口提升到前台                            |

菜单注册使用 `RegisterWindowMenu()`：

```python
ctx.Extensions().RegisterWindowMenu(
    qrd.WindowMenu.Tools,                    # 菜单位置：Tools 菜单
    ["Texture Exporter", "Export All"],       # 菜单路径（支持多级）
    callback_function                        # 点击回调 callback(ctx, data)
)
```

### 1.3 回放线程模型

**这是扩展开发中最关键的概念。**

RenderDoc 使用**双线程模型**：

```
┌─────────────────────┐     ┌──────────────────────┐
│     UI 线程 (主线程)    │     │   回放线程 (Replay)     │
│                     │     │                      │
│  - 菜单回调            │     │  - 重放 GPU 指令         │
│  - UI 控件操作          │     │  - 读取缓冲区数据          │
│  - 对话框显示            │     │  - 查询管线状态            │
│                     │     │  - 保存贴图到文件          │
│  ─── BlockInvoke() ──┼────>│                      │
│     (阻塞等待返回)        │<────┼── (执行完成，返回结果)       │
└─────────────────────┘     └──────────────────────┘
```

**关键规则**：

- 所有涉及 GPU 数据读取的操作（读缓冲区、查管线状态、保存贴图）**必须在回放线程中执行**
- UI 线程通过 `ctx.Replay().BlockInvoke(callback)` 将函数调度到回放线程
- `BlockInvoke` 会阻塞 UI 线程直到回调执行完成
- 回调函数签名：`def callback(controller: ReplayController)`

这就是为什么两个扩展的核心导出逻辑都被包装在 `_run(controller)` 闭包中，通过 `BlockInvoke` 调用：

```python
def _do_export(ctx, ...):
    result = [None]                    # 用列表在闭包间传递结果

    def _run(controller):              # 此函数在回放线程中执行
        result[0] = do_export(controller, ...)

    ctx.Replay().BlockInvoke(_run)     # 阻塞等待回放线程完成
    show_result(result[0])             # 回到 UI 线程显示结果
```

---

## 二、安装脚本原理

`install_extension.py` 的核心逻辑非常简单：将扩展目录复制到 RenderDoc 的标准扩展路径。

**平台检测逻辑：**

```python
# Windows:  %APPDATA%\qrenderdoc\extensions\
# macOS:    ~/Library/Application Support/qrenderdoc/extensions/
# Linux:    $XDG_DATA_HOME/qrenderdoc/extensions/  (默认 ~/.local/share/)
```

**安装流程：**

```
1. 检测当前操作系统 → 确定扩展目录路径
2. 如果目标目录已存在同名扩展 → shutil.rmtree() 删除旧版
3. shutil.copytree() 将整个扩展目录复制过去
4. 提示用户重启 RenderDoc 并在 Manage Extensions 中启用
```

支持通过 `--ext` 参数选择性安装单个扩展，通过 `--target` 指定自定义目录。

---

## 三、Texture Exporter 原理解析

### 3.1 整体架构

```
用户操作 (菜单/面板)
  │
  ├─> Export All → BlockInvoke → do_export_textures(controller, config, target_ids=None)
  │                                │
  │                                ├─> controller.GetTextures()  获取所有贴图列表
  │                                ├─> 过滤（尺寸、名称、目标ID）
  │                                └─> controller.SaveTexture()  逐个保存
  │
  ├─> Export Event → BlockInvoke → collect_event_texture_ids(controller)
  │                                │   ├─> controller.GetPipelineState()
  │                                │   ├─> 遍历各着色器阶段的 SRV / UAV
  │                                │   └─> 收集 Render Target / Depth Target
  │                                └─> do_export_textures(controller, config, target_ids=收集结果)
  │
  └─> List All → BlockInvoke → list_all_textures(controller)
                                 └─> 遍历 + 统计 + 格式化输出
```

### 3.2 贴图资源枚举

贴图枚举通过 `controller.GetTextures()` 实现，返回 Capture 中所有纹理资源的元信息列表（`TextureDescription` 对象）。

每个 `TextureDescription` 包含：

| 属性               | 说明                |
| ---------------- | ----------------- |
| `resourceId`     | 资源唯一 ID           |
| `width / height` | 尺寸（像素）            |
| `depth`          | 3D 纹理的深度          |
| `mips`           | Mipmap 级别数        |
| `arraysize`      | 纹理数组大小            |
| `msSamp`         | 多重采样数             |
| `format`         | 像素格式（如 BC7_UNORM） |
| `type`           | 纹理类型（2D/3D/Cube等） |

**资源名称获取**是一个独立步骤。贴图本身不携带名称，名称存储在资源描述列表中：

```python
def _build_resource_name_map(controller):
    """构建 resourceId -> name 的映射表"""
    name_map = {}
    resources = controller.GetResources()    # 获取所有资源描述
    for res in resources:
        name_map[int(res.resourceId)] = res.name
    return name_map
```

之所以将 `resourceId` 转为 `int` 作为字典键，是因为 RenderDoc 的 `ResourceId` 对象直接比较可能不可靠（不同 Python 包装实例），而其整数表示是稳定的。

### 3.3 事件贴图关联收集

"导出当前事件贴图"功能的核心是 `collect_event_texture_ids()`，它从 GPU 管线状态中提取当前 DrawCall 使用的所有贴图资源。

**工作原理：**

```python
def collect_event_texture_ids(controller):
    state = controller.GetPipelineState()    # 获取当前事件的完整管线状态
    resource_ids = set()

    # 遍历所有着色器阶段
    for stage in [Vertex, Hull, Domain, Geometry, Pixel, Compute]:
        # 1. 只读资源（Shader Resource Views / 纹理采样器绑定）
        ro_resources = state.GetReadOnlyResources(stage, False)
        # ... 提取 resource ID ...

        # 2. 读写资源（Unordered Access Views）
        rw_resources = state.GetReadWriteResources(stage, False)
        # ... 提取 resource ID ...

    # 3. 渲染目标（Render Targets）
    om_targets = state.GetOutputTargets()
    # ... 提取 resource ID ...

    # 4. 深度缓冲（Depth/Stencil Target）
    ds = state.GetDepthTarget()
    # ... 提取 resource ID ...

    return resource_ids
```

**GPU 管线绑定点解析：**

```
GPU Pipeline 各阶段的资源绑定
├── SRV (Shader Resource View) — 着色器读取的贴图
│   ├── VS (顶点着色器) 的 SRV
│   ├── PS (像素着色器) 的 SRV ← 大部分纹理采样在这里
│   └── CS (计算着色器) 的 SRV
├── UAV (Unordered Access View) — 可读写的贴图/缓冲
│   └── 通常用于计算着色器输出
├── Render Targets — 渲染目标（帧缓冲颜色附件）
└── Depth Target — 深度/模板缓冲
```

**API 兼容性处理**（详见第六节）：RenderDoc v1.30+ 的 API 改变了资源绑定的返回结构，`collect_event_texture_ids` 通过 `_extract_resource_id()` 辅助函数同时支持新旧两种 API：

```python
def _extract_resource_id(obj):
    if hasattr(obj, 'resource'):      # 新版 API: Descriptor.resource
        return obj.resource
    if hasattr(obj, 'resourceId'):    # 旧版 API: .resourceId
        return obj.resourceId
    return rd.ResourceId.Null()
```

### 3.4 贴图保存与格式处理

贴图保存使用 RenderDoc 内建的 `controller.SaveTexture()` API：

```python
save = rd.TextureSave()
save.resourceId = tex.resourceId     # 要保存的贴图资源 ID
save.mip = mip_level                 # Mipmap 级别
save.slice.sliceIndex = slice_idx    # 数组/CubeMap/3D 切片索引
save.alpha = rd.AlphaMapping.Preserve  # Alpha 通道处理方式
save.destType = rd.FileType.DDS      # 目标文件格式

controller.SaveTexture(save, filepath)
```

**"保持原始格式"机制：**

当用户勾选 "Keep original format (DDS)" 时，扩展使用 DDS 格式保存。DDS（DirectDraw Surface）是唯一能保持 GPU 压缩纹理原始格式的导出选项：

- BC1/BC3/BC5/BC7 等 Block Compression 格式会**原样保存**，不经过解压重压
- ASTC 格式同理
- 如果转为 PNG/TGA 等格式，RenderDoc 会先将压缩纹理**解压为 RGBA**，再编码为目标格式，会丢失压缩特征

**文件命名策略：**

```
{资源名称}_{宽}x{高}_{原始格式名}[_mip{N}][_面/切片].{扩展名}
```

原始格式名（如 `BC7_UNORM`）嵌入文件名中，便于用户在文件列表中快速识别贴图的 GPU 格式。

### 3.5 特殊贴图类型处理

扩展根据 `TextureType` 枚举处理不同类型的纹理：

| 纹理类型             | 切片策略                                                        |
| ---------------- | ----------------------------------------------------------- |
| Texture2D        | 单张，无需切片                                                     |
| TextureCube      | 6 个面（PosX, NegX, PosY, NegY, PosZ, NegZ），通过 `sliceIndex` 遍历 |
| TextureCubeArray | `arraysize` 个面（每 6 个一组为一个 Cube）                             |
| Texture2DArray   | `arraysize` 个切片                                             |
| Texture3D        | `depth` 个深度切片                                               |
| Texture2DMS      | 多重采样纹理，按单张处理                                                |

```python
def get_slice_count(tex, config):
    if tex.type == TextureCube:
        return 6 if config["cubemap_faces"] else 1
    elif tex.type == Texture3D:
        return tex.depth if config["slices_3d"] else 1
    elif tex.type in (Texture2DArray, ...):
        return tex.arraysize
    return 1
```

### 3.6 UI 面板与停靠窗口

Texture Exporter 实现了两种 UI 形态：

**1. 模态对话框**（用于 Export All / Export Event）：

```python
dialog = mqt.CreateToplevelWidget(title, on_closed)
# ... 添加控件 ...
mqt.ShowWidgetAsDialog(dialog)    # 以模态对话框形式显示
```

**2. 停靠面板**（Quick Panel）：

```python
# 创建面板类实例
panel = TextureExporterPanel(ctx)
widget = panel.get_widget()

# 注册为 RenderDoc 可停靠窗口
ctx.AddDockWindow(widget, qrd.DockReference.MainToolArea, None)
```

停靠面板通过 `TextureExporterPanel` 类封装，使用 `MiniQtHelper` API 构建 UI。面板只创建一次（单例），后续通过 `RaiseDockWindow()` 提升到前台：

```python
def _on_open_panel(ctx, data):
    if _panel_widget is not None:
        ctx.RaiseDockWindow(_panel_widget)    # 已存在，提到前台
    else:
        _create_panel(ctx)                    # 首次创建
```

`MiniQtHelper` 是 RenderDoc 提供的轻量级 Qt 包装 API，支持以下控件：

| 方法                            | 创建控件   |
| ----------------------------- | ------ |
| `CreateLabel()`               | 文本标签   |
| `CreateButton(cb)`            | 按钮     |
| `CreateCheckbox(cb)`          | 复选框    |
| `CreateComboBox(edit,cb)`     | 下拉选择框  |
| `CreateTextBox(sl,cb)`        | 文本输入框  |
| `CreateVerticalContainer()`   | 垂直布局容器 |
| `CreateHorizontalContainer()` | 水平布局容器 |

---

## 四、Model Extractor 原理解析

### 4.1 整体架构

```
用户操作
  │
  ├─> Extract Current → BlockInvoke → extract_mesh_from_draw(controller, action, config)
  │                                     │
  │                                     ├─> controller.SetFrameEvent(eid)  跳转到目标事件
  │                                     ├─> controller.GetPipelineState()  获取管线状态
  │                                     ├─> state.GetVertexInputs()       获取顶点属性布局
  │                                     ├─> state.GetVBuffers()           获取顶点缓冲区绑定
  │                                     ├─> state.GetIBuffer()            获取索引缓冲区绑定
  │                                     ├─> 识别语义（Position/Normal/UV/Color）
  │                                     ├─> 读取索引缓冲区 → 解码索引
  │                                     ├─> 读取顶点缓冲区 → 解码各属性
  │                                     └─> 返回 mesh_data 字典
  │                                   │
  │                                   └─> export_mesh() → OBJ/PLY/glTF/CSV/FBX
  │
  └─> Batch Extract → 递归遍历 action 树 → 对每个 DrawCall 执行上述流程
```

### 4.2 GPU 管线数据提取原理

理解 Model Extractor 需要先理解 GPU 渲染管线中网格数据的存储方式：

```
应用程序提交 DrawCall
  │
  ├─> Input Assembler (IA) 阶段
  │   ├── Vertex Buffers (VB) — 存储顶点属性数据（位置、法线、UV...）
  │   │   └── VB0: [stride=32, offset=0] → GPU 内存块
  │   │   └── VB1: [stride=16, offset=0] → GPU 内存块
  │   ├── Index Buffer (IB) — 存储三角面索引
  │   │   └── IB: [stride=2(uint16) 或 4(uint32)]
  │   └── Vertex Input Layout — 描述如何从 VB 中提取各属性
  │       ├── POSITION:  VB=0, offset=0,  format=R32G32B32_FLOAT
  │       ├── NORMAL:    VB=0, offset=12, format=R32G32B32_FLOAT
  │       ├── TEXCOORD0: VB=0, offset=24, format=R32G32_FLOAT
  │       └── TEXCOORD1: VB=1, offset=0,  format=R16G16_FLOAT
  │
  └─> Vertex Shader → ... → Rasterizer → Pixel Shader → 输出
```

Model Extractor 的核心策略是**从 Input Assembler 阶段读取原始顶点输入数据**（VSIn），而非经过着色器变换后的数据。这确保了提取的是模型的**物体空间（Object Space）**几何体，而非经过 MVP 变换后的裁剪空间/屏幕空间坐标。

提取步骤：

```python
# 1. 跳转到目标 DrawCall 事件
controller.SetFrameEvent(action.eventId, True)

# 2. 获取当前管线状态快照
state = controller.GetPipelineState()

# 3. 获取 IA 阶段的配置
vbs = state.GetVBuffers()       # 顶点缓冲区绑定列表
ib = state.GetIBuffer()         # 索引缓冲区绑定
attrs = state.GetVertexInputs() # 顶点属性布局列表
```

### 4.3 顶点属性语义识别

GPU 管线中的顶点属性只有名称（语义名）和格式信息，扩展需要通过**语义名匹配**来识别各属性的用途。

**识别优先级与规则：**

```python
for attr in attrs:
    name_lower = attr.name.lower()

    # === 位置属性 ===
    # 匹配: POSITION, Pos 等
    # 排除: SV_POSITION（这是 VS 输出语义，不是顶点输入）
    if 'sv_position' in name_lower:
        skip  # SV_POSITION 是系统语义，是顶点着色器的输出
    elif 'position' or 'pos' in name_lower:
        pos_attr = attr

    # === 法线属性 ===
    # 匹配: NORMAL, Norm
    if 'normal' or 'norm' in name_lower:
        normal_attr = attr

    # === UV 属性 ===
    # 匹配: TEXCOORD, UV, Tex
    # 从名称尾部提取语义索引: TEXCOORD0 → 0, TEXCOORD1 → 1
    if 'texcoord' or 'uv' or 'tex' in name_lower:
        sem_idx = extract_trailing_number(attr.name)
        # 根据分量数和 unpack_uv 设置决定处理方式（详见 4.6 节）

    # === 顶点色属性 ===
    # 匹配: COLOR, Colour
    if 'color' or 'colour' in name_lower:
        color_attr = attr
```

**启发式回退**：当常规匹配找不到位置属性时，扩展会进行启发式推断 — 查找第一个不属于已知语义（法线/UV/颜色/切线等）且分量数 ≥ 3、类型为 Float 的属性：

```python
if pos_attr is None:
    for attr in attrs:
        if attr.format.compCount >= 3 and attr.format.compType == Float:
            if not any_known_semantic(attr.name):
                pos_attr = attr    # 启发式选择
```

**SV_POSITION 排除的原因**：`SV_POSITION` 是 HLSL/Direct3D 的系统值语义，表示顶点着色器输出的裁剪空间坐标（已经过 MVP 变换），不是我们需要的物体空间位置。RenderDoc 在 Vertex Input Layout 中可能同时列出输入语义和系统语义，必须区分。

### 4.4 索引缓冲区读取

索引缓冲区决定了三角面的顶点组成方式。

```python
# 从管线状态获取 IB 信息
ib = state.GetIBuffer()
# ib.resourceId  — 索引缓冲区的 GPU 资源 ID
# ib.byteStride  — 每个索引的字节宽度（2 = uint16, 4 = uint32）
# ib.byteOffset  — 缓冲区起始偏移

# 从 DrawCall 获取索引范围
# action.numIndices    — 本次 DrawCall 使用的索引数量
# action.indexOffset   — 索引起始偏移（以索引为单位，非字节）
# action.baseVertex    — BaseVertexLocation 偏移（GPU 在查找顶点时自动加上）
# action.flags         — 标志位，ActionFlags.Indexed 表示使用索引绘制
```

**读取流程：**

```python
# 读取整个索引缓冲区的原始字节
ib_data = controller.GetBufferData(ib.resourceId, 0, 0)

# 计算实际字节偏移
byte_offset = action.indexOffset * ib.byteStride + ib.byteOffset

# 逐个解码索引
for i in range(num_indices):
    off = byte_offset + i * ib.byteStride
    if ib.byteStride == 2:
        index = struct.unpack_from('<H', ib_data, off)[0]    # uint16
    elif ib.byteStride == 4:
        index = struct.unpack_from('<I', ib_data, off)[0]    # uint32
```

**非索引绘制**的情况下（`action.flags` 不含 `Indexed`），索引直接生成为连续序列 `[base, base+1, ..., base+N-1]`。

#### 4.4.1 baseVertex 偏移

`DrawIndexed` 系列 API（D3D11/D3D12/Vulkan）有一个 `BaseVertexLocation` 参数。**GPU 在使用索引查找顶点时会自动执行 `vertex = VB[index + baseVertex]`**。原始索引缓冲区中存储的是相对索引，必须加上 baseVertex 才能指向正确的顶点。

```python
# 应用 baseVertex 偏移
base_vertex = getattr(action, 'baseVertex', 0) or getattr(action, 'vertexOffset', 0) or 0
if base_vertex != 0:
    raw_indices = [idx + base_vertex for idx in raw_indices]
```

**不应用 baseVertex 的后果**：索引指向错误的顶点位置，导出模型出现碎片化三角面——三角面引用了不属于当前 DrawCall 的顶点数据，表现为模型表面有飞出的碎片。

#### 4.4.2 索引重映射（Index Remapping）

应用 baseVertex 后，`raw_indices` 中的值是顶点缓冲区的**绝对索引**。例如，一个 DrawCall 可能使用了 VB 中第 5000~8345 号顶点。如果直接读取 VB[0] 到 VB[8345]，会多读 5000 个无关顶点，导致：

- 导出的顶点数与实际使用量不一致
- UV 和位置数据无法正确对应
- 导出文件体积膨胀

**解决方案**：索引重映射——只收集 DrawCall 实际引用的唯一顶点，建立旧→新的紧凑映射：

```python
unique_verts_ordered = []  # 按首次出现顺序排列的唯一缓冲区索引
old_to_new = {}            # 旧缓冲区索引 → 新紧凑索引（0-based）

for old_idx in raw_indices:
    if old_idx not in old_to_new:
        old_to_new[old_idx] = len(unique_verts_ordered)
        unique_verts_ordered.append(old_idx)

# 最终的索引：紧凑的 0-based
indices = [old_to_new[idx] for idx in raw_indices]
```

**效果对比**（以实测 EID 320 为例）：

| | 修复前 | 修复后 |
|---|--------|--------|
| 导出顶点数 | 3543（0 到 max_index 全量读取） | 3346（仅 DrawCall 引用的唯一顶点） |
| UV 对应 | 错乱（多余顶点破坏对应关系） | 精确一一对应 |
| 碎片三角面 | 有（引用了无关顶点） | 无 |

### 4.5 顶点缓冲区读取与解码

这是整个 Model Extractor 中最复杂的部分。顶点缓冲区是一段连续的 GPU 内存，数据按 **交错布局（interleaved layout）** 或 **分离布局（separate buffers）** 存储。

**数据布局示意**（交错布局，单个 VB）：

```
VB0, stride=32:
┌─────────┬─────────┬────────┐
│ Vertex 0 │ Vertex 1 │ Vertex 2 │ ...
├─────────┼─────────┼────────┤
│ [Position: 12B] [Normal: 12B] [UV: 8B] │ → 共 32 字节 = stride
└─────────┴─────────┴────────┘
```

**读取一个属性的流程（v2.1 改进版）：**

```python
def read_vertex_attr(attr, num_components=None):
    """
    按 unique_verts_ordered 中记录的缓冲区索引精确读取顶点属性数据。
    返回的列表按 unique_verts_ordered 的顺序排列，与重映射后的 indices 一一对应。

    关键改进（v2.1）：
    - 始终按属性原始 compCount 读取，避免 4 分量属性读 3 分量时的跨步对齐错误
    - 只读取 DrawCall 实际引用的顶点（不再全量扫描 0 到 max_index）
    - 支持 R10G10B10A2 打包格式
    """
    vb_idx = attr.vertexBuffer
    vb_info = vbs[vb_idx]
    stride = vb_info.byteStride

    buf_data = controller.GetBufferData(vb_info.resourceId, 0, 0)

    fmt = attr.format
    raw_comp_count = fmt.compCount        # 原始分量数
    wanted_comp = num_components or raw_comp_count  # 调用方请求的分量数

    results = []
    for vi in unique_verts_ordered:       # 只遍历实际使用的顶点
        offset = vb_info.byteOffset + attr.byteOffset + vi * stride
        raw = struct.unpack_from(format_string, buf_data, offset)

        # 截取或填充到 wanted_comp
        if len(raw) > wanted_comp:
            vals = raw[:wanted_comp]
        elif len(raw) < wanted_comp:
            vals = raw + (0.0,) * (wanted_comp - len(raw))

        # 清理 NaN / Inf / 异常大值
        cleaned = [0.0 if not math.isfinite(v) or abs(v) > 1e9 else v for v in vals]
        results.append(tuple(cleaned))

    return results
```

**v2.1 关键改进 — 始终按原始分量数读取：**

许多 GPU 格式是 4 分量的（如法线用 `R8G8B8A8_SNorm`），但我们只需要 3 个分量。旧版本直接 `unpack('<3b', ...)`，导致**字节对齐偏移**——每个顶点少读 1 字节，后续数据全部错位。新版本始终按原始 `compCount` 读取完整数据，再截取需要的分量：

```python
raw_comp_count = fmt.compCount    # 例如 4（R8G8B8A8）
wanted_comp = 3                   # 我们只需要 xyz
unpack_fmt = f'<{raw_comp_count}{char}'  # '<4b' 而非 '<3b'
raw = struct.unpack_from(unpack_fmt, buf_data, offset)
vals = raw[:wanted_comp]          # 截取前 3 分量
```

**支持的数据类型矩阵：**

| compType | compByteWidth | struct 格式 | 后处理                            |
| -------- | ------------- | --------- | ------------------------------ |
| Float    | 2             | `e`       | 直接使用（float16/half）             |
| Float    | 4             | `f`       | 直接使用（float32）                  |
| Float    | 8             | `d`       | 直接使用（float64/double）           |
| UInt     | 1/2/4         | `B/H/I`   | 直接使用                           |
| SInt     | 1/2/4         | `b/h/i`   | 直接使用                           |
| UNorm    | 1/2/4         | `B/H/I`   | 除以 `(2^bits - 1)` → [0,1]      |
| SNorm    | 1/2/4         | `b/h/i`   | 除以 `(2^(bits-1) - 1)` → [-1,1] |

**UNorm / SNorm 归一化**：这是 GPU 常用的压缩格式，用整数表示 [0,1] 或 [-1,1] 范围的浮点值，可以节省带宽和内存。例如法线经常用 SNorm8x4 存储（4 字节表示 3 分量法线 + padding），扩展需要将其反归一化为浮点数。

#### 4.5.1 R10G10B10A2 打包格式支持

**v2.1 新增。** `R10G10B10A2` 是一种 4 分量但仅占 4 字节的打包格式——4 个分量挤在一个 `uint32` 中（10+10+10+2 位）。常用于存储法线、切线等单位向量（10 位精度足以表达方向信息，比 `float16` 更省空间）。

```python
# 检测 R10G10B10A2 格式
is_r10g10b10a2 = False
# 方式1: RenderDoc 的 SpecialFormat 枚举
if special == rd.SpecialFormat.R10G10B10A2:
    is_r10g10b10a2 = True
# 方式2: 启发式 — 4 分量但 compByteWidth=0（标记为打包格式）
if not is_r10g10b10a2 and raw_comp_count == 4 and fmt.compByteWidth == 0:
    is_r10g10b10a2 = True

# 解包 10+10+10+2 位
packed = struct.unpack_from('<I', buf_data, offset)[0]
r = (packed >>  0) & 0x3FF    # 低 10 位
g = (packed >> 10) & 0x3FF    # 中 10 位
b = (packed >> 20) & 0x3FF    # 高 10 位
a = (packed >> 30) & 0x3      # 最高 2 位

# SNorm 模式：10 位有符号 → [-1, 1]
if is_signed:
    if r >= 512: r -= 1024    # 二进制补码
    if g >= 512: g -= 1024
    if b >= 512: b -= 1024
    vals = (r / 511.0, g / 511.0, b / 511.0, a / 1.0)
# UNorm 模式：[0, 1]
else:
    vals = (r / 1023.0, g / 1023.0, b / 1023.0, a / 3.0)
```

#### 4.5.2 分量数截取与填充

调用方请求的分量数 (`wanted_comp`) 可能与属性原始分量数 (`raw_comp_count`) 不同。规则：

- **原始 > 请求**：截取前 N 个分量（如 4 分量法线截取前 3 个 xyz）
- **原始 < 请求**：用 `0.0` 填充（如 1 分量属性读 3 分量时补两个零）
- **相等**：直接使用

```python
if len(vals) > wanted_comp:
    vals = vals[:wanted_comp]
elif len(vals) < wanted_comp:
    vals = vals + (0.0,) * (wanted_comp - len(vals))
```

### 4.6 UV 多通道与 Unpack 机制

UV 坐标的存储方式因游戏而异，这是提取中最需要灵活处理的部分。

**标准情况**（大多数游戏）：

```
TEXCOORD0: float2 (R32G32_FLOAT)     → 一套 UV，直接使用
TEXCOORD1: float2 (R32G32_FLOAT)     → 第二套 UV
```

**打包情况**（部分游戏的优化手段）：

```
TEXCOORD0: float4 (R32G32B32A32_FLOAT)
  → xy 分量 = UV0
  → zw 分量 = UV1（两套 UV 打包进一个 float4）

TEXCOORD0: float3 (R32G32B32_FLOAT)
  → xy 分量 = UV
  → z 分量 = 额外数据（通常可忽略）
```

**UV Unpack 处理逻辑：**

```python
comp_count = attr.format.compCount

if comp_count <= 2:
    # 标准 2 分量 → 直接作为一套 UV
    uv_attrs.append((attr, start=0, count=2, channel=sem_idx))

elif unpack_uv and comp_count == 3:
    # 3 分量，取前 2 个分量（忽略第 3 分量）
    uv_attrs.append((attr, start=0, count=2, channel=sem_idx))

elif unpack_uv and comp_count >= 4:
    # 4 分量，拆分为两套 UV
    uv_attrs.append((attr, start=0, count=2, channel=sem_idx*2))      # xy → UV0
    uv_attrs.append((attr, start=2, count=2, channel=sem_idx*2+1))    # zw → UV1

elif not unpack_uv and comp_count > 2:
    # unpack 关闭，跳过非标准 UV
    skip
```

**读取时的子分量提取：**

```python
# 先读取属性的全部分量（如 float4 的 4 个分量）
raw_uvs = read_vertex_attr(attr, full_comp_count=4)

# 再提取需要的子分量
for uv in raw_uvs:
    u = uv[comp_start]        # 如 comp_start=2 → 取 z 分量
    v = uv[comp_start + 1]    # 取 w 分量
```

**UV 去重**：当 float4 拆分的两套 UV 数据完全相同时（说明 zw 只是 xy 的冗余副本），扩展会自动移除重复通道：

```python
if len(uv_sets) > 1:
    unique_uv_sets = [uv_sets[0]]
    for ui in range(1, len(uv_sets)):
        if uv_sets[ui] != any_existing:
            unique_uv_sets.append(uv_sets[ui])
```

### 4.7 坐标系变换与数据清洗

#### 4.7.1 DX 左手→右手坐标系转换

**v2.1 核心改进。** DX11/DX12 使用**左手坐标系**，而 OBJ/FBX/glTF/Unity/Blender 等使用**右手坐标系**。不做转换会导致**模型在 X 轴镜像**——例如角色左右腿的膝盖弯曲方向互换。

转换需要两步配合才能正确：

```
┌──────────────────────────────────┐
│  DX 左手坐标系 (Z-forward)         │
│  ┌─── X                          │
│  │    Y up                       │
│  │                               │
│  │ 面法线按 CW（顺时针）缠绕         │
└──────────────────────────────────┘
          │ 步骤 1: 位置 X 取负
          │ 步骤 2: 反转缠绕方向
          ▼
┌──────────────────────────────────┐
│  右手坐标系 (Z-forward)            │
│       X ───┐                     │
│        Y up                      │
│                                  │
│ 面法线按 CCW（逆时针）缠绕           │
└──────────────────────────────────┘
```

**步骤 1：位置 X 轴取负**

```python
for p in raw_positions:
    x, y, z = p[0], p[1], p[2]
    x = -x          # 镜像 X 轴
    positions.append((x, y, z))
```

**步骤 2：反转三角面缠绕方向**

翻转 X 后，所有三角面的面法线方向变反（朝内），导致 Unity 的 backface culling 把它们全部剔除。通过交换每个三角形的第 2、3 个顶点索引来反转缠绕方向：

```python
# 交换 v1 和 v2
i0, i1, i2 = indices[i], indices[i+1], indices[i+2]
# 导出为: i0, i2, i1（不是 i0, i1, i2）
```

**重要**：法线**不需要**取负 X。缠绕方向反转已经处理了面法线方向，顶点法线保持原始方向即可。这与经过实测验证的 MCP Bridge 导出行为一致。

**Flip UV V**：`flip_uv_v` 配置项控制是否执行 `v = 1.0 - v`。v2.1 将默认值从 `True` 改为 **`False`**。原因：DX11 抓帧的 UV 直接导出到 Unity/Blender 时不需要翻转，翻转反而会导致 UV 上下颠倒。用户如有特殊需求可在面板上手动勾选。

**Swap Y/Z**：DirectX 使用左手坐标系（Y 向上），部分工具使用右手坐标系（Z 向上，如 3ds Max 的世界坐标）：

```python
if swap_yz:
    y, z = z, y    # 位置和法线都需要交换
```

**数据清洗**：GPU 缓冲区中可能存在无效数据（NaN、Inf、异常大值），需要在导出前清理：

```python
for v in vals:
    if not math.isfinite(v) or abs(v) > 1e9:
        cleaned.append(0.0)    # 将无效值替换为 0
    else:
        cleaned.append(v)
```

**归一化位置数据检测**：某些游戏使用 SNorm/UNorm 格式存储位置数据（压缩到 [-1,1] 或 [0,1] 范围），扩展会检测并发出警告：

```python
if all_range <= 1.0 and pos_attr.format.compType in (SNorm, UNorm):
    print("[WARN] Position data appears to be in normalized format.")
    print("       The mesh may need manual scaling.")
```

#### 4.7.2 法线压缩编码检测

**v2.1 新增。** 许多游戏在 `NORMAL` 语义的顶点属性中**不存储法线向量**，而是存储**压缩编码的 tangent frame**。顶点着色器内部用位操作（`and`、`ubfe`、`utof`）从一个 `uint32` 中提取并解码出法线和切线数据。

**实测案例**（通过 RenderDoc MCP 逆向顶点着色器确认）：

```hlsl
// NORMAL 语义 (v2) 的 DXBC 反汇编：
// 这不是法线！着色器用位操作从中解码：
and r0.x, v2.x, l(1023)              // 取低 10 位
ubfe r0.zw, l(10,10), l(10,20), v2.x  // 取第 10-19 位、20-29 位
utof r0.xzw, r0.xxzw                  // 转 float
```

如果直接将这些原始值当作法线使用，得到的是巨大的整数值（远非 [-1, 1] 范围），导致 Unity 中光照完全错乱。

**检测与处理策略**：

```python
# 关键：必须在归一化之前检测！
# 归一化后所有值都在 [-1, 1]，检测永远不会触发
raw_max_abs = max(max(abs(n[0]), abs(n[1]), abs(n[2])) for n in raw_normals)

if raw_max_abs > 1.5:
    # 法线值远超 [-1, 1] 范围，是压缩编码数据
    print("[WARN] Normal data appears to be packed/encoded")
    print("       Discarding — Unity/Blender will recalculate normals from faces")
    normals = []    # 丢弃，让导入工具自行计算
```

**检测顺序至关重要**：压缩编码检测必须在归一化**之前**执行。如果先归一化（`nx / length`），任何向量都会被缩放到单位长度，`max_abs` 永远不超过 1.0，检测永远不触发——这是 v2.1 修复的一个关键 bug。

**有效法线的后续处理**：

```python
if not is_packed and raw_normals:
    for n in raw_normals:
        nx, ny, nz = n[0], n[1], n[2]
        # 归一化（压缩格式解码后可能不是精确单位长度）
        length = math.sqrt(nx*nx + ny*ny + nz*nz)
        if length > 1e-8:
            nx, ny, nz = nx/length, ny/length, nz/length
        else:
            nx, ny, nz = 0.0, 0.0, 1.0  # 退化法线用默认值
        normals.append((nx, ny, nz))
```

### 4.8 导出格式实现细节

#### OBJ 格式

Wavefront OBJ 是纯文本格式，结构简单：

```
v x y z         # 顶点位置（1-indexed）
vt u v          # UV 坐标
vn nx ny nz     # 法线
f v/vt/vn ...   # 面（引用上述索引，1-based）
```

**缠绕方向反转**（v2.1）：配合位置 X 取负，面索引交换 v1 和 v2：

```python
for i in range(0, len(indices) - 2, 3):
    i0, i1, i2 = indices[i], indices[i+1], indices[i+2]
    v0, v1, v2 = i0+1, i2+1, i1+1   # swap v1/v2，OBJ 是 1-based
    f.write(f"f {v0}/{v0}/{v0} {v1}/{v1}/{v1} {v2}/{v2}/{v2}\n")
```

**限制**：OBJ 标准只支持一套 UV。扩展在导出第一套 UV 的同时，将额外 UV 通道写入注释区域：

```
# EXTRA_UV_CHANNEL 1 (1024 vertices)
# vt1 0.500000 0.500000
```

#### PLY 格式

PLY（Polygon File Format）使用**二进制小端**编码，支持自定义属性头：

```
ply
format binary_little_endian 1.0
element vertex N
property float x
property float y
property float z
property float s      ← UV0.u
property float t      ← UV0.v
property float s1     ← UV1.u（多套 UV）
property float t1     ← UV1.v
element face M
property list uchar uint vertex_indices
end_header
[二进制顶点数据]
[二进制面数据]
```

多套 UV 通过自定义属性名（`s1/t1`, `s2/t2`...）支持。

**缠绕方向反转**（v2.1）：写入面数据时交换 v1 和 v2：

```python
# 原始: indices[i], indices[i+1], indices[i+2]
# 反转: indices[i], indices[i+2], indices[i+1]
face_data.extend(struct.pack('<3I', indices[i], indices[i+2], indices[i+1]))
```

#### glTF 格式

glTF 2.0 由 JSON 描述文件 + 二进制数据文件组成：

```
.gltf (JSON)
├── asset        — 元信息
├── scenes/nodes — 场景层级
├── meshes       — 网格定义
│   └── primitives
│       ├── attributes: { POSITION: 1, NORMAL: 2, TEXCOORD_0: 3, TEXCOORD_1: 4 }
│       └── indices: 0
├── accessors    — 数据访问器（描述如何从 bufferView 中读取数据）
├── bufferViews  — 缓冲区视图（描述 buffer 中的数据区间）
└── buffers      — 引用 .bin 文件

.bin (Binary)
├── [索引数据]     — UNSIGNED_INT, SCALAR
├── [位置数据]     — FLOAT, VEC3 (含 min/max 包围盒)
├── [法线数据]     — FLOAT, VEC3
├── [UV0 数据]    — FLOAT, VEC2
└── [UV1 数据]    — FLOAT, VEC2
```

多套 UV 通过标准属性名 `TEXCOORD_0`, `TEXCOORD_1`, ... 原生支持。

**数据对齐**：glTF 规范要求 bufferView 的 `byteOffset` 对齐到 4 字节边界：

```python
while len(bin_data) % 4 != 0:
    bin_data += b'\x00'    # 填充对齐
```

**缠绕方向反转**（v2.1）：写入索引数据时交换 i1 和 i2：

```python
for i in range(0, len(indices) - 2, 3):
    # 反转: i0, i2, i1（不是 i0, i1, i2）
    idx_buf += struct.pack('<3I', indices[i], indices[i+2], indices[i+1])
```

#### CSV 格式

CSV 格式将 mesh 数据拆分为三个文件，方便外部工具（如 Python 脚本）处理：

```
_vertices.csv:  vx,vy,vz,nx,ny,nz,u,v,u1,v1,cr,cg,cb,ca
_indices.csv:   i0,i1,i2
_meta.json:     元数据（顶点数、面数、属性标志等）
```

多套 UV 在 CSV 头部用 `u,v`（第一套）和 `u1,v1`（后续套）区分。

**缠绕方向反转**（v2.1）：面索引也交换 v1 和 v2：

```python
# _indices.csv 中每行写入: i0, i2, i1
f.write(f"{indices[i]},{indices[i+2]},{indices[i+1]}\n")
```

#### FBX 格式

FBX 7.4 ASCII 是最复杂的导出格式，需要构建完整的 FBX 文档结构：

```
FBXHeaderExtension  — 文件头（版本、创建时间、生成器信息）
GlobalSettings      — 全局设置（坐标轴、单位等）
Documents           — 文档列表
Definitions         — 对象类型模板定义
Objects             — 实际对象
├── Geometry        — 几何体数据
│   ├── Vertices    — 顶点位置（flat float array）
│   ├── PolygonVertexIndex — 面索引（FBX 约定）
│   ├── Edges       — 边列表（Unity 导入必需）
│   ├── LayerElementNormal — 法线层
│   ├── LayerElementUV: 0  — UV 层 0
│   ├── LayerElementUV: 1  — UV 层 1（多套 UV）
│   └── Layer       — 层定义（关联各 LayerElement）
├── Model           — 模型节点（变换信息）
└── Material        — 默认材质
Connections         — 对象关系连接
```

**FBX 索引约定**：FBX 使用特殊的索引编码 — 每个多边形的最后一个索引取**负数减一**。v2.1 同时反转缠绕方向（交换 i1/i2）：

```python
# 三角面 [0, 1, 2]：
#   缠绕反转 → [0, 2, 1]
#   FBX 编码 → [0, 2, -(1+1)] = [0, 2, -2]
for fi in range(num_faces):
    i0, i1, i2 = indices[fi*3], indices[fi*3+1], indices[fi*3+2]
    fbx_indices.extend([i0, i2, -(i1 + 1)])  # swap i1/i2
```

**Edge 生成**（Unity 导入必需）：FBX 的 Edges 数组存储每条边在 `PolygonVertexIndex` 中的起始索引位置，且每条边只出现一次：

```python
def _generate_edges(indices, num_faces):
    edge_set = set()    # 用于去重
    edges = []
    for fi in range(num_faces):
        base = fi * 3
        tri = [indices[fi*3], indices[fi*3+1], indices[fi*3+2]]
        for j in range(3):
            v0, v1 = tri[j], tri[(j+1) % 3]
            edge_key = (min(v0, v1), max(v0, v1))    # 无向边标识
            if edge_key not in edge_set:
                edge_set.add(edge_key)
                edges.append(base + j)    # 在 PolygonVertexIndex 中的位置
    return edges
```

**法线和 UV 展开顺序**（v2.1 关键修复）：FBX 的法线和 UV 使用 `ByPolygonVertex` 映射，数据按面展开。**展开顺序必须与 PolygonVertexIndex 的缠绕反转一致**——即 (i0, i2, i1)，而非原始的 (i0, i1, i2)：

```python
# 法线展开（ByPolygonVertex / Direct）
for fi in range(num_faces):
    i0, i1, i2 = indices[fi*3], indices[fi*3+1], indices[fi*3+2]
    for idx in [i0, i2, i1]:    # 与缠绕反转一致：i0, i2, i1
        face_normals.extend(normals[idx])

# UV 索引展开（ByPolygonVertex / IndexToDirect）
for fi in range(num_faces):
    i0, i1, i2 = indices[fi*3], indices[fi*3+1], indices[fi*3+2]
    uv_indices.extend([i0, i2, i1])  # 与缠绕反转一致
```

如果法线/UV 展开顺序与 PolygonVertexIndex 不一致，Unity 导入后法线和 UV 会错位——三角面的第 2 个顶点的法线被赋给了第 3 个顶点。

**多套 UV 的 FBX 表示**：FBX 通过 Layer 机制支持多套 UV。Layer 0 包含法线、第一套 UV 和材质。每个额外的 UV 通道放在独立的 Layer 中：

```
Layer: 0            → Normal (TypedIndex: 0) + UV (TypedIndex: 0) + Material
Layer: 1            → UV (TypedIndex: 1)      ← 第二套 UV
Layer: 2            → UV (TypedIndex: 2)      ← 第三套 UV
```

**浮点数格式化**：FBX 文件中的浮点数使用固定小数格式（`{v:.6f}`），而非科学计数法。某些解析器（包括 Unity 的 FBX 导入器）对科学计数法支持不佳。

---

## 五、核心数据结构

### mesh_data 字典（Model Extractor 的中间表示）

```python
mesh_data = {
    "name":      str,              # DrawCall 名称
    "event_id":  int,              # Event ID
    "positions": [(x, y, z), ...], # 顶点位置列表（物体空间）
    "normals":   [(nx, ny, nz), ...],  # 法线列表（可能为空）
    "uvs":       [(u, v), ...],    # 第一套 UV（向后兼容）
    "uv_sets":   [                 # 所有 UV 通道
        [(u, v), ...],             #   UV channel 0
        [(u, v), ...],             #   UV channel 1
        ...
    ],
    "colors":    [(r, g, b, a), ...],  # 顶点色（可能为空）
    "indices":   [int, ...],       # 三角面索引（flat list, 每3个一组）
}
```

各导出格式函数（`export_obj`, `export_ply` 等）统一接受此字典作为输入，实现了**提取与导出的解耦**。

### _config 字典（配置）

配置使用全局字典存储，UI 面板和导出函数共享。UI 操作完成后通过 `_config.update(config)` 同步到全局状态，下次打开面板时自动恢复上次的设置。

---

## 六、API 兼容性处理

RenderDoc 在 v1.30+ 版本中对 Python API 进行了重大改动，主要影响资源绑定的查询方式。

### 资源绑定 API 变更

| 功能                   | 旧版 API（v1.29-）                                           | 新版 API（v1.30+）                                   |
| -------------------- | -------------------------------------------------------- | ------------------------------------------------ |
| GetReadOnlyResources | 返回 `BoundResourceArray` 列表，内含 `.resources[i].resourceId` | 返回 `UsedDescriptor` 列表，内含 `.descriptor.resource` |
| GetOutputTargets     | 返回带 `.resourceId` 字段的对象                                  | 返回 `Descriptor` 对象，字段名为 `.resource`              |
| GetDepthTarget       | 返回带 `.resourceId` 字段的对象                                  | 返回 `Descriptor` 对象，字段名为 `.resource`              |

**兼容策略**：通过 `_extract_resource_id()` 函数使用 `hasattr` 进行运行时检测：

```python
def _extract_resource_id(obj):
    if hasattr(obj, 'resource'):      # 新版 API
        return obj.resource
    if hasattr(obj, 'resourceId'):    # 旧版 API
        return obj.resourceId
    return rd.ResourceId.Null()
```

对于 `GetReadOnlyResources` 返回的列表项，进一步检测其结构：

```python
for item in ro_resources:
    if hasattr(item, 'descriptor'):       # 新版: UsedDescriptor
        rid = _extract_resource_id(item.descriptor)
    elif hasattr(item, 'resources'):      # 旧版: BoundResourceArray
        for res in item.resources:
            rid = _extract_resource_id(res)
    else:
        rid = _extract_resource_id(item)  # 直接尝试
```

---

## 七、错误处理与容错机制

### 多层 try-except 保护

两个扩展都采用**多层异常捕获**策略，确保单个操作的失败不会导致整个扩展崩溃：

```
菜单回调函数
└── try-except (最外层：防止菜单回调异常导致 RenderDoc 崩溃)
    └── BlockInvoke 内部回调
        └── try-except (回放线程层：捕获 GPU 数据访问异常)
            └── 单个 DrawCall / 贴图处理
                └── try-except (单项层：捕获单个资源的错误，跳过继续)
```

### 容错处理清单

| 场景                      | 处理方式                           |
| ----------------------- | ------------------------------ |
| Capture 未加载             | 弹出 ErrorDialog 提示用户，终止操作       |
| 顶点缓冲区读取失败               | 打印警告，返回空列表，跳过该属性               |
| 索引缓冲区读取失败               | 回退到生成连续索引 `[0, 1, 2, ...]`     |
| 找不到位置属性                 | 启发式搜索；仍找不到则跳过该 DrawCall        |
| 找不到 UV 属性               | 第二轮宽松匹配（查找所有 2 分量 / float4 属性） |
| 当前事件不是 DrawCall         | 自动查找最近的 DrawCall（按 eventId 距离） |
| 浮点数 NaN / Inf / 异常大值    | 替换为 0.0                        |
| UV 通道数与位置数不一致           | 打印警告，该 UV 通道在导出时可能被丢弃          |
| 文件名冲突                   | 自动追加 `_1`, `_2` 等后缀            |
| 不支持的顶点格式（compType未知）    | 尝试 float16 回退，或跳过并打印警告         |
| SNorm/UNorm 位置数据        | 检测并打印警告，提示用户可能需要手动缩放           |
| GetReadOnlyResources 异常 | 捕获并继续检查下一个着色器阶段                |
| 法线数据为压缩编码（v2.1 新增）    | 检测 max\|value\| > 1.5 后自动丢弃，让导入工具重算 |
| R10G10B10A2 格式（v2.1 新增）| 自动检测并正确解包 10+10+10+2 位         |
| 属性分量数不足（v2.1 新增）      | 用 0.0 填充到请求的分量数                |

### 调试信息输出

两个扩展都会向 RenderDoc 的 Python Output 控制台输出详细的调试信息，使用分级标签：

```
[DEBUG]  — 详细的中间数据（属性列表、数值范围、资源 ID 集合）
[INFO]   — 关键决策结果（属性映射、UV 去重）
[WARN]   — 可恢复的异常情况（属性缺失、数据不一致）
[ERROR]  — 不可恢复的错误
```

---

*本文档基于 Texture Exporter v1.3 和 Model Extractor v2.1 的源代码编写。*

---

## 八、v2.1 修复记录

Model Extractor v2.1 针对模型导出质量进行了全面修复，确保从 DX11 抓帧导出的模型在 Unity/Blender 中完全正确。以下为完整修复清单：

### 8.1 索引系统重构

| Bug | 影响 | 修复 |
|-----|------|------|
| 缺少 `baseVertex` 偏移 | 索引指向错误顶点，模型有碎片三角面飞出 | 索引读取后应用 `action.baseVertex` |
| 顶点全量读取（0 到 max_index） | 多余顶点破坏 UV/位置对应关系，导出顶点数不对 | 引入索引重映射，只读取 DrawCall 引用的唯一顶点 |

### 8.2 顶点属性读取修复

| Bug | 影响 | 修复 |
|-----|------|------|
| 按请求分量数读取（如 3 分量读 4 分量属性） | 字节对齐偏移，后续所有顶点数据错位 | 始终按原始 `compCount` 读取再截取 |
| 不支持 R10G10B10A2 格式 | 把 uint32 打包数据当其他格式解析，数值完全错误 | 新增 R10G10B10A2 检测与 10+10+10+2 位解包 |
| 属性原始分量数 < 请求分量数 | IndexError 访问越界 | 不足时用 0.0 填充 |

### 8.3 坐标系与法线修复

| Bug | 影响 | 修复 |
|-----|------|------|
| 未做 DX→右手坐标系转换 | 模型 X 轴镜像（左右腿互换） | 位置 X 取负 + 所有格式缠绕方向反转 |
| `flip_uv_v` 默认 True | DX11 导出 UV 上下颠倒 | 默认改为 False |
| 法线压缩编码未检测 | 压缩 tangent frame 被当法线用，光照错乱 | 归一化前检测 max\|value\| > 1.5 自动丢弃 |
| 压缩编码检测在归一化后（致命 bug） | 归一化后检测永远不触发 | 检测移到归一化之前 |
| 法线多余的 X 取负 | 法线方向错误 | 移除 `nx = -nx`（缠绕反转已处理面法线方向） |

### 8.4 导出格式一致性

所有 5 种导出格式（OBJ/PLY/glTF/CSV/FBX）均已同步应用缠绕方向反转。FBX 格式额外修复了：

- **法线展开顺序**：从 (i0, i1, i2) 改为 (i0, i2, i1)，与 PolygonVertexIndex 一致
- **UV 索引展开顺序**：同上

### 8.5 验证方法

使用 RenderDoc MCP Bridge 的 `export_to_unity` 工具（已验证正确）作为基准，逐项对齐 model_extractor 的行为。最终两个工具导出的模型在 Unity 中表现一致。
