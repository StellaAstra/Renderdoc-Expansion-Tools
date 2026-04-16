"""
Texture Exporter - RenderDoc Extension
========================================
集成到 RenderDoc 菜单栏的贴图资源批量导出工具。

安装后会在 Tools 菜单下添加:
  - Tools > Texture Exporter > Open Quick Panel              (打开停靠式快捷面板)
  - Tools > Texture Exporter > Export All Textures           (导出所有贴图，弹出设置面板)
  - Tools > Texture Exporter > Export Current Event Textures (导出当前事件关联的贴图，弹出设置面板)
  - Tools > Texture Exporter > List All Textures             (在控制台列出所有贴图信息)

Quick Panel (快捷面板):
  注册为 RenderDoc 可停靠窗口，通过 Window 菜单或 Tools 菜单打开。
  面板内集成所有导出选项和操作按钮，免去反复进入子菜单。
"""

import renderdoc as rd
import qrenderdoc as qrd
import os

# ============================================================
# 全局状态
# ============================================================

_ctx = None  # CaptureContext

# 导出配置（全局默认值）
_config = {
    "format": "dds",          # 导出格式（默认 DDS 保持原始格式）
    "keep_original_format": True,  # 保持原始格式（使用 DDS 逐贴图保存）
    "min_size": 2,            # 最小尺寸
    "export_all_mips": False, # 是否导出所有 mip
    "cubemap_faces": True,    # 导出 CubeMap 六面
    "slices_3d": True,        # 导出 3D 纹理切片
    "name_filter": "",        # 名称过滤
    "output_dir": "",         # 输出目录（空则自动选择）
}

# ============================================================
# 格式映射
# ============================================================

EXPORT_FORMAT_MAP = {
    "png": rd.FileType.PNG,
    "bmp": rd.FileType.BMP,
    "tga": rd.FileType.TGA,
    "hdr": rd.FileType.HDR,
    "exr": rd.FileType.EXR,
    "dds": rd.FileType.DDS,
}

CUBEMAP_FACE_NAMES = ["PosX", "NegX", "PosY", "NegY", "PosZ", "NegZ"]


# ============================================================
# 工具函数
# ============================================================

def sanitize_filename(name):
    """清理文件名中的非法字符"""
    invalid_chars = '<>:"/\\|?*'
    for ch in invalid_chars:
        name = name.replace(ch, '_')
    return name


def format_size(size_bytes):
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def get_texture_type_str(tex):
    """获取贴图类型描述"""
    t = tex.type
    if t == rd.TextureType.Texture1D:
        return "1D"
    elif t == rd.TextureType.Texture1DArray:
        return f"1DArray[{tex.arraysize}]"
    elif t == rd.TextureType.Texture2D:
        return "2D"
    elif t == rd.TextureType.Texture2DArray:
        return f"2DArray[{tex.arraysize}]"
    elif t == rd.TextureType.Texture2DMS:
        return f"2DMS(x{tex.msSamp})"
    elif t == rd.TextureType.Texture2DMSArray:
        return f"2DMSArray[{tex.arraysize}](x{tex.msSamp})"
    elif t == rd.TextureType.Texture3D:
        return f"3D(d={tex.depth})"
    elif t == rd.TextureType.TextureCube:
        return "Cube"
    elif t == rd.TextureType.TextureCubeArray:
        return f"CubeArray[{tex.arraysize // 6}]"
    return "Unknown"


def get_slice_count(tex, config):
    """获取需要导出的切片数"""
    t = tex.type
    if t == rd.TextureType.TextureCube:
        return 6 if config["cubemap_faces"] else 1
    elif t == rd.TextureType.TextureCubeArray:
        return tex.arraysize if config["cubemap_faces"] else 1
    elif t in (rd.TextureType.Texture1DArray, rd.TextureType.Texture2DArray,
               rd.TextureType.Texture2DMSArray):
        return tex.arraysize
    elif t == rd.TextureType.Texture3D:
        return tex.depth if config["slices_3d"] else 1
    return 1


# ============================================================
# 贴图后处理（Y翻转 + Linear→sRGB gamma 校正）
# ============================================================
#
# RenderDoc SaveTexture 的两个已知问题：
# 1. DX11 纹理以 top-down 方向存储，导出的 PNG 上下颠倒
# 2. sRGB 格式贴图（BC7_SRGB 等）被解码为 Linear 值，颜色偏暗
#
# 此函数用纯 Python + struct + zlib 实现，不依赖 PIL/numpy
# （因为 RenderDoc 内置 Python 3.6 环境没有这些包）

import struct as _struct
import zlib as _zlib
import math as _math


def _linear_to_srgb_byte(v):
    """将一个 0-255 的 Linear 值转换为 sRGB 值（返回 0-255 整数）"""
    c = v / 255.0
    if c <= 0.0031308:
        s = c * 12.92
    else:
        s = 1.055 * (c ** (1.0 / 2.4)) - 0.055
    return max(0, min(255, int(s * 255.0 + 0.5)))


# 预计算 Linear→sRGB 查找表（256 个值，只算一次）
_LIN2SRGB_LUT = [_linear_to_srgb_byte(i) for i in range(256)]


