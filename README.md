# Texture Exporter — RenderDoc 贴图导出扩展

## 安装与使用说明

---

## 一、环境要求

- **RenderDoc** v1.2 或更高版本（推荐 v1.30+）
- **Python 3** 环境（用于运行安装脚本）
- 操作系统：Windows / macOS / Linux

---

## 二、安装步骤

### 方法一：自动安装（推荐）

1. 打开终端/命令提示符，进入项目根目录：
   
   ```bash
   cd path/plugin #(插件所在目录)
   ```

2. 运行安装脚本：
   
   ```bash
   # 安装所有扩展（包括 Texture Exporter 和 Model Extractor）
   python install_extension.py
   
   # 仅安装贴图导出扩展
   python install_extension.py --ext texture_exporter
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

1. 将 `texture_exporter` 文件夹（包含 `__init__.py` 和 `extension.json`）整个复制到 RenderDoc 的扩展目录中。

2. 重启 RenderDoc。

### 启用扩展

1. 打开 RenderDoc，进入菜单 **Tools → Manage Extensions**。

2. 在扩展列表中找到 **Texture Exporter**，勾选启用。

3. 扩展加载成功后，RenderDoc 的 Python 输出控制台会显示：
   
   ```
   [Texture Exporter] Extension loaded (v1.3)
   ```

---

## 三、功能菜单

安装启用后，在 **Tools** 菜单中会出现 **Texture Exporter** 子菜单，包含以下功能：

| 菜单项                               | 说明                           |
| --------------------------------- | ---------------------------- |
| **Open Quick Panel**              | 打开可停靠的快捷操作面板                 |
| **Export All Textures**           | 导出当前 Capture 中的所有贴图（弹出设置对话框） |
| **Export Current Event Textures** | 仅导出当前选中 Event/DrawCall 绑定的贴图 |
| **List All Textures (Console)**   | 在控制台列出所有贴图的详细信息和统计摘要         |

---

## 四、使用方法

### 4.1 基本流程

1. 在 RenderDoc 中**加载一个 Capture 文件**（.rdc）。
2. 通过 **Tools → Texture Exporter** 选择对应功能。
3. 在弹出的设置面板中配置导出参数。
4. 点击 **Export...** 按钮，选择输出目录。
5. 等待导出完成，弹窗显示导出结果。

### 4.2 Export All Textures（导出所有贴图）

- 菜单路径：**Tools → Texture Exporter → Export All Textures**
- 导出 Capture 中包含的全部贴图资源。
- 适用于批量提取整个场景的贴图。

### 4.3 Export Current Event Textures（导出当前事件贴图）

- 菜单路径：**Tools → Texture Exporter → Export Current Event Textures**
- 仅导出当前选中的 DrawCall/Event 绑定的贴图（包括 SRV、UAV、Render Target、Depth Target）。
- **使用前请先在 Event Browser 中选中目标 DrawCall**。
- 扩展会自动通过管线状态收集该事件使用的所有贴图资源 ID。

### 4.4 Quick Panel（快捷面板）

- 菜单路径：**Tools → Texture Exporter → Open Quick Panel**
- 打开一个可停靠的面板窗口，集成所有导出选项和操作按钮。
- 面板可拖拽停靠到 RenderDoc 界面的任意位置，方便频繁操作。
- 面板内包含三个按钮：
  - **Export All Textures** — 导出全部贴图
  - **Export Event Textures** — 导出当前事件贴图
  - **List Textures (Console)** — 列出贴图信息

### 4.5 List All Textures（列出贴图信息）

- 菜单路径：**Tools → Texture Exporter → List All Textures (Console)**
- 在 Python Output 控制台打印所有贴图的详细列表，并弹窗显示统计摘要。
- 统计内容包括：贴图总数、类型分布、尺寸分布、格式分布（Top 10）。

---

## 五、导出设置参数

| 参数                             | 说明                                                    | 默认值    |
| ------------------------------ | ----------------------------------------------------- | ------ |
| **Keep original format (DDS)** | 勾选后使用 DDS 格式保存，保持原始压缩格式（BC/ASTC 等不丢失）                 | 开启     |
| **Convert Format**             | 不勾选"Keep original"时可选转换格式：PNG、BMP、TGA、HDR、EXR、DDS     | DDS    |
| **Min Size**                   | 最小尺寸过滤，宽高均小于此值的贴图会被跳过                                 | 2      |
| **Name Filter**                | 名称过滤，只导出名称中包含该关键字的贴图（不区分大小写）                          | 空（不过滤） |
| **Export all mip levels**      | 导出所有 Mipmap 级别                                        | 关闭     |
| **Export CubeMap faces**       | 导出 CubeMap 的 6 个面（PosX, NegX, PosY, NegY, PosZ, NegZ） | 开启     |
| **Export 3D texture slices**   | 导出 3D 纹理的各个深度切片                                       | 开启     |

---

## 六、输出文件命名规则

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

---

## 七、常见问题

### Q1：Export Current Event Textures 没有导出任何贴图？

- 请确保在 **Event Browser** 中已选中一个具体的 DrawCall 事件（而非 Marker/组节点）。
- 检查 Python Output 控制台的 `[DEBUG]` 信息，确认是否找到了资源 ID。

### Q2：导出的 DDS 文件用普通图片查看器打不开？

- DDS 格式保留了 GPU 原始压缩格式（如 BC7、BC1、ASTC 等），需要专用工具查看：
  - **Windows**: 安装 [Microsoft DDS Viewer](https://apps.microsoft.com/detail/dds-viewer) 或使用 [Paint.NET](https://www.getpaint.net/) + DDS 插件
  - 也可在导出设置中取消勾选 **Keep original format**，改用 PNG 格式导出

### Q3：扩展菜单没有出现？

1. 确认扩展文件正确放置在 `qrenderdoc/extensions/texture_exporter/` 目录。
2. 确认目录下包含 `__init__.py` 和 `extension.json` 两个文件。
3. 在 **Tools → Manage Extensions** 中确认扩展已勾选启用。
4. 重启 RenderDoc。

### Q4：导出报错或贴图数据为空？

- 查看 RenderDoc 的 **Python Output** 面板（Window → Python Output），检查错误日志。
- 确保 Capture 文件完整且可正常回放。

---

## 八、支持的导出格式

| 格式      | 说明                                        |
| ------- | ----------------------------------------- |
| **DDS** | DirectDraw Surface，保持 GPU 原始压缩格式，推荐用于引擎资产 |
| **PNG** | 无损压缩，适合通用查看和编辑                            |
| **TGA** | Targa 格式，游戏行业常用                           |
| **BMP** | 无压缩位图                                     |
| **HDR** | 高动态范围格式，适合 HDRI/光照贴图                      |
| **EXR** | OpenEXR 高精度格式，适合 VFX/合成流程                 |

---

## 九、版本信息

- 当前版本：**v0.1**
- 后续会持续更新完善，还有一个模型导出功能正在测试
- 兼容 RenderDoc：v1.2+（推荐 v1.30+，完整支持新版 API）
