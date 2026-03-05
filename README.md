# RenderDoc 扩展工具集 — 安装与使用文档

> 本文档涵盖 **Texture Exporter（贴图导出扩展）** 和 **Model Extractor（模型提取扩展）** 的完整安装与使用说明。

---

## 目录

- [一、环境要求](#一环境要求)
- [二、项目文件结构](#二项目文件结构)
- [三、安装步骤](#三安装步骤)
- [四、Texture Exporter — 贴图导出扩展](#四texture-exporter--贴图导出扩展)
  - [4.1 功能概览](#41-功能概览)
  - [4.2 使用方法](#42-使用方法)
  - [4.3 导出设置参数](#43-导出设置参数)
  - [4.4 输出文件命名规则](#44-输出文件命名规则)
  - [4.5 支持的导出格式](#45-支持的导出格式)
- [五、Model Extractor — 模型提取扩展](#五model-extractor--模型提取扩展)
  - [5.1 功能概览](#51-功能概览)
  - [5.2 使用方法](#52-使用方法)
  - [5.3 导出设置参数](#53-导出设置参数)
  - [5.4 支持的导出格式](#54-支持的导出格式)
  - [5.5 输出文件结构](#55-输出文件结构)
- [六、常见问题](#六常见问题)
- [七、版本信息](#七版本信息)

---

## 一、环境要求

| 项目            | 要求                                 |
| ------------- | ---------------------------------- |
| **RenderDoc** | v1.2 或更高版本（推荐 v1.30+，完整支持新版 API）   |
| **Python 3**  | 用于运行安装脚本（RenderDoc 自带 Python 环境即可） |
| **操作系统**      | Windows / macOS / Linux            |

---

## 二、项目文件结构

```
项目根目录/
├── install_extension.py          # 自动安装脚本
├── texture_exporter/             # 贴图导出扩展
│   ├── __init__.py               # 扩展主代码
│   └── extension.json            # 扩展描述文件
├── model_extractor/              # 模型提取扩展
│   ├── __init__.py               # 扩展主代码
│   └── extension.json            # 扩展描述文件
└── F_Texture_Exporter_使用说明.md  # 本文档
```

---

## 三、安装步骤

### 方法一：自动安装（推荐）

1. 打开终端/命令提示符，进入项目根目录：
   
   ```bash
   cd path/to/plugin   # 项目根目录
   ```

2. 运行安装脚本：
   
   ```bash
   # 安装所有扩展（Texture Exporter + Model Extractor）
   python install_extension.py
   
   # 仅安装贴图导出扩展
   python install_extension.py --ext texture_exporter
   
   # 仅安装模型提取扩展
   python install_extension.py --ext model_extractor
   ```

3. 如需指定自定义安装目录：
   
   ```bash
   python install_extension.py --target "C:\自定义路径\extensions"
   ```
   
   > 脚本会自动检测 RenderDoc 扩展目录：
   > 
   > - **Windows**: `%APPDATA%\qrenderdoc\extensions\`
   > - **macOS**: `~/Library/Application Support/qrenderdoc/extensions/`
   > - **Linux**: `~/.local/share/qrenderdoc/extensions/`

4. 安装完成后，**重启 RenderDoc**。

### 方法二：手动安装

1. 将 `texture_exporter` 和/或 `model_extractor` 文件夹（每个文件夹包含 `__init__.py` 和 `extension.json`）整个复制到 RenderDoc 的扩展目录中。

2. **重启 RenderDoc**。

### 启用扩展

1. 打开 RenderDoc，进入菜单 **Tools → Manage Extensions**。

2. 在扩展列表中找到 **Texture Exporter** 和/或 **Model Extractor**，勾选启用。

3. 扩展加载成功后，RenderDoc 的 Python 输出控制台会显示：
   
   ```
   [Texture Exporter] Extension loaded (v1.3)
   [Model Extractor] Extension loaded (v2.1 - multi-UV support)
   ```

---

## 四、Texture Exporter — 贴图导出扩展

### 4.1 功能概览

从 RenderDoc 抓帧文件中批量导出贴图资源，支持按事件筛选、按名称过滤、多格式输出。

安装启用后，**Tools** 菜单中会出现 **Texture Exporter** 子菜单：

| 菜单项                               | 说明                           |
| --------------------------------- | ---------------------------- |
| **Open Quick Panel**              | 打开可停靠的快捷操作面板（集成所有功能）         |
| **Export All Textures**           | 导出当前 Capture 中的所有贴图（弹出设置对话框） |
| **Export Current Event Textures** | 仅导出当前选中 Event/DrawCall 绑定的贴图 |
| **List All Textures (Console)**   | 在控制台列出所有贴图的详细信息和统计摘要         |

### 4.2 使用方法

#### 基本流程

1. 在 RenderDoc 中**加载一个 Capture 文件**（.rdc）。
2. 通过 **Tools → Texture Exporter** 选择对应功能。
3. 在弹出的设置面板中配置导出参数。
4. 点击 **Export...** 按钮，选择输出目录。
5. 等待导出完成，弹窗显示导出结果。

#### Export All Textures（导出所有贴图）

- 菜单路径：**Tools → Texture Exporter → Export All Textures**
- 导出 Capture 中包含的**全部**贴图资源。
- 适用于批量提取整个场景的贴图。

#### Export Current Event Textures（导出当前事件贴图）

- 菜单路径：**Tools → Texture Exporter → Export Current Event Textures**
- 仅导出当前选中的 DrawCall/Event 绑定的贴图（包括 SRV、UAV、Render Target、Depth Target）。
- **使用前请先在 Event Browser 中选中目标 DrawCall**。
- 扩展会自动通过管线状态收集该事件使用的所有贴图资源 ID。

#### Quick Panel（快捷面板）

- 菜单路径：**Tools → Texture Exporter → Open Quick Panel**
- 打开一个**可停靠**的面板窗口，集成所有导出选项和操作按钮。
- 面板可拖拽停靠到 RenderDoc 界面的任意位置，方便频繁操作。
- 面板内包含三个按钮：
  - **Export All Textures** — 导出全部贴图
  - **Export Event Textures** — 导出当前事件贴图
  - **List Textures (Console)** — 列出贴图信息

#### List All Textures（列出贴图信息）

- 菜单路径：**Tools → Texture Exporter → List All Textures (Console)**
- 在 Python Output 控制台打印所有贴图的详细列表，并弹窗显示统计摘要。
- 统计内容包括：贴图总数、类型分布、尺寸分布、格式分布（Top 10）。

### 4.3 导出设置参数

| 参数                             | 说明                                                    | 默认值    |
| ------------------------------ | ----------------------------------------------------- | ------ |
| **Keep original format (DDS)** | 勾选后使用 DDS 格式保存，保持原始压缩格式（BC/ASTC 等不丢失）                 | 开启     |
| **Convert Format**             | 不勾选 "Keep original" 时可选转换格式：PNG、BMP、TGA、HDR、EXR、DDS   | DDS    |
| **Min Size**                   | 最小尺寸过滤，宽高均小于此值的贴图会被跳过                                 | 2      |
| **Name Filter**                | 名称过滤，只导出名称中包含该关键字的贴图（不区分大小写）                          | 空（不过滤） |
| **All mip levels**             | 导出所有 Mipmap 级别                                        | 关闭     |
| **CubeMap faces**              | 导出 CubeMap 的 6 个面（PosX, NegX, PosY, NegY, PosZ, NegZ） | 开启     |
| **3D slices**                  | 导出 3D 纹理的各个深度切片                                       | 开启     |

### 4.4 输出文件命名规则

导出的文件名格式为：

```
{贴图名称}_{宽}x{高}_{原始格式}[_mip{N}][_面/切片].{扩展名}
```

示例：

```
Albedo_Texture_2048x2048_BC7_UNORM.dds
Albedo_Texture_2048x2048_BC7_UNORM_mip0.dds
CubeSky_1024x1024_R16G16B16A16_FLOAT_PosX.dds
Volume_Fog_128x128_R8G8B8A8_UNORM_slice5.dds
```

导出完成后，输出目录下还会生成 `_export_log.txt` 日志文件，记录导出统计信息。

### 4.5 支持的导出格式

| 格式      | 说明                                        |
| ------- | ----------------------------------------- |
| **DDS** | DirectDraw Surface，保持 GPU 原始压缩格式，推荐用于引擎资产 |
| **PNG** | 无损压缩，适合通用查看和编辑                            |
| **TGA** | Targa 格式，游戏行业常用                           |
| **BMP** | 无压缩位图                                     |
| **HDR** | 高动态范围格式，适合 HDRI / 光照贴图                    |
| **EXR** | OpenEXR 高精度格式，适合 VFX / 合成流程               |

---

## 五、Model Extractor — 模型提取扩展

### 5.1 功能概览

从 RenderDoc 抓帧文件的 DrawCall 中提取 mesh（网格）数据，并导出为多种 3D 模型格式。支持单个 DrawCall 提取和批量导出。

安装启用后，**Tools** 菜单中会出现 **Model Extractor** 子菜单：

| 菜单项                              | 说明                         |
| -------------------------------- | -------------------------- |
| **Extract Current Draw Call**    | 提取当前选中的 DrawCall 的 mesh 数据 |
| **Batch Extract All Draw Calls** | 批量提取所有 DrawCall 的 mesh 数据  |
| **List Draw Calls (Console)**    | 在控制台列出所有 DrawCall 信息       |

### 5.2 使用方法

#### 基本流程

1. 在 RenderDoc 中**加载一个 Capture 文件**（.rdc）。
2. 在 **Event Browser** 中选中一个 DrawCall 事件（如需提取单个模型）。
3. 通过 **Tools → Model Extractor** 选择对应功能。
4. 在弹出的设置面板中配置导出参数。
5. 点击 **Select Directory & Extract...** 按钮，选择输出目录。
6. 等待导出完成，弹窗显示导出结果（顶点数、面数、UV 通道数等）。

#### Extract Current Draw Call（提取当前 DrawCall）

- 菜单路径：**Tools → Model Extractor → Extract Current Draw Call**
- 提取当前选中的 DrawCall 的 mesh 数据并导出为文件。
- 如果当前选中的不是 DrawCall，会自动查找最近的 DrawCall。
- 导出完成后弹窗显示详细信息：Event ID、文件名、顶点数、面数、法线、UV 通道数。

#### Batch Extract All Draw Calls（批量提取）

- 菜单路径：**Tools → Model Extractor → Batch Extract All Draw Calls**
- 遍历 Capture 中所有 DrawCall，逐一提取 mesh 并导出。
- 导出文件按格式归类到对应子文件夹（如 `fbx/`、`obj/` 等）。
- 导出完成后弹窗显示统计：导出数、跳过数、错误数。
- 输出目录下会生成 `_export_log.txt` 日志文件。

#### List Draw Calls（列出 DrawCall）

- 菜单路径：**Tools → Model Extractor → List Draw Calls (Console)**
- 在 Python Output 控制台列出所有 DrawCall 的详细信息，并弹窗显示摘要。
- 列出内容：Event ID、索引数、实例数。

### 5.3 导出设置参数

| 参数                                  | 说明                                                                 | 默认值 |
| ----------------------------------- | ------------------------------------------------------------------ | --- |
| **Export Format**                   | 导出格式，可选：OBJ、PLY、glTF、CSV、FBX                                       | OBJ |
| **Export normals**                  | 导出法线数据                                                             | 开启  |
| **Export UVs**                      | 导出 UV 坐标                                                           | 开启  |
| **Export vertex colors (PLY only)** | 导出顶点色（仅 PLY 格式支持）                                                  | 关闭  |
| **Flip UV V coordinate**            | 翻转 UV 的 V 坐标（DX → OpenGL 约定转换）                                     | 开启  |
| **UV unpack**                       | UV 增强识别：float4 自动拆分为两套 UV（xy + zw），float3 取前 2 分量。关闭则只识别标准 2 分量 UV | 关闭  |
| **Swap Y/Z axis**                   | 交换 Y/Z 轴（用于坐标系转换）                                                  | 关闭  |
| **Scale Factor**                    | 缩放因子                                                               | 1.0 |

#### UV unpack 选项详解

- **开启时**：
  - 3 分量 UV 属性（UVW）→ 自动取前 2 分量作为 UV
  - 4 分量 UV 属性 → 自动拆分为两套 UV（xy = UV0，zw = UV1）
  - 适用于某些游戏将多套 UV 打包到单个高分量属性中的情况
- **关闭时**：
  - 只识别标准 2 分量的 UV 属性
  - 3 分量及以上的 UV 属性会被跳过
  - 适用于 UV 属性格式规范、不需要拆分的场景

### 5.4 支持的导出格式

| 格式       | 扩展名          | 说明                                                                  |
| -------- | ------------ | ------------------------------------------------------------------- |
| **OBJ**  | .obj         | Wavefront OBJ，通用 3D 格式。仅支持 1 套 UV，多套 UV 写入注释区                       |
| **PLY**  | .ply         | 二进制 PLY（little-endian），支持多套 UV 和顶点色                                 |
| **glTF** | .gltf + .bin | glTF 2.0 格式，支持多套 UV（TEXCOORD_0, TEXCOORD_1, ...）                    |
| **CSV**  | .csv + .json | 顶点/索引数据分别导出为 CSV，附带元数据 JSON。可配合外部脚本转换为 FBX                          |
| **FBX**  | .fbx         | FBX 7.4 ASCII 格式，完全兼容 Unity / Unreal / Blender / Maya。支持多套 UV、法线、材质 |

#### 格式选择建议

| 使用场景              | 推荐格式                       |
| ----------------- | -------------------------- |
| 导入 Unity / Unreal | **FBX**                    |
| 导入 Blender        | **FBX** 或 **glTF**         |
| 需要多套 UV           | **FBX**、**glTF** 或 **PLY** |
| 需要顶点色             | **PLY**                    |
| 通用兼容 / 快速查看       | **OBJ**                    |
| 数据分析 / 自定义处理      | **CSV**                    |

### 5.5 输出文件结构

导出文件按格式归类到子文件夹中：

```
输出目录/
├── fbx/
│   ├── EID1234_DrawCall_Name.fbx
│   ├── EID1235_DrawCall_Name.fbx
│   └── ...
├── _export_log.txt                    # 导出日志（批量模式）
```

单个 DrawCall 导出时，同样会在输出目录下按格式创建子文件夹。

CSV 格式会额外生成三个文件：

```
csv/
├── EID1234_DrawCall_Name_vertices.csv   # 顶点数据（位置、法线、UV、顶点色）
├── EID1234_DrawCall_Name_indices.csv    # 三角面索引
└── EID1234_DrawCall_Name_meta.json      # 元数据（顶点数、面数、属性信息）
```

glTF 格式会生成两个文件：

```
gltf/
├── EID1234_DrawCall_Name.gltf           # JSON 描述文件
└── EID1234_DrawCall_Name.bin            # 二进制数据
```

---

## 六、常见问题

### 通用问题

#### Q：扩展菜单没有出现？

1. 确认扩展文件夹已正确放置在 RenderDoc 扩展目录中（如 `%APPDATA%\qrenderdoc\extensions\texture_exporter\`）。
2. 确认每个扩展目录下包含 `__init__.py` 和 `extension.json` 两个文件。
3. 在 **Tools → Manage Extensions** 中确认扩展已勾选启用。
4. 重启 RenderDoc。

#### Q：导出报错或数据为空？

- 查看 RenderDoc 的 **Python Output** 面板（**Window → Python Output**），检查错误日志。
- 确保 Capture 文件完整且可正常回放。

### Texture Exporter 相关

#### Q：Export Current Event Textures 没有导出任何贴图？

- 请确保在 **Event Browser** 中已选中一个具体的 DrawCall 事件（而非 Marker / 组节点）。
- 检查 Python Output 控制台的 `[DEBUG]` 信息，确认是否找到了资源 ID。

#### Q：导出的 DDS 文件用普通图片查看器打不开？

- DDS 格式保留了 GPU 原始压缩格式（如 BC7、BC1、ASTC 等），需要专用工具查看：
  - **Windows**: 安装 [Microsoft DDS Viewer](https://apps.microsoft.com/detail/dds-viewer) 或使用 [Paint.NET](https://www.getpaint.net/) + DDS 插件
  - 也可在导出设置中取消勾选 **Keep original format**，改用 PNG 格式导出

### Model Extractor 相关

#### Q：导出的模型在 Unity / Blender 中看起来不对（朝向、缩放异常）？

- 尝试开启 **Swap Y/Z axis** 选项（DirectX 使用左手坐标系，部分引擎使用右手坐标系）。
- 调整 **Scale Factor**（某些游戏使用非标准单位）。
- 检查 Python Output 中的 `[DEBUG] Position range` 信息，确认位置数据范围是否合理。

#### Q：导出的模型没有 UV / UV 错乱？

- 确认 **Export UVs** 选项已开启。
- 如果贴图在模型上出现上下翻转，尝试切换 **Flip UV V coordinate** 选项。
- 如果游戏将多套 UV 打包到 float4 属性中，尝试开启 **UV unpack** 选项。
- 查看 Python Output 中的 `[DEBUG] UV range` 信息，确认 UV 值范围是否合理（通常在 0~1 之间）。

#### Q：批量导出时大量 DrawCall 被 Skipped？

- 被跳过的 DrawCall 通常是因为没有可读取的顶点数据（如清屏操作、全屏后处理 Pass 等）。
- 这是正常现象，并非所有 DrawCall 都对应有效的 3D 网格。

#### Q：导出的 OBJ 文件只有一套 UV，但游戏中有多套？

- OBJ 格式标准仅支持一套 UV。多套 UV 数据会写入文件注释区（以 `# vt1` 开头）。
- 如需完整的多套 UV 支持，请使用 **FBX**、**glTF** 或 **PLY** 格式导出。

---

## 七、版本信息

| 扩展                   | 当前版本 | 说明                                        |
| -------------------- | ---- | ----------------------------------------- |
| **Texture Exporter** | v1.3 | 支持 Quick Panel、按事件筛选、多格式导出                |
| **Model Extractor**  | v2.1 | 支持多套 UV、FBX/glTF/OBJ/PLY/CSV 导出、UV unpack |

兼容 RenderDoc：v1.2+（推荐 v1.30+，完整支持新版 API）