def _post_process_texture_file(filepath, apply_flip=True, apply_gamma=True):
    """
    对 SaveTexture 导出的 PNG 文件做后处理：
    1. Y 轴翻转（DX top-down → 标准图片方向）
    2. Linear→sRGB gamma 校正（仅 RGB 通道，Alpha 不动）

    纯 Python 实现，不依赖 PIL/numpy。仅处理 PNG 格式。
    对 DDS/HDR/EXR 等格式不调用此函数。
    """
    if not filepath.lower().endswith('.png'):
        # 对非 PNG 格式（BMP/TGA），尝试用 PIL（如果有的话）
        try:
            from PIL import Image
            img = Image.open(filepath)
            import numpy as np
            arr = np.array(img).astype(np.float64)
            if apply_flip:
                arr = arr[::-1].copy()
            if apply_gamma and arr.shape[2] >= 3:
                rgb = arr[:, :, :3] / 255.0
                srgb = np.where(
                    rgb <= 0.0031308, rgb * 12.92,
                    1.055 * np.power(np.clip(rgb, 0.0031308, 1.0), 1.0 / 2.4) - 0.055)
                arr[:, :, :3] = np.clip(srgb * 255.0, 0, 255)
            Image.fromarray(arr.astype(np.uint8), mode=img.mode).save(filepath)
            return True
        except ImportError:
            return False
        except Exception:
            return False

    try:
        with open(filepath, 'rb') as f:
            data = f.read()

        if data[:8] != b'\x89PNG\r\n\x1a\n':
            return False

        # Parse IHDR
        ihdr_len = _struct.unpack('>I', data[8:12])[0]
        if data[12:16] != b'IHDR':
            return False
        ihdr_data = data[16:16 + ihdr_len]
        width = _struct.unpack('>I', ihdr_data[0:4])[0]
        height = _struct.unpack('>I', ihdr_data[4:8])[0]
        bit_depth = ihdr_data[8]
        color_type = ihdr_data[9]

        if bit_depth != 8 or color_type not in (2, 6):
            return False

        channels = 4 if color_type == 6 else 3
        bpp = channels  # bytes per pixel (8-bit)
        row_bytes = width * channels
        stride = 1 + row_bytes  # filter byte + pixel data

        # Collect IDAT and other chunks
        idat_data = b''
        pos = 8
        chunks = []
        while pos < len(data):
            clen = _struct.unpack('>I', data[pos:pos + 4])[0]
            ctype = data[pos + 4:pos + 8]
            cdata = data[pos + 8:pos + 8 + clen]
            if ctype == b'IDAT':
                idat_data += cdata
            chunks.append((ctype, cdata))
            pos += 12 + clen

        raw = _zlib.decompress(idat_data)
        if len(raw) != height * stride:
            return False

        # ============================================================
        # PNG Filter 反解码：还原出真实像素值
        # Filter types: 0=None, 1=Sub, 2=Up, 3=Average, 4=Paeth
        # ============================================================
        decoded_rows = []  # list of bytearray, each row_bytes long (no filter byte)
        prev_row = bytearray(row_bytes)  # previous row (all zeros for first row)

        for y in range(height):
            row_start = y * stride
            ftype = raw[row_start]
            scanline = bytearray(raw[row_start + 1:row_start + stride])

            if ftype == 0:  # None
                pass
            elif ftype == 1:  # Sub
                for i in range(bpp, row_bytes):
                    scanline[i] = (scanline[i] + scanline[i - bpp]) & 0xFF
            elif ftype == 2:  # Up
                for i in range(row_bytes):
                    scanline[i] = (scanline[i] + prev_row[i]) & 0xFF
            elif ftype == 3:  # Average
                for i in range(row_bytes):
                    left = scanline[i - bpp] if i >= bpp else 0
                    scanline[i] = (scanline[i] + ((left + prev_row[i]) >> 1)) & 0xFF
            elif ftype == 4:  # Paeth
                for i in range(row_bytes):
                    a = scanline[i - bpp] if i >= bpp else 0
                    b = prev_row[i]
                    c = prev_row[i - bpp] if i >= bpp else 0
                    p = a + b - c
                    pa = abs(p - a)
                    pb = abs(p - b)
                    pc = abs(p - c)
                    if pa <= pb and pa <= pc:
                        pr = a
                    elif pb <= pc:
                        pr = b
                    else:
                        pr = c
                    scanline[i] = (scanline[i] + pr) & 0xFF

            decoded_rows.append(scanline)
            prev_row = scanline

        # ============================================================
        # 应用 gamma 校正（Linear → sRGB，仅 RGB，Alpha 不动）
        # ============================================================
        if apply_gamma:
            lut = _LIN2SRGB_LUT
            for row in decoded_rows:
                for x in range(width):
                    base = x * channels
                    row[base] = lut[row[base]]          # R
                    row[base + 1] = lut[row[base + 1]]  # G
                    row[base + 2] = lut[row[base + 2]]  # B
                    # Alpha (base+3) untouched

        # ============================================================
        # Y 翻转
        # ============================================================
        if apply_flip:
            decoded_rows.reverse()

        # ============================================================
        # 重建 PNG（所有行用 filter=0 None，最简单可靠）
        # ============================================================
        new_raw = b''
        for row in decoded_rows:
            new_raw += b'\x00' + bytes(row)

        new_idat = _zlib.compress(new_raw, 6)

        out = b'\x89PNG\r\n\x1a\n'
        first_idat = True
        for ctype, cdata in chunks:
            if ctype == b'IDAT':
                if first_idat:
                    cdata = new_idat
                    first_idat = False
                else:
                    continue
            chunk_bytes = ctype + cdata
            crc = _zlib.crc32(chunk_bytes) & 0xFFFFFFFF
            out += _struct.pack('>I', len(cdata)) + chunk_bytes + _struct.pack('>I', crc)

        with open(filepath, 'wb') as f:
            f.write(out)
        return True

    except Exception as e:
        print("  [PostProcess] Error: %s" % str(e))
        return False


# ============================================================
# 核心导出逻辑
# ============================================================

def _build_resource_name_map(controller):
    """构建 resourceId(int) -> name 的映射表"""
    name_map = {}
    try:
        resources = controller.GetResources()
        for res in resources:
            rid = int(res.resourceId)
            if res.name:
                name_map[rid] = res.name
    except Exception as e:
        print(f"  [DEBUG] GetResources error: {e}")
    return name_map


def do_export_textures(controller, config, target_ids=None):
    """
    执行贴图导出（在回放线程中调用）。
    导出时保持原始贴图尺寸（不缩放），格式由配置决定。

    Args:
        controller: ReplayController
        config: 配置字典（必须包含 output_dir）
        target_ids: 如果指定，仅导出这些资源 ID 的贴图
    """
    out_dir = config["output_dir"]
    os.makedirs(out_dir, exist_ok=True)

    keep_original = config.get("keep_original_format", True)
    file_ext = config["format"].lower()
    file_type = EXPORT_FORMAT_MAP.get(file_ext, rd.FileType.PNG)
    min_size = config["min_size"]
    name_filter = config["name_filter"].strip().lower()

    # 构建资源名称映射
    name_map = _build_resource_name_map(controller)

    textures = controller.GetTextures()
    total = len(textures)

    fmt_desc = "DDS (preserve original)" if keep_original else file_ext.upper()
    print(f"\n{'='*60}")
    print(f" Texture Exporter")
    print(f"{'='*60}")
    print(f" Total textures in capture: {total}")
    print(f" Output directory: {out_dir}")
    print(f" Format: {fmt_desc}")
    print(f" Keep original format: {keep_original}")
    print(f" Min size filter: {min_size}")
    if target_ids is not None:
        print(f" Filtering to {len(target_ids)} resource IDs from current event")
    if name_filter:
        print(f" Name filter: {name_filter}")
    if total > 0:
        for dbg_i in range(min(3, total)):
            t = textures[dbg_i]
            tname = name_map.get(int(t.resourceId), "(unnamed)")
            print(f"  [DEBUG] tex[{dbg_i}]: id={t.resourceId}, name='{tname}', {t.width}x{t.height}, fmt={t.format.Name()}")
    print(f"{'='*60}\n")

    exported = 0
    skipped = 0
    errors = 0

    for i, tex in enumerate(textures):
        # 过滤：目标 ID（用 int 比较确保可靠）
        if target_ids is not None and int(tex.resourceId) not in target_ids:
            continue

        # 过滤：尺寸
        if tex.width < min_size and tex.height < min_size:
            skipped += 1
            continue

        # 过滤：名称（从 name_map 获取）
        tex_name = name_map.get(int(tex.resourceId), f"Texture_{tex.resourceId}")
        if name_filter and name_filter not in tex_name.lower():
            skipped += 1
            continue

        safe_name = sanitize_filename(tex_name)
        fmt_name = str(tex.format.Name())
        tex_type = get_texture_type_str(tex)

        # 决定导出使用的格式
        if keep_original:
            # 使用 DDS 格式保持原始贴图格式（BC/ASTC 等压缩格式不丢失）
            cur_file_type = rd.FileType.DDS
            cur_file_ext = "dds"
        else:
            cur_file_type = file_type
            cur_file_ext = file_ext

        mip_count = tex.mips if config["export_all_mips"] else 1
        slice_count = get_slice_count(tex, config)

        for mip in range(mip_count):
            mip_w = max(1, tex.width >> mip)
            mip_h = max(1, tex.height >> mip)

            for s in range(slice_count):
                # 构建文件名：包含尺寸和格式信息方便识别
                parts = [safe_name]
                parts.append(f"{tex.width}x{tex.height}")
                if keep_original:
                    # 将原始格式名嵌入文件名，便于识别
                    safe_fmt = sanitize_filename(fmt_name)
                    parts.append(safe_fmt)
                if mip_count > 1:
                    parts.append(f"mip{mip}")
                if slice_count > 1:
                    if tex.type in (rd.TextureType.TextureCube, rd.TextureType.TextureCubeArray):
                        face_idx = s % 6
                        if tex.type == rd.TextureType.TextureCubeArray:
                            parts.append(f"cube{s // 6}_{CUBEMAP_FACE_NAMES[face_idx]}")
                        else:
                            parts.append(CUBEMAP_FACE_NAMES[face_idx])
                    elif tex.type == rd.TextureType.Texture3D:
                        parts.append(f"slice{s}")
                    else:
                        parts.append(f"array{s}")

                filename = "_".join(parts) + f".{cur_file_ext}"
                filepath = os.path.join(out_dir, filename)

                try:
                    save = rd.TextureSave()
                    save.resourceId = tex.resourceId
                    save.mip = mip
                    save.slice.sliceIndex = s
                    save.alpha = rd.AlphaMapping.Preserve
                    save.destType = cur_file_type

                    controller.SaveTexture(save, filepath)

                    # Post-process: Y-flip + Linear→sRGB (skip DDS/HDR/EXR)
                    if cur_file_ext not in ("dds", "hdr", "exr"):
                        _post_process_texture_file(filepath)

                    size_str = ""
                    if os.path.exists(filepath):
                        size_str = format_size(os.path.getsize(filepath))

                    print(f"  [{exported+1}] {filename} ({mip_w}x{mip_h}, {fmt_name}, {tex_type}) {size_str}")
                    exported += 1

                except Exception as e:
                    print(f"  [ERROR] {tex_name}: {e}")
                    errors += 1

    print(f"\n{'='*60}")
    print(f" Export complete!")
    print(f" Exported: {exported} | Skipped: {skipped} | Errors: {errors}")
    print(f" Output: {out_dir}")
    print(f"{'='*60}\n")

    # 写入导出日志
    try:
        log_path = os.path.join(out_dir, "_export_log.txt")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Texture Export Log\n")
            f.write(f"Exported: {exported}, Skipped: {skipped}, Errors: {errors}\n")
            f.write(f"Format: {fmt_desc}\n")
            f.write(f"Keep original format: {keep_original}\n")
            f.write(f"Output: {out_dir}\n")
    except Exception:
        pass

    return exported, skipped, errors


def _extract_resource_id(obj):
    """
    从不同版本的 RenderDoc API 对象中安全提取 ResourceId。

    新版 API (v1.30+):
      - GetReadOnlyResources 返回 UsedDescriptor 列表
        UsedDescriptor.descriptor.resource 是 ResourceId
      - GetOutputTargets 返回 Descriptor 列表
        Descriptor.resource 是 ResourceId
      - GetDepthTarget 返回 Descriptor
        Descriptor.resource 是 ResourceId

    旧版 API:
      - GetReadOnlyResources 返回 BoundResourceArray 列表
        binding.resources[i].resourceId
      - GetOutputTargets/GetDepthTarget 返回带 .resourceId 字段的对象
    """
    # 新版 Descriptor: 字段名是 .resource
    if hasattr(obj, 'resource'):
        return obj.resource
    # 旧版: 字段名是 .resourceId
    if hasattr(obj, 'resourceId'):
        return obj.resourceId
    return rd.ResourceId.Null()


def collect_event_texture_ids(controller):
    """从当前事件的管线状态中收集所有关联的贴图资源 ID（兼容新旧 API）"""
    state = controller.GetPipelineState()
    resource_ids = set()

    for stage in [rd.ShaderStage.Vertex, rd.ShaderStage.Hull, rd.ShaderStage.Domain,
                  rd.ShaderStage.Geometry, rd.ShaderStage.Pixel, rd.ShaderStage.Compute]:
        # --- 只读资源（SRV / Texture Bindings）---
        try:
            ro_resources = state.GetReadOnlyResources(stage, False)
            for item in ro_resources:
                # 新版 API: item 是 UsedDescriptor，有 .descriptor.resource
                if hasattr(item, 'descriptor'):
                    rid = _extract_resource_id(item.descriptor)
                    if rid != rd.ResourceId.Null():
                        resource_ids.add(int(rid))
                # 旧版 API: item 是 BoundResourceArray，有 .resources 列表
                elif hasattr(item, 'resources'):
                    for res in item.resources:
                        rid = _extract_resource_id(res)
                        if rid != rd.ResourceId.Null():
                            resource_ids.add(int(rid))
                else:
                    # 直接尝试提取
                    rid = _extract_resource_id(item)
                    if rid != rd.ResourceId.Null():
                        resource_ids.add(int(rid))
        except Exception as e:
            print(f"  [DEBUG] GetReadOnlyResources stage={stage} error: {e}")

        # --- 读写资源（UAV）---
        try:
            rw_resources = state.GetReadWriteResources(stage, False)
            for item in rw_resources:
                if hasattr(item, 'descriptor'):
                    rid = _extract_resource_id(item.descriptor)
                    if rid != rd.ResourceId.Null():
                        resource_ids.add(int(rid))
                elif hasattr(item, 'resources'):
                    for res in item.resources:
                        rid = _extract_resource_id(res)
                        if rid != rd.ResourceId.Null():
                            resource_ids.add(int(rid))
                else:
                    rid = _extract_resource_id(item)
                    if rid != rd.ResourceId.Null():
                        resource_ids.add(int(rid))
        except Exception as e:
            print(f"  [DEBUG] GetReadWriteResources stage={stage} error: {e}")

    # --- Render Targets ---
    try:
        om_targets = state.GetOutputTargets()
        for rt in om_targets:
            rid = _extract_resource_id(rt)
            if rid != rd.ResourceId.Null():
                resource_ids.add(int(rid))
    except Exception as e:
        print(f"  [DEBUG] GetOutputTargets error: {e}")

    # --- Depth Target ---
    try:
        ds = state.GetDepthTarget()
        rid = _extract_resource_id(ds)
        if rid != rd.ResourceId.Null():
            resource_ids.add(int(rid))
    except Exception as e:
        print(f"  [DEBUG] GetDepthTarget error: {e}")

    print(f"  [DEBUG] collect_event_texture_ids found {len(resource_ids)} resource IDs: {resource_ids}")
    return resource_ids


def list_all_textures(controller):
    """列出所有贴图信息，返回统计摘要"""
    # 构建资源名称映射
    name_map = _build_resource_name_map(controller)

    textures = controller.GetTextures()
    total = len(textures)

    # 统计分类
    type_counts = {}
    format_counts = {}
    total_pixels = 0
    named_count = 0
    size_buckets = {"tiny (<16)": 0, "small (16-128)": 0, "medium (128-1024)": 0, "large (1024-4096)": 0, "huge (>4096)": 0}

    print(f"\n{'='*90}")
    print(f" Texture Resources ({total} total)")
    print(f"{'='*90}")
    print(f"{'#':>5} | {'ResourceId':>12} | {'Size':>14} | {'Format':>28} | {'Type':>15} | Name")
    print(f"{'-'*90}")

    for i, tex in enumerate(textures):
        rid = int(tex.resourceId)
        name = name_map.get(rid, "(unnamed)")
        fmt = str(tex.format.Name())
        tex_type = get_texture_type_str(tex)
        size = f"{tex.width}x{tex.height}"
        if tex.depth > 1:
            size += f"x{tex.depth}"
        print(f"{i+1:>5} | {str(tex.resourceId):>12} | {size:>14} | {fmt:>28} | {tex_type:>15} | {name}")

        # 统计
        type_counts[tex_type] = type_counts.get(tex_type, 0) + 1
        format_counts[fmt] = format_counts.get(fmt, 0) + 1
        total_pixels += tex.width * tex.height
        if rid in name_map:
            named_count += 1
        max_dim = max(tex.width, tex.height)
        if max_dim < 16:
            size_buckets["tiny (<16)"] += 1
        elif max_dim < 128:
            size_buckets["small (16-128)"] += 1
        elif max_dim < 1024:
            size_buckets["medium (128-1024)"] += 1
        elif max_dim <= 4096:
            size_buckets["large (1024-4096)"] += 1
        else:
            size_buckets["huge (>4096)"] += 1

    print(f"{'='*90}\n")

    # 构建统计摘要
    summary_lines = [
        f"Total textures: {total}",
        f"Named: {named_count} | Unnamed: {total - named_count}",
        f"Total pixels: {total_pixels:,}",
        "",
        "--- By Type ---",
    ]
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        summary_lines.append(f"  {t}: {c}")

    summary_lines.append("")
    summary_lines.append("--- By Size ---")
    for bucket, c in size_buckets.items():
        if c > 0:
            summary_lines.append(f"  {bucket}: {c}")

    summary_lines.append("")
    summary_lines.append("--- By Format (top 10) ---")
    for fmt, c in sorted(format_counts.items(), key=lambda x: -x[1])[:10]:
        summary_lines.append(f"  {fmt}: {c}")

    summary = "\n".join(summary_lines)
    print(summary)
    return summary


# ============================================================
# 快捷面板 (Quick Panel / Dock Widget)
# ============================================================

_panel = None       # 面板实例引用
_panel_widget = None  # 顶层 QWidget 引用（用于 RaiseDockWindow）


class TextureExporterPanel:
    """
    停靠式快捷面板，通过 AddDockWindow 注册为可停靠窗口。
    用户可通过 Tools 菜单打开，面板可拖拽停靠到任意位置。
    """

    PANEL_TITLE = "Texture Exporter"

    def __init__(self, ctx):
        self.ctx = ctx
        self.mqt = ctx.Extensions().GetMiniQtHelper()
        self.widgets = {}
        self.fmt_state = [_config["format"].lower()]
        self._build_ui()

    def _build_ui(self):
        mqt = self.mqt
        ctx = self.ctx
        w = self.widgets

        def _noop(ctx, widget, text):
            pass

        def _on_closed(ctx, widget, text):
            pass

        # 创建顶层窗口（返回 QWidget，可用于 AddDockWindow / RaiseDockWindow）
        toplevel = mqt.CreateToplevelWidget(self.PANEL_TITLE, _on_closed)
        self.toplevel = toplevel

        root = mqt.CreateVerticalContainer()
        mqt.AddWidget(toplevel, root)
        self.root = root

        # ─── 标题 ───
        title = mqt.CreateLabel()
        mqt.SetWidgetText(title, "Texture Exporter  ─  Quick Panel")
        mqt.AddWidget(root, title)

        sep = mqt.CreateLabel()
        mqt.SetWidgetText(sep, "─────────────────────────────────")
        mqt.AddWidget(root, sep)

        # ─── 格式行 ───
        fmt_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(root, fmt_row)
        lbl = mqt.CreateLabel()
        mqt.SetWidgetText(lbl, "Format:")
        mqt.AddWidget(fmt_row, lbl)
        w["fmt_label"] = lbl

        def _on_fmt(ctx, widget, text):
            self.fmt_state[0] = text.strip().lower() if text else self.fmt_state[0]

        fmt_combo = mqt.CreateComboBox(False, _on_fmt)
        mqt.SetComboOptions(fmt_combo, ["png", "bmp", "tga", "hdr", "exr", "dds"])
        cur = _config["format"].lower()
        if cur in ["png", "bmp", "tga", "hdr", "exr", "dds"]:
            mqt.SelectComboOption(fmt_combo, cur)
        mqt.AddWidget(fmt_row, fmt_combo)
        w["fmt_combo"] = fmt_combo

        # ─── 保持原始格式 ───
        def _on_keep(ctx, widget, text):
            checked = mqt.IsWidgetChecked(widget)
            mqt.SetWidgetEnabled(w["fmt_combo"], not checked)
            mqt.SetWidgetEnabled(w["fmt_label"], not checked)

        keep_check = mqt.CreateCheckbox(_on_keep)
        mqt.SetWidgetText(keep_check, "Keep original (DDS)")
        mqt.SetWidgetChecked(keep_check, _config.get("keep_original_format", True))
        mqt.AddWidget(root, keep_check)
        w["keep_check"] = keep_check

        init_keep = _config.get("keep_original_format", True)
        mqt.SetWidgetEnabled(fmt_combo, not init_keep)
        mqt.SetWidgetEnabled(lbl, not init_keep)

        # ─── 最小尺寸 ───
        sz_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(root, sz_row)
        sz_lbl = mqt.CreateLabel()
        mqt.SetWidgetText(sz_lbl, "Min Size:")
        mqt.AddWidget(sz_row, sz_lbl)
        sz_input = mqt.CreateTextBox(True, _noop)
        mqt.SetWidgetText(sz_input, str(_config["min_size"]))
        mqt.AddWidget(sz_row, sz_input)
        w["size_input"] = sz_input

        # ─── 名称过滤 ───
        nf_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(root, nf_row)
        nf_lbl = mqt.CreateLabel()
        mqt.SetWidgetText(nf_lbl, "Name Filter:")
        mqt.AddWidget(nf_row, nf_lbl)
        nf_input = mqt.CreateTextBox(True, _noop)
        mqt.SetWidgetText(nf_input, _config["name_filter"])
        mqt.AddWidget(nf_row, nf_input)
        w["name_input"] = nf_input

        # ─── 选项 ───
        mips_chk = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(mips_chk, "All mip levels")
        mqt.SetWidgetChecked(mips_chk, _config["export_all_mips"])
        mqt.AddWidget(root, mips_chk)
        w["mips_check"] = mips_chk

        cube_chk = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(cube_chk, "CubeMap faces")
        mqt.SetWidgetChecked(cube_chk, _config["cubemap_faces"])
        mqt.AddWidget(root, cube_chk)
        w["cube_check"] = cube_chk

        slice_chk = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(slice_chk, "3D slices")
        mqt.SetWidgetChecked(slice_chk, _config["slices_3d"])
        mqt.AddWidget(root, slice_chk)
        w["slice_check"] = slice_chk

        # ─── 分隔 ───
        sep2 = mqt.CreateLabel()
        mqt.SetWidgetText(sep2, "─────────────────────────────────")
        mqt.AddWidget(root, sep2)

        # ─── 按钮组 ───
        btn1_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(root, btn1_row)

        export_all_btn = mqt.CreateButton(self._on_export_all)
        mqt.SetWidgetText(export_all_btn, "Export All Textures")
        mqt.AddWidget(btn1_row, export_all_btn)

        export_cur_btn = mqt.CreateButton(self._on_export_current)
        mqt.SetWidgetText(export_cur_btn, "Export Event Textures")
        mqt.AddWidget(btn1_row, export_cur_btn)

        btn2_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(root, btn2_row)

        list_btn = mqt.CreateButton(self._on_list)
        mqt.SetWidgetText(list_btn, "List Textures (Console)")
        mqt.AddWidget(btn2_row, list_btn)

        # ─── 状态标签 ───
        self.status_label = mqt.CreateLabel()
        mqt.SetWidgetText(self.status_label, "Ready.")
        mqt.AddWidget(root, self.status_label)

    def _gather_config(self):
        """从面板控件中收集当前配置"""
        mqt = self.mqt
        w = self.widgets
        return {
            "format": self.fmt_state[0],
            "keep_original_format": mqt.IsWidgetChecked(w["keep_check"]),
            "min_size": int(mqt.GetWidgetText(w["size_input"]) or "2"),
            "name_filter": mqt.GetWidgetText(w["name_input"]),
            "export_all_mips": mqt.IsWidgetChecked(w["mips_check"]),
            "cubemap_faces": mqt.IsWidgetChecked(w["cube_check"]),
            "slices_3d": mqt.IsWidgetChecked(w["slice_check"]),
        }

    def _set_status(self, text):
        self.mqt.SetWidgetText(self.status_label, text)

    def _on_export_all(self, ctx, widget, text):
        self._do_export("all")

    def _on_export_current(self, ctx, widget, text):
        self._do_export("current_event")

    def _do_export(self, mode):
        ctx = self.ctx
        ext = ctx.Extensions()

        if not ctx.IsCaptureLoaded():
            ext.ErrorDialog("No capture is currently loaded.", "Texture Exporter")
            return

        out_dir = ext.OpenDirectoryName("Select output directory")
        if not out_dir:
            return

        config = self._gather_config()
        config["output_dir"] = out_dir
        _config.update(config)

        self._set_status("Exporting...")

        export_result = [0, 0, 0]

        def _run(controller):
            try:
                target_ids = None
                if mode == "current_event":
                    cur_event = ctx.CurEvent()
                    print(f"[Texture Exporter] SetFrameEvent to EID {cur_event}")
                    controller.SetFrameEvent(cur_event, True)

                    target_ids = collect_event_texture_ids(controller)
                    if not target_ids:
                        print("[Texture Exporter] No textures found for the current event.")
                        return
                exported, skipped, errors = do_export_textures(controller, config, target_ids)
                export_result[0] = exported
                export_result[1] = skipped
                export_result[2] = errors
            except Exception as e:
                print(f"[Texture Exporter] ERROR: {e}")
                import traceback
                traceback.print_exc()

        ctx.Replay().BlockInvoke(_run)

        msg = f"Done! Exported: {export_result[0]}, Skipped: {export_result[1]}, Errors: {export_result[2]}"
        self._set_status(msg)
        ext.MessageDialog(
            f"Export complete!\n{msg}\nOutput: {out_dir}",
            "Texture Exporter"
        )

    def _on_list(self, ctx, widget, text):
        ctx = self.ctx
        ext = ctx.Extensions()

        if not ctx.IsCaptureLoaded():
            ext.ErrorDialog("No capture is currently loaded.", "Texture Exporter")
            return

        self._set_status("Listing textures...")

        result = [None, None]

        def _run(controller):
            try:
                result[0] = list_all_textures(controller)
            except Exception as e:
                result[1] = str(e)

        ctx.Replay().BlockInvoke(_run)

        if result[1]:
            self._set_status(f"Error: {result[1]}")
            ext.ErrorDialog(f"Error:\n{result[1]}", "Texture Exporter")
        else:
            summary = result[0] or "No textures found."
            self._set_status("List complete. See console.")
            ext.MessageDialog(f"{summary}\n\n(Full list in Python Output console)", "Texture Exporter")

    def get_widget(self):
        """返回顶层 QWidget（用于 AddDockWindow / RaiseDockWindow）"""
        return self.toplevel


def _create_panel(ctx):
    """创建面板实例并通过 AddDockWindow 注册为停靠窗口"""
    global _panel, _panel_widget
    try:
        _panel = TextureExporterPanel(ctx)
        _panel_widget = _panel.get_widget()
        # 注册为停靠窗口，放置在主工具区域
        ctx.AddDockWindow(_panel_widget, qrd.DockReference.MainToolArea, None)
        print("[Texture Exporter] Quick Panel created and docked")
    except Exception as e:
        print(f"[Texture Exporter] Panel create error: {e}")
        import traceback
        traceback.print_exc()


def _on_open_panel(ctx, data):
    """菜单回调：打开/激活快捷面板窗口"""
    global _panel, _panel_widget
    try:
        if _panel_widget is not None:
            # 面板已存在，提升到前台
            ctx.RaiseDockWindow(_panel_widget)
        else:
            # 首次打开，创建面板
            _create_panel(ctx)
    except Exception as e:
        print(f"[Texture Exporter] Open panel error: {e}")
        import traceback
        traceback.print_exc()


# ============================================================
# 菜单回调
# ============================================================

def _show_settings_and_export(ctx, mode="all"):
    """
    通用设置面板 + 导出逻辑。

    Args:
        ctx: CaptureContext
        mode: "all" = 导出所有贴图, "current_event" = 导出当前事件关联的贴图
    """
    try:
        if not ctx.IsCaptureLoaded():
            ctx.Extensions().ErrorDialog("No capture is currently loaded.", "Texture Exporter")
            return

        ext = ctx.Extensions()
        mqt = ext.GetMiniQtHelper()

        widgets = {}

        def _noop(ctx, widget, text):
            pass

        def _on_dialog_closed(ctx, widget, text):
            pass

        # 对话框标题根据模式区分
        if mode == "current_event":
            title = "Export Current Event Textures - Settings"
        else:
            title = "Export All Textures - Settings"

        dialog = mqt.CreateToplevelWidget(title, _on_dialog_closed)

        layout = mqt.CreateVerticalContainer()
        mqt.AddWidget(dialog, layout)

        # === 模式提示 ===
        mode_label = mqt.CreateLabel()
        if mode == "current_event":
            mqt.SetWidgetText(mode_label, "Mode: Export Current Event Textures")
        else:
            mqt.SetWidgetText(mode_label, "Mode: Export All Textures")
        mqt.AddWidget(layout, mode_label)

        # === 分隔 ===
        sep_label = mqt.CreateLabel()
        mqt.SetWidgetText(sep_label, "─────────────────────────────")
        mqt.AddWidget(layout, sep_label)

        # === 格式选择（仅在不保持原始格式时可用）===
        fmt_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(layout, fmt_row)
        fmt_label = mqt.CreateLabel()
        mqt.SetWidgetText(fmt_label, "Convert Format:")
        mqt.AddWidget(fmt_row, fmt_label)

        # 用列表存储当前选中的格式（GetWidgetText 不支持 ComboBox）
        fmt_state = [_config["format"].lower()]

        def _on_fmt_changed(ctx, widget, text):
            fmt_state[0] = text.strip().lower() if text else fmt_state[0]

        fmt_combo = mqt.CreateComboBox(False, _on_fmt_changed)
        fmt_options = ["png", "bmp", "tga", "hdr", "exr", "dds"]
        mqt.SetComboOptions(fmt_combo, fmt_options)
        current_fmt = _config["format"].lower()
        if current_fmt in fmt_options:
            mqt.SelectComboOption(fmt_combo, current_fmt)
        mqt.AddWidget(fmt_row, fmt_combo)
        widgets["fmt_combo"] = fmt_combo
        widgets["fmt_label"] = fmt_label
        widgets["fmt_state"] = fmt_state

        # === 保持原始格式（勾选后禁用格式选择下拉框）===
        def _on_keep_fmt_changed(ctx, widget, text):
            checked = mqt.IsWidgetChecked(widget)
            mqt.SetWidgetEnabled(widgets["fmt_combo"], not checked)
            mqt.SetWidgetEnabled(widgets["fmt_label"], not checked)

        keep_fmt_check = mqt.CreateCheckbox(_on_keep_fmt_changed)
        mqt.SetWidgetText(keep_fmt_check, "Keep original format (DDS, preserves BC/ASTC/etc.)")
        mqt.SetWidgetChecked(keep_fmt_check, _config.get("keep_original_format", True))
        mqt.AddWidget(layout, keep_fmt_check)
        widgets["keep_fmt_check"] = keep_fmt_check

        # 初始化时根据 checkbox 状态设置格式下拉框的启用/禁用
        _init_keep_original = _config.get("keep_original_format", True)
        mqt.SetWidgetEnabled(fmt_combo, not _init_keep_original)
        mqt.SetWidgetEnabled(fmt_label, not _init_keep_original)

        # === 最小尺寸 ===
        size_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(layout, size_row)
        size_label = mqt.CreateLabel()
        mqt.SetWidgetText(size_label, "Min Size:")
        mqt.AddWidget(size_row, size_label)
        size_input = mqt.CreateTextBox(True, _noop)
        mqt.SetWidgetText(size_input, str(_config["min_size"]))
        mqt.AddWidget(size_row, size_input)
        widgets["size_input"] = size_input

        # === 名称过滤 ===
        name_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(layout, name_row)
        name_label = mqt.CreateLabel()
        mqt.SetWidgetText(name_label, "Name Filter:")
        mqt.AddWidget(name_row, name_label)
        name_input = mqt.CreateTextBox(True, _noop)
        mqt.SetWidgetText(name_input, _config["name_filter"])
        mqt.AddWidget(name_row, name_input)
        widgets["name_input"] = name_input

        # === 选项复选框 ===
        mips_check = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(mips_check, "Export all mip levels")
        mqt.SetWidgetChecked(mips_check, _config["export_all_mips"])
        mqt.AddWidget(layout, mips_check)
        widgets["mips_check"] = mips_check

        cube_check = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(cube_check, "Export CubeMap faces")
        mqt.SetWidgetChecked(cube_check, _config["cubemap_faces"])
        mqt.AddWidget(layout, cube_check)
        widgets["cube_check"] = cube_check

        slice_check = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(slice_check, "Export 3D texture slices")
        mqt.SetWidgetChecked(slice_check, _config["slices_3d"])
        mqt.AddWidget(layout, slice_check)
        widgets["slice_check"] = slice_check

        # === 按钮行 ===
        btn_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(layout, btn_row)

        def _do_export_click(ctx, widget, text):
            try:
                out_dir = ext.OpenDirectoryName("Select output directory")
                if not out_dir:
                    return

                config = {
                    "format": widgets["fmt_state"][0],
                    "keep_original_format": mqt.IsWidgetChecked(widgets["keep_fmt_check"]),
                    "min_size": int(mqt.GetWidgetText(widgets["size_input"]) or "2"),
                    "name_filter": mqt.GetWidgetText(widgets["name_input"]),
                    "export_all_mips": mqt.IsWidgetChecked(widgets["mips_check"]),
                    "cubemap_faces": mqt.IsWidgetChecked(widgets["cube_check"]),
                    "slices_3d": mqt.IsWidgetChecked(widgets["slice_check"]),
                    "output_dir": out_dir,
                }

                # 更新全局配置（下次打开时记住上次设置）
                _config.update(config)

                export_result = [0, 0, 0]

                def _run(controller):
                    try:
                        target_ids = None
                        if mode == "current_event":
                            # 必须先跳转到当前事件，才能获取正确的管线状态
                            cur_event = ctx.CurEvent()
                            print(f"[Texture Exporter] SetFrameEvent to EID {cur_event}")
                            controller.SetFrameEvent(cur_event, True)

                            target_ids = collect_event_texture_ids(controller)
                            if not target_ids:
                                print("[Texture Exporter] No textures found for the current event.")
                                export_result[0] = 0
                                export_result[1] = 0
                                export_result[2] = 0
                                return
                            print(f"[Texture Exporter] Found {len(target_ids)} textures for current event")

                        exported, skipped, errors = do_export_textures(controller, config, target_ids)
                        export_result[0] = exported
                        export_result[1] = skipped
                        export_result[2] = errors
                    except Exception as e:
                        print(f"[Texture Exporter] ERROR in replay thread: {e}")
                        import traceback
                        traceback.print_exc()

                ctx.Replay().BlockInvoke(_run)

                mqt.CloseCurrentDialog(True)

                ext.MessageDialog(
                    f"Export complete!\nExported: {export_result[0]}, Skipped: {export_result[1]}, Errors: {export_result[2]}\nOutput directory: {out_dir}",
                    "Texture Exporter"
                )
            except Exception as e:
                print(f"[Texture Exporter] ERROR: {e}")
                import traceback
                traceback.print_exc()

        export_btn = mqt.CreateButton(_do_export_click)
        mqt.SetWidgetText(export_btn, "Export...")
        mqt.AddWidget(btn_row, export_btn)

        def _do_cancel_click(ctx, widget, text):
            mqt.CloseCurrentDialog(False)

        cancel_btn = mqt.CreateButton(_do_cancel_click)
        mqt.SetWidgetText(cancel_btn, "Cancel")
        mqt.AddWidget(btn_row, cancel_btn)

        mqt.ShowWidgetAsDialog(dialog)
    except Exception as e:
        print(f"[Texture Exporter] ERROR: {e}")
        import traceback
        traceback.print_exc()
        try:
            ctx.Extensions().ErrorDialog(f"Error: {e}", "Texture Exporter")
        except Exception:
            pass


def _on_export_all(ctx, data):
    """菜单回调：导出所有贴图（弹出设置面板）"""
    print("[Texture Exporter] Export All triggered")
    _show_settings_and_export(ctx, mode="all")


def _on_export_current_event(ctx, data):
    """菜单回调：导出当前事件关联的贴图（弹出设置面板）"""
    print("[Texture Exporter] Export Current Event triggered")
    _show_settings_and_export(ctx, mode="current_event")


def _on_list_textures(ctx, data):
    """菜单回调：列出所有贴图信息并弹窗显示统计"""
    try:
        print("[Texture Exporter] List All Textures triggered")

        if not ctx.IsCaptureLoaded():
            ctx.Extensions().ErrorDialog("No capture is currently loaded.", "Texture Exporter")
            return

        result = [None, None]

        def _do_list(controller):
            try:
                result[0] = list_all_textures(controller)
            except Exception as e:
                print(f"[Texture Exporter] ERROR in replay thread: {e}")
                import traceback
                traceback.print_exc()
                result[1] = str(e)

        ctx.Replay().BlockInvoke(_do_list)

        if result[1]:
            ctx.Extensions().ErrorDialog(f"Error querying textures:\n{result[1]}", "Texture Exporter")
        else:
            summary = result[0] or "No textures found."
            msg = f"{summary}\n\n(Full list printed to Python Output console)"
            ctx.Extensions().MessageDialog(msg, "Texture Exporter - Texture Summary")
    except Exception as e:
        print(f"[Texture Exporter] ERROR: {e}")
        import traceback
        traceback.print_exc()
        try:
            ctx.Extensions().ErrorDialog(f"Error: {e}", "Texture Exporter")
        except Exception:
            pass


# ============================================================
# 扩展注册/注销
# ============================================================

extiface_version = 0


def register(version, ctx):
    """RenderDoc 加载扩展时调用"""
    global _ctx
    _ctx = ctx

    print("[Texture Exporter] Extension loaded (v2.0 — Y-flip + sRGB gamma correction)")

    # 注册到 Tools 菜单 — 打开快捷面板
    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools,
        ["Texture Exporter", "Open Quick Panel"],
        _on_open_panel
    )

    # 注册到 Tools 菜单
    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools,
        ["Texture Exporter", "Export All Textures"],
        _on_export_all
    )

    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools,
        ["Texture Exporter", "Export Current Event Textures"],
        _on_export_current_event
    )

    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools,
        ["Texture Exporter", "List All Textures (Console)"],
        _on_list_textures
    )

    # 注册到 Texture Viewer 的上下文菜单（右键菜单）
    try:
        ctx.Extensions().RegisterContextMenu(
            qrd.ContextMenu.TextureViewer_Thumbnail,
            ["Export This Texture..."],
            _on_context_export_texture
        )
    except Exception:
        pass  # 旧版本可能不支持


def _on_context_export_texture(ctx, data):
    """Texture Viewer 右键菜单回调：导出选中的贴图"""
    try:
        print("[Texture Exporter] Context Export Texture triggered")

        resource_id = None

        # data 可能是 dict
        if isinstance(data, dict):
            resource_id = data.get("resourceId", None)
        else:
            # 尝试迭代 data.items()
            try:
                for key, val in data.items():
                    if key == "resourceId":
                        resource_id = val
                        break
            except Exception:
                pass

        if resource_id is None:
            ctx.Extensions().ErrorDialog("No texture selected.", "Texture Exporter")
            return

        out_path = ctx.Extensions().SaveFileName(
            "Save Texture As", "",
            "PNG (*.png);;BMP (*.bmp);;TGA (*.tga);;HDR (*.hdr);;EXR (*.exr);;DDS (*.dds)"
        )
        if not out_path:
            return

        file_ext = os.path.splitext(out_path)[1].lstrip('.').lower()
        file_type = EXPORT_FORMAT_MAP.get(file_ext, rd.FileType.PNG)

        def _do_save(controller):
            try:
                save = rd.TextureSave()
                save.resourceId = resource_id
                save.mip = 0
                save.slice.sliceIndex = 0
                save.alpha = rd.AlphaMapping.Preserve
                save.destType = file_type

                controller.SaveTexture(save, out_path)

                # Post-process: Y-flip + Linear→sRGB (skip DDS/HDR/EXR)
                if file_ext not in ("dds", "hdr", "exr"):
                    _post_process_texture_file(out_path)

                print(f"[Texture Exporter] Texture saved to: {out_path}")
            except Exception as e:
                print(f"[Texture Exporter] ERROR saving texture: {e}")
                import traceback
                traceback.print_exc()

        ctx.Replay().BlockInvoke(_do_save)

        ctx.Extensions().MessageDialog(
            f"Texture saved to:\n{out_path}",
            "Texture Exporter"
        )
    except Exception as e:
        print(f"[Texture Exporter] ERROR: {e}")
        import traceback
        traceback.print_exc()
        try:
            ctx.Extensions().ErrorDialog(f"Error: {e}", "Texture Exporter")
        except Exception:
            pass


def unregister():
    """RenderDoc 卸载扩展时调用"""
    global _ctx, _panel, _panel_widget
    _ctx = None
    _panel = None
    _panel_widget = None
    print("[Texture Exporter] Extension unloaded")
