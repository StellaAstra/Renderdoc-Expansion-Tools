"""
Model Extractor - RenderDoc Extension
========================================
从 RenderDoc 抓帧中提取 DrawCall 的 mesh 数据并导出为模型文件。

安装后会在 Tools 菜单下添加:
  - Tools > Model Extractor > Extract Current Draw Call     (提取当前选中的 DrawCall)
  - Tools > Model Extractor > Batch Extract All Draw Calls  (批量提取所有 DrawCall)
  - Tools > Model Extractor > List Draw Calls (Console)     (在控制台列出所有 DrawCall 信息)

支持导出格式: OBJ, PLY, glTF (.gltf + .bin), CSV (顶点数据), FBX (7.4 ASCII, 兼容 Unity/Unreal/Blender)

CSV 格式可配合独立脚本 csv_to_fbx.py 转换为标准 FBX 文件。
"""

import renderdoc as rd
import qrenderdoc as qrd
import os
import struct
import json
import time
import base64
import math

# ============================================================
# 全局状态
# ============================================================

_ctx = None  # CaptureContext

# 导出配置
_config = {
    "format": "obj",           # 导出格式 (obj / ply / gltf / csv / fbx)
    "export_normals": True,    # 导出法线
    "export_uvs": True,        # 导出 UV
    "export_colors": False,    # 导出顶点色
    "flip_uv_v": True,         # 翻转 UV V 坐标 (DX -> OpenGL 约定)
    "swap_yz": False,          # 交换 Y/Z 轴
    "scale": 1.0,              # 缩放因子
    "merge_by_marker": False,  # 按 Marker 分组合并
    "unpack_uv": False,        # UV 增强识别：float4 自动拆分两套 UV，float3 取前 2 分量；关闭则只识别 2 分量 UV
}


# ============================================================
# 工具函数
# ============================================================

def sanitize_filename(name):
    """清理文件名中的非法字符"""
    invalid_chars = '<>:"/\\|?*\0'
    for ch in invalid_chars:
        name = name.replace(ch, '_')
    return name.strip()


def format_count(n):
    """格式化数字显示"""
    return f"{n:,}"


# ============================================================
# 从 DrawCall 提取 Mesh 数据
# ============================================================

def _unpack_mesh_format_data(controller, mesh_fmt, num_verts, attrs_wanted):
    """
    从 MeshFormat 描述的缓冲区中解包顶点属性数据。
    这是内部辅助函数，用于处理 GetPostVSData 或手动 VBuffer 读取的结果。

    Args:
        controller: ReplayController
        mesh_fmt: MeshFormat 对象（来自 GetPostVSData）
        num_verts: 顶点数量
        attrs_wanted: 需要的属性列表 [{"name": ..., "offset": ..., "fmt": ...}, ...]

    Returns:
        dict: 属性名 -> [(val, ...), ...] 列表
    """
    result = {}
    if mesh_fmt.vertexResourceId == rd.ResourceId.Null():
        return result

    try:
        buf_data = controller.GetBufferData(
            mesh_fmt.vertexResourceId,
            mesh_fmt.vertexByteOffset,
            mesh_fmt.vertexByteStride * num_verts
        )
    except Exception as e:
        print(f"  [WARN] GetBufferData for mesh format failed: {e}")
        return result

    stride = mesh_fmt.vertexByteStride
    if stride == 0 or not buf_data:
        return result

    for attr_info in attrs_wanted:
        attr_name = attr_info["name"]
        offset = attr_info.get("offset", 0)
        fmt = attr_info.get("fmt", None)
        comp_count = attr_info.get("comp_count", 3)

        values = []
        for vi in range(num_verts):
            base = vi * stride + offset
            if base + comp_count * 4 > len(buf_data):
                values.append(tuple([0.0] * comp_count))
                continue
            try:
                vals = struct.unpack_from(f'<{comp_count}f', buf_data, base)
                cleaned = []
                for v in vals:
                    fv = float(v)
                    if not math.isfinite(fv) or abs(fv) > 1e9:
                        cleaned.append(0.0)
                    else:
                        cleaned.append(fv)
                values.append(tuple(cleaned))
            except Exception:
                values.append(tuple([0.0] * comp_count))

        result[attr_name] = values

    return result


def extract_mesh_from_draw(controller, action, config):
    """
    从一个 DrawCall 中提取 mesh 数据。
    优先使用 GetPostVSData 获取经过顶点着色器处理前的输入数据（VSIn），
    如果失败则回退到手动读取 VBuffer。

    Args:
        controller: ReplayController
        action: ActionDescription (DrawCall)
        config: 配置字典

    Returns:
        dict 或 None
    """
    scale = config.get("scale", 1.0)
    flip_v = config.get("flip_uv_v", True)
    swap_yz = config.get("swap_yz", False)
    export_normals = config.get("export_normals", True)
    export_uvs = config.get("export_uvs", True)
    unpack_uv = config.get("unpack_uv", True)
    export_colors = config.get("export_colors", False)

    try:
        controller.SetFrameEvent(action.eventId, True)
    except Exception as e:
        print(f"  [ERROR] SetFrameEvent({action.eventId}) failed: {e}")
        return None

    state = controller.GetPipelineState()

    # ======== 方法1: 通过 PipelineState 的 VertexInputs 和 VBuffers 读取 ========
    vbs = state.GetVBuffers()
    ib = state.GetIBuffer()
    attrs = state.GetVertexInputs()

    if not attrs:
        print(f"  [WARN] EID {action.eventId}: No vertex inputs found")
        return None

    # 打印所有顶点属性信息，方便调试
    print(f"  [DEBUG] EID {action.eventId}: Found {len(attrs)} vertex attributes:")
    for i, attr in enumerate(attrs):
        fmt = attr.format
        print(f"    [{i}] name='{attr.name}', vbSlot={attr.vertexBuffer}, "
              f"offset={attr.byteOffset}, compCount={fmt.compCount}, "
              f"compType={fmt.compType}, compByteWidth={fmt.compByteWidth}, "
              f"perInstance={attr.perInstance}")

    # 识别各属性
    pos_attr = None
    normal_attr = None
    uv_attrs = []  # 支持多套 UV，元素为 (attr, comp_start, comp_count, uv_channel_index)
    color_attr = None

    def _extract_semantic_index(name):
        """从属性名中提取语义索引，如 TEXCOORD2 -> 2, uv0 -> 0"""
        import re
        m = re.search(r'(\d+)\s*$', name)
        return int(m.group(1)) if m else 0

    for attr in attrs:
        if attr.perInstance:
            continue
        name_lower = attr.name.lower()

        # 位置属性：匹配 position/pos，但排除 sv_position（那是 VS 输出语义）
        if pos_attr is None:
            if 'sv_position' in name_lower:
                print(f"    -> Skipping '{attr.name}' for position (sv_position is VS output semantic)")
            elif any(k in name_lower for k in ['position', 'pos']):
                pos_attr = attr
        # 法线属性
        if export_normals and normal_attr is None and any(k in name_lower for k in ['normal', 'norm']):
            normal_attr = attr
        # UV 属性：收集所有匹配的 UV 通道（TEXCOORD0, TEXCOORD1, uv0, uv1 等）
        if export_uvs and any(k in name_lower for k in ['texcoord', 'uv', 'tex']):
            sem_idx = _extract_semantic_index(attr.name)
            comp_count = attr.format.compCount

            if comp_count <= 2:
                # 标准 2 分量 UV
                uv_attrs.append((attr, 0, 2, sem_idx))
            elif unpack_uv and comp_count == 3:
                # 3 分量 UVW — 取前 2 个分量作为 UV（仅 unpack_uv 开启时）
                uv_attrs.append((attr, 0, 2, sem_idx))
                print(f"    -> UV '{attr.name}' has {comp_count} components, using first 2 as UV (UVW)")
            elif unpack_uv and comp_count >= 4:
                # 4+ 分量可能是打包的多套 UV：xy = UV0, zw = UV1（仅 unpack_uv 开启时）
                uv_attrs.append((attr, 0, 2, sem_idx * 2))
                uv_attrs.append((attr, 2, 2, sem_idx * 2 + 1))
                print(f"    -> UV '{attr.name}' has {comp_count} components, splitting into 2 UV channels (xy + zw)")
            elif not unpack_uv and comp_count > 2:
                # unpack_uv 关闭时，跳过非 2 分量的 UV 属性
                print(f"    -> UV '{attr.name}' has {comp_count} components, skipped (unpack_uv disabled, only 2-component UVs)")

        # 顶点色属性
        if export_colors and color_attr is None and any(k in name_lower for k in ['color', 'colour']):
            color_attr = attr

    # 启发式：如果没找到位置属性，用语义索引和格式来推断
    if pos_attr is None:
        for attr in attrs:
            if attr.perInstance:
                continue
            name_lower = attr.name.lower()
            if any(k in name_lower for k in ['normal', 'norm', 'texcoord', 'uv', 'tex',
                                               'color', 'colour', 'tangent', 'binormal',
                                               'blendweight', 'blendindice', 'sv_']):
                continue
            if attr.format.compCount >= 3 and attr.format.compType == rd.CompType.Float:
                pos_attr = attr
                print(f"    -> Heuristic: using '{attr.name}' as position attribute")
                break

    # 如果还没找到 UV，进行第二轮更宽松的匹配（找所有 2 分量属性）
    if export_uvs and not uv_attrs:
        for attr in attrs:
            if attr.perInstance:
                continue
            if attr == pos_attr or attr == normal_attr:
                continue
            name_lower = attr.name.lower()
            if any(k in name_lower for k in ['normal', 'norm', 'position', 'pos',
                                               'color', 'colour', 'tangent', 'binormal',
                                               'blendweight', 'blendindice']):
                continue
            comp_count = attr.format.compCount
            if comp_count == 2:
                sem_idx = _extract_semantic_index(attr.name)
                uv_attrs.append((attr, 0, 2, sem_idx))
                print(f"    -> Heuristic: using '{attr.name}' as UV attribute (2-component)")
            elif unpack_uv and comp_count >= 4:
                # 可能是打包 UV（仅 unpack_uv 开启时）
                sem_idx = _extract_semantic_index(attr.name)
                uv_attrs.append((attr, 0, 2, sem_idx * 2))
                uv_attrs.append((attr, 2, 2, sem_idx * 2 + 1))
                print(f"    -> Heuristic: using '{attr.name}' as packed UV (4-component, splitting xy+zw)")

    # 按 UV 通道索引排序，确保 UV0, UV1, UV2... 顺序正确
    uv_attrs.sort(key=lambda x: x[3])

    if pos_attr is None:
        print(f"  [WARN] EID {action.eventId}: No position attribute found")
        return None

    # 打印最终属性选择结果
    print(f"  [INFO] Attribute mapping:")
    print(f"    Position: '{pos_attr.name}' (slot={pos_attr.vertexBuffer}, comp={pos_attr.format.compCount})")
    if normal_attr:
        print(f"    Normal:   '{normal_attr.name}' (slot={normal_attr.vertexBuffer}, comp={normal_attr.format.compCount})")
    else:
        print(f"    Normal:   <not found>")
    if uv_attrs:
        for ui, (ua, cs, cc, ci) in enumerate(uv_attrs):
            print(f"    UV[{ui}]:   '{ua.name}' (slot={ua.vertexBuffer}, totalComp={ua.format.compCount}, "
                  f"use comp[{cs}:{cs+cc}], channel={ci})")
    else:
        print(f"    UV:       <not found>")
    if color_attr:
        print(f"    Color:    '{color_attr.name}' (slot={color_attr.vertexBuffer}, comp={color_attr.format.compCount})")
    else:
        print(f"    Color:    <not found>")

    # ---- 读取索引 ----
    indices = []
    num_indices = action.numIndices

    if ib.resourceId != rd.ResourceId.Null() and (action.flags & rd.ActionFlags.Indexed):
        try:
            ib_data = controller.GetBufferData(ib.resourceId, 0, 0)
            idx_offset = action.indexOffset * ib.byteStride + ib.byteOffset

            for i in range(num_indices):
                off = idx_offset + i * ib.byteStride
                if ib.byteStride == 2:
                    if off + 2 <= len(ib_data):
                        idx = struct.unpack_from('<H', ib_data, off)[0]
                        indices.append(idx)
                elif ib.byteStride == 4:
                    if off + 4 <= len(ib_data):
                        idx = struct.unpack_from('<I', ib_data, off)[0]
                        indices.append(idx)
                else:
                    if off + 1 <= len(ib_data):
                        idx = ib_data[off]
                        indices.append(idx)
        except Exception as e:
            print(f"  [WARN] Index buffer read error: {e}")
            indices = list(range(num_indices))
    else:
        base_vertex = getattr(action, 'vertexOffset', 0)
        indices = list(range(base_vertex, base_vertex + num_indices))

    if not indices:
        return None

    # ---- 读取顶点属性 ----
    def read_vertex_attr(attr, num_components=None):
        """读取顶点属性数据，返回 tuples 列表"""
        vb_idx = attr.vertexBuffer
        if vb_idx >= len(vbs):
            print(f"    [WARN] Attr '{attr.name}': vb_idx {vb_idx} >= num VBs {len(vbs)}")
            return []

        vb_info = vbs[vb_idx]
        if vb_info.resourceId == rd.ResourceId.Null():
            print(f"    [WARN] Attr '{attr.name}': VB {vb_idx} has null resource ID")
            return []

        try:
            buf_data = controller.GetBufferData(vb_info.resourceId, 0, 0)
        except Exception as e:
            print(f"    [WARN] Attr '{attr.name}': GetBufferData failed: {e}")
            return []

        fmt = attr.format
        comp_count = num_components or fmt.compCount
        stride = vb_info.byteStride

        if stride == 0:
            print(f"    [WARN] Attr '{attr.name}': stride is 0")
            return []

        max_idx = max(indices) if indices else 0
        results = []

        comp_map = {
            (rd.CompType.Float, 2): ('e', 2),    # float16
            (rd.CompType.Float, 4): ('f', 4),    # float32
            (rd.CompType.Float, 8): ('d', 8),    # float64
            (rd.CompType.UInt, 1):  ('B', 1),
            (rd.CompType.UInt, 2):  ('H', 2),
            (rd.CompType.UInt, 4):  ('I', 4),
            (rd.CompType.SInt, 1):  ('b', 1),
            (rd.CompType.SInt, 2):  ('h', 2),
            (rd.CompType.SInt, 4):  ('i', 4),
            (rd.CompType.UNorm, 1): ('B', 1),
            (rd.CompType.UNorm, 2): ('H', 2),
            (rd.CompType.UNorm, 4): ('I', 4),
            (rd.CompType.SNorm, 1): ('b', 1),
            (rd.CompType.SNorm, 2): ('h', 2),
            (rd.CompType.SNorm, 4): ('i', 4),
        }

        key = (fmt.compType, fmt.compByteWidth)
        if key not in comp_map:
            print(f"    [WARN] Attr '{attr.name}': unsupported format compType={fmt.compType}, "
                  f"compByteWidth={fmt.compByteWidth} — trying float32 fallback")
            # 尝试按 float32 回退
            if fmt.compByteWidth == 2:
                # 可能是 float16 (half)
                char, size = 'e', 2
            else:
                return []
        else:
            char, size = comp_map[key]
        unpack_fmt = f'<{comp_count}{char}'
        elem_size = comp_count * size

        is_unorm = fmt.compType == rd.CompType.UNorm
        is_snorm = fmt.compType == rd.CompType.SNorm
        unorm_max = float((2 ** (fmt.compByteWidth * 8)) - 1) if is_unorm else 1.0
        snorm_max = float(2 ** (fmt.compByteWidth * 8 - 1) - 1) if is_snorm else 1.0

        for vi in range(max_idx + 1):
            offset = vb_info.byteOffset + attr.byteOffset + vi * stride
            if offset + elem_size > len(buf_data):
                results.append(tuple([0.0] * comp_count))
                continue

            try:
                raw = struct.unpack_from(unpack_fmt, buf_data, offset)
                if is_unorm:
                    vals = tuple(v / unorm_max for v in raw)
                elif is_snorm:
                    vals = tuple(max(-1.0, v / snorm_max) for v in raw)
                else:
                    vals = tuple(float(v) for v in raw)
                # 清理 NaN / Inf / 异常大值
                cleaned = []
                for v in vals:
                    if not math.isfinite(v) or abs(v) > 1e9:
                        cleaned.append(0.0)
                    else:
                        cleaned.append(v)
                results.append(tuple(cleaned))
            except Exception:
                results.append(tuple([0.0] * comp_count))

        return results

    # 读取位置
    raw_positions = read_vertex_attr(pos_attr, 3)
    if not raw_positions:
        return None

    # 打印位置数据范围用于调试
    if raw_positions:
        xs = [p[0] for p in raw_positions]
        ys = [p[1] for p in raw_positions]
        zs = [p[2] for p in raw_positions]
        print(f"  [DEBUG] Position range (object space): X=[{min(xs):.4f}, {max(xs):.4f}], "
              f"Y=[{min(ys):.4f}, {max(ys):.4f}], Z=[{min(zs):.4f}, {max(zs):.4f}]")
        # 检查是否是归一化的位置数据（压缩格式如 SNORM/UNORM）
        all_range = max(abs(min(xs)), abs(max(xs)), abs(min(ys)), abs(max(ys)), abs(min(zs)), abs(max(zs)))
        if all_range <= 1.0 and pos_attr.format.compType in (rd.CompType.SNorm, rd.CompType.UNorm):
            print(f"  [WARN] Position data appears to be in normalized format ({pos_attr.format.compType}).")
            print(f"         Values are in range [-1, 1]. The mesh may need manual scaling.")

    positions = []
    for p in raw_positions:
        x, y, z = p[0], p[1], p[2]
        x, y, z = x * scale, y * scale, z * scale
        if swap_yz:
            y, z = z, y
        positions.append((x, y, z))

    # 读取法线
    normals = []
    if normal_attr:
        raw_normals = read_vertex_attr(normal_attr, 3)
        for n in raw_normals:
            nx, ny, nz = n[0], n[1], n[2]
            if swap_yz:
                ny, nz = nz, ny
            normals.append((nx, ny, nz))

    # 读取 UV（支持多套，包括从高分量属性中拆分）
    uv_sets = []
    for ui, (uv_a, comp_start, comp_count, channel_idx) in enumerate(uv_attrs):
        uvs_channel = []
        # 读取属性的全部分量
        full_comp = uv_a.format.compCount
        raw_uvs = read_vertex_attr(uv_a, full_comp)
        for uv in raw_uvs:
            # 从 full_comp 中提取需要的子分量
            u = uv[comp_start] if comp_start < len(uv) else 0.0
            v = uv[comp_start + 1] if comp_start + 1 < len(uv) else 0.0
            if flip_v:
                v = 1.0 - v
            uvs_channel.append((u, v))
        if uvs_channel:
            us = [uv[0] for uv in uvs_channel]
            vs = [uv[1] for uv in uvs_channel]
            print(f"  [DEBUG] UV[{ui}] '{uv_a.name}'[{comp_start}:{comp_start+comp_count}] range: "
                  f"U=[{min(us):.4f}, {max(us):.4f}], V=[{min(vs):.4f}, {max(vs):.4f}]")
        print(f"  [DEBUG] UV[{ui}] count: {len(uvs_channel)}, Position count: {len(positions)}")
        if len(uvs_channel) != len(positions):
            print(f"  [WARN] UV[{ui}] count ({len(uvs_channel)}) != Position count ({len(positions)}), UVs may be dropped in export")
        uv_sets.append(uvs_channel)

    # 去重：移除数据完全相同的 UV 通道（保留第一个出现的）
    if len(uv_sets) > 1:
        unique_uv_sets = [uv_sets[0]]
        for ui in range(1, len(uv_sets)):
            is_duplicate = False
            for uj in range(len(unique_uv_sets)):
                if len(uv_sets[ui]) == len(unique_uv_sets[uj]) and uv_sets[ui] == unique_uv_sets[uj]:
                    is_duplicate = True
                    print(f"  [INFO] UV[{ui}] is identical to a previous UV channel, removing duplicate")
                    break
            if not is_duplicate:
                unique_uv_sets.append(uv_sets[ui])
        if len(unique_uv_sets) < len(uv_sets):
            print(f"  [INFO] UV channels after dedup: {len(uv_sets)} -> {len(unique_uv_sets)}")
        uv_sets = unique_uv_sets

    # 读取顶点色
    colors = []
    if color_attr:
        raw_colors = read_vertex_attr(color_attr, 4)
        colors = list(raw_colors)

    # 构建名称
    name = action.GetName(controller.GetStructuredFile()) if hasattr(action, 'GetName') else f"EID_{action.eventId}"
    if not name:
        name = f"EID_{action.eventId}"

    return {
        "name": name,
        "event_id": action.eventId,
        "positions": positions,
        "normals": normals,
        "uvs": uv_sets[0] if uv_sets else [],  # 第一套 UV（向后兼容）
        "uv_sets": uv_sets,                     # 所有 UV 通道
        "colors": colors,
        "indices": indices,
    }


# ============================================================
# OBJ 导出
# ============================================================

def export_obj(mesh_data, filepath):
    """
    将 mesh 数据导出为 Wavefront OBJ 格式。
    注意: OBJ 标准只支持一套 UV（vt），这里导出第一套 UV。
    如有多套 UV，额外的 UV 数据会写在注释中，并建议使用 FBX/glTF 格式。
    """
    positions = mesh_data["positions"]
    normals = mesh_data["normals"]
    uv_sets = mesh_data.get("uv_sets", [])
    uvs = uv_sets[0] if uv_sets else mesh_data.get("uvs", [])
    indices = mesh_data["indices"]
    name = mesh_data.get("name", "mesh")

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(f"# Exported from RenderDoc by Model Extractor\n")
        f.write(f"# Event ID: {mesh_data.get('event_id', 'N/A')}\n")
        f.write(f"# Vertices: {len(positions)}, Faces: {len(indices) // 3}\n")
        f.write(f"# UV channels: {len(uv_sets)}\n")
        if len(uv_sets) > 1:
            f.write(f"# NOTE: OBJ only supports 1 UV set. Only UV[0] is exported as vt.\n")
            f.write(f"#       Use FBX or glTF format for multiple UV sets.\n")
        f.write(f"\n")
        f.write(f"o {sanitize_filename(name)}\n\n")

        # 顶点位置
        for p in positions:
            f.write(f"v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}\n")

        # UV（第一套）
        if uvs:
            f.write(f"\n# UV channel 0\n")
            for uv in uvs:
                f.write(f"vt {uv[0]:.6f} {uv[1]:.6f}\n")

        # 法线
        if normals:
            f.write(f"\n")
            for n in normals:
                f.write(f"vn {n[0]:.6f} {n[1]:.6f} {n[2]:.6f}\n")

        # 额外 UV 通道数据写入注释块（供高级工具解析）
        for ui in range(1, len(uv_sets)):
            uv_channel = uv_sets[ui]
            if len(uv_channel) == len(positions):
                f.write(f"\n# EXTRA_UV_CHANNEL {ui} ({len(uv_channel)} vertices)\n")
                for uv in uv_channel:
                    f.write(f"# vt{ui} {uv[0]:.6f} {uv[1]:.6f}\n")

        # 面（OBJ 是 1-based）
        f.write(f"\n")
        has_uvs = len(uvs) > 0
        has_normals = len(normals) > 0

        for i in range(0, len(indices) - 2, 3):
            i0, i1, i2 = indices[i], indices[i + 1], indices[i + 2]
            v0, v1, v2 = i0 + 1, i1 + 1, i2 + 1

            if has_uvs and has_normals:
                f.write(f"f {v0}/{v0}/{v0} {v1}/{v1}/{v1} {v2}/{v2}/{v2}\n")
            elif has_uvs:
                f.write(f"f {v0}/{v0} {v1}/{v1} {v2}/{v2}\n")
            elif has_normals:
                f.write(f"f {v0}//{v0} {v1}//{v1} {v2}//{v2}\n")
            else:
                f.write(f"f {v0} {v1} {v2}\n")

    return True


# ============================================================
# PLY 导出
# ============================================================

def export_ply(mesh_data, filepath):
    """
    将 mesh 数据导出为 PLY 格式（binary little-endian）。
    支持多套 UV：第一套用 s/t，后续用 s1/t1, s2/t2...
    """
    positions = mesh_data["positions"]
    normals = mesh_data["normals"]
    uv_sets = mesh_data.get("uv_sets", [])
    colors = mesh_data.get("colors", [])
    indices = mesh_data["indices"]

    has_normals = len(normals) == len(positions)
    # 只保留与 positions 数量一致的 UV 通道
    valid_uv_sets = [us for us in uv_sets if len(us) == len(positions)]
    has_colors = len(colors) == len(positions)
    num_faces = len(indices) // 3

    with open(filepath, 'wb') as f:
        # 头部
        header_lines = [
            "ply",
            "format binary_little_endian 1.0",
            f"comment Exported from RenderDoc by Model Extractor",
            f"comment Event ID: {mesh_data.get('event_id', 'N/A')}",
            f"comment UV channels: {len(valid_uv_sets)}",
            f"element vertex {len(positions)}",
            "property float x",
            "property float y",
            "property float z",
        ]
        if has_normals:
            header_lines += ["property float nx", "property float ny", "property float nz"]
        for ui in range(len(valid_uv_sets)):
            if ui == 0:
                header_lines += ["property float s", "property float t"]
            else:
                header_lines += [f"property float s{ui}", f"property float t{ui}"]
        if has_colors:
            header_lines += [
                "property uchar red", "property uchar green",
                "property uchar blue", "property uchar alpha"
            ]
        header_lines += [
            f"element face {num_faces}",
            "property list uchar uint vertex_indices",
            "end_header",
        ]
        header = "\n".join(header_lines) + "\n"
        f.write(header.encode('ascii'))

        # 顶点数据
        for i in range(len(positions)):
            p = positions[i]
            f.write(struct.pack('<fff', p[0], p[1], p[2]))

            if has_normals:
                n = normals[i]
                f.write(struct.pack('<fff', n[0], n[1], n[2]))

            for uv_channel in valid_uv_sets:
                uv = uv_channel[i]
                f.write(struct.pack('<ff', uv[0], uv[1]))

            if has_colors:
                c = colors[i]
                r = int(max(0, min(255, c[0] * 255)))
                g = int(max(0, min(255, c[1] * 255)))
                b = int(max(0, min(255, c[2] * 255)))
                a = int(max(0, min(255, c[3] * 255))) if len(c) > 3 else 255
                f.write(struct.pack('<BBBB', r, g, b, a))

        # 面数据
        for i in range(0, len(indices) - 2, 3):
            f.write(struct.pack('<B', 3))
            f.write(struct.pack('<III', indices[i], indices[i + 1], indices[i + 2]))

    return True


# ============================================================
# glTF 导出
# ============================================================

def export_gltf(mesh_data, filepath):
    """
    将 mesh 数据导出为 glTF 2.0 格式（.gltf + .bin）。
    支持多套 UV（TEXCOORD_0, TEXCOORD_1, ...）。
    """
    positions = mesh_data["positions"]
    normals = mesh_data["normals"]
    uvs = mesh_data["uvs"]
    indices = mesh_data["indices"]
    name = mesh_data.get("name", "mesh")
    uv_sets = mesh_data.get("uv_sets", [uvs] if uvs else [])

    has_normals = len(normals) == len(positions)
    # 只保留与 positions 数量一致的 UV 通道
    valid_uv_sets = [us for us in uv_sets if len(us) == len(positions)]

    # 构建二进制缓冲区
    bin_data = bytearray()

    # 索引数据（UNSIGNED_INT）
    idx_offset = len(bin_data)
    for idx in indices:
        bin_data += struct.pack('<I', idx)
    idx_length = len(bin_data) - idx_offset

    # 对齐到 4 字节
    while len(bin_data) % 4 != 0:
        bin_data += b'\x00'

    # 位置数据
    pos_offset = len(bin_data)
    pos_min = [float('inf')] * 3
    pos_max = [float('-inf')] * 3
    for p in positions:
        bin_data += struct.pack('<fff', p[0], p[1], p[2])
        for j in range(3):
            pos_min[j] = min(pos_min[j], p[j])
            pos_max[j] = max(pos_max[j], p[j])
    pos_length = len(bin_data) - pos_offset

    if not positions:
        pos_min = [0, 0, 0]
        pos_max = [0, 0, 0]

    # 法线数据
    norm_offset = 0
    norm_length = 0
    if has_normals:
        norm_offset = len(bin_data)
        for n in normals:
            bin_data += struct.pack('<fff', n[0], n[1], n[2])
        norm_length = len(bin_data) - norm_offset

    # UV 数据（多套）
    uv_offsets = []
    uv_lengths = []
    for uv_channel in valid_uv_sets:
        uv_off = len(bin_data)
        for uv in uv_channel:
            bin_data += struct.pack('<ff', uv[0], uv[1])
        uv_len = len(bin_data) - uv_off
        uv_offsets.append(uv_off)
        uv_lengths.append(uv_len)

    # 写 .bin 文件
    bin_filename = os.path.splitext(os.path.basename(filepath))[0] + ".bin"
    bin_filepath = os.path.join(os.path.dirname(filepath), bin_filename)

    with open(bin_filepath, 'wb') as f:
        f.write(bytes(bin_data))

    # 构建 glTF JSON
    buffer_views = []
    accessors = []
    attributes = {}

    # Buffer View 0: 索引
    buffer_views.append({
        "buffer": 0,
        "byteOffset": idx_offset,
        "byteLength": idx_length,
        "target": 34963,  # ELEMENT_ARRAY_BUFFER
    })
    # Accessor 0: 索引
    accessors.append({
        "bufferView": 0,
        "byteOffset": 0,
        "componentType": 5125,  # UNSIGNED_INT
        "count": len(indices),
        "type": "SCALAR",
        "max": [max(indices)] if indices else [0],
        "min": [min(indices)] if indices else [0],
    })

    # Buffer View 1: 位置
    buffer_views.append({
        "buffer": 0,
        "byteOffset": pos_offset,
        "byteLength": pos_length,
        "target": 34962,  # ARRAY_BUFFER
        "byteStride": 12,
    })
    # Accessor 1: 位置
    accessors.append({
        "bufferView": 1,
        "byteOffset": 0,
        "componentType": 5126,  # FLOAT
        "count": len(positions),
        "type": "VEC3",
        "max": pos_max,
        "min": pos_min,
    })
    attributes["POSITION"] = 1

    bv_idx = 2
    acc_idx = 2

    # 法线
    if has_normals:
        buffer_views.append({
            "buffer": 0,
            "byteOffset": norm_offset,
            "byteLength": norm_length,
            "target": 34962,
            "byteStride": 12,
        })
        accessors.append({
            "bufferView": bv_idx,
            "byteOffset": 0,
            "componentType": 5126,
            "count": len(normals),
            "type": "VEC3",
        })
        attributes["NORMAL"] = acc_idx
        bv_idx += 1
        acc_idx += 1

    # UV（多套）
    for ui, (uv_off, uv_len) in enumerate(zip(uv_offsets, uv_lengths)):
        buffer_views.append({
            "buffer": 0,
            "byteOffset": uv_off,
            "byteLength": uv_len,
            "target": 34962,
            "byteStride": 8,
        })
        accessors.append({
            "bufferView": bv_idx,
            "byteOffset": 0,
            "componentType": 5126,
            "count": len(valid_uv_sets[ui]),
            "type": "VEC2",
        })
        attributes[f"TEXCOORD_{ui}"] = acc_idx
        bv_idx += 1
        acc_idx += 1

    gltf = {
        "asset": {
            "version": "2.0",
            "generator": "RenderDoc Model Extractor",
        },
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": sanitize_filename(name)}],
        "meshes": [{
            "name": sanitize_filename(name),
            "primitives": [{
                "attributes": attributes,
                "indices": 0,
                "mode": 4,  # TRIANGLES
            }],
        }],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{
            "uri": bin_filename,
            "byteLength": len(bin_data),
        }],
    }

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(gltf, f, indent=2)

    return True


# ============================================================
# CSV 导出
# ============================================================

def export_csv(mesh_data, filepath):
    """
    将 mesh 数据导出为 CSV 格式。
    生成两个文件：
      - {name}_vertices.csv  — 顶点数据（pos, normal, uv, color）
      - {name}_indices.csv   — 三角面索引

    CSV 格式便于外部工具（如 csv_to_fbx.py）转换为标准 FBX。
    """
    positions = mesh_data["positions"]
    normals = mesh_data["normals"]
    uvs = mesh_data["uvs"]
    colors = mesh_data.get("colors", [])
    indices = mesh_data["indices"]
    uv_sets = mesh_data.get("uv_sets", [uvs] if uvs else [])

    has_normals = len(normals) == len(positions)
    has_uvs = len(uvs) == len(positions)
    has_colors = len(colors) == len(positions)
    # 只保留与 positions 数量一致的 UV 通道
    valid_uv_sets = [us for us in uv_sets if len(us) == len(positions)]
    num_uv_channels = len(valid_uv_sets)

    base = os.path.splitext(filepath)[0]
    vert_path = base + "_vertices.csv"
    idx_path = base + "_indices.csv"

    # -- 顶点 CSV --
    with open(vert_path, 'w', encoding='utf-8') as f:
        # 头部
        header = ["vx", "vy", "vz"]
        if has_normals:
            header += ["nx", "ny", "nz"]
        # 多套 UV：u0,v0,u1,v1,...（第一套仍用 u,v 保持向后兼容）
        for ui in range(num_uv_channels):
            if ui == 0:
                header += ["u", "v"]
            else:
                header += [f"u{ui}", f"v{ui}"]
        if has_colors:
            header += ["cr", "cg", "cb", "ca"]
        f.write(",".join(header) + "\n")

        for i in range(len(positions)):
            row = [f"{positions[i][0]:.8f}", f"{positions[i][1]:.8f}", f"{positions[i][2]:.8f}"]
            if has_normals:
                row += [f"{normals[i][0]:.8f}", f"{normals[i][1]:.8f}", f"{normals[i][2]:.8f}"]
            for ui in range(num_uv_channels):
                row += [f"{valid_uv_sets[ui][i][0]:.8f}", f"{valid_uv_sets[ui][i][1]:.8f}"]
            if has_colors:
                row += [f"{colors[i][0]:.6f}", f"{colors[i][1]:.6f}",
                        f"{colors[i][2]:.6f}", f"{colors[i][3]:.6f}" if len(colors[i]) > 3 else "1.000000"]
            f.write(",".join(row) + "\n")

    # -- 索引 CSV --
    with open(idx_path, 'w', encoding='utf-8') as f:
        f.write("i0,i1,i2\n")
        for fi in range(0, len(indices) - 2, 3):
            f.write(f"{indices[fi]},{indices[fi+1]},{indices[fi+2]}\n")

    # -- 元数据 JSON（供 csv_to_fbx.py 使用）--
    meta_path = base + "_meta.json"
    meta = {
        "name": mesh_data.get("name", "mesh"),
        "event_id": mesh_data.get("event_id", 0),
        "num_vertices": len(positions),
        "num_faces": len(indices) // 3,
        "has_normals": has_normals,
        "has_uvs": has_uvs,
        "num_uv_channels": num_uv_channels,
        "has_colors": has_colors,
        "vertices_file": os.path.basename(vert_path),
        "indices_file": os.path.basename(idx_path),
    }
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"  [CSV] Vertices: {vert_path}")
    print(f"  [CSV] Indices:  {idx_path}")
    print(f"  [CSV] Metadata: {meta_path}")

    return True


# ============================================================
# FBX 导出 (ASCII 7.4, Unity 兼容)
# ============================================================

def _generate_edges(indices, num_faces):
    """
    从三角面索引生成边列表（FBX Edges 格式）。
    每条边只出现一次，值为 PolygonVertexIndex 数组中的索引位置。
    """
    edge_set = set()
    edges = []
    for fi in range(num_faces):
        base = fi * 3
        tri = [indices[fi * 3], indices[fi * 3 + 1], indices[fi * 3 + 2]]
        for j in range(3):
            v0 = tri[j]
            v1 = tri[(j + 1) % 3]
            edge_key = (min(v0, v1), max(v0, v1))
            if edge_key not in edge_set:
                edge_set.add(edge_key)
                edges.append(base + j)
    return edges


def export_fbx(mesh_data, filepath):
    """
    将 mesh 数据导出为 FBX 7.4 ASCII 格式，完全兼容 Unity / Unreal / Blender / Maya。

    关键兼容性要点：
    - 使用 FBX 7.4 ASCII（Unity 原生支持最好的格式之一）
    - 包含 Edges 数据（Unity importer 需要）
    - 使用固定小数格式避免科学计数法
    - 法线不包含 NormalsW（避免干扰）
    - 正确的 Properties70 类型签名
    """
    positions = mesh_data["positions"]
    normals = mesh_data["normals"]
    uvs = mesh_data["uvs"]
    indices = mesh_data["indices"]
    name = sanitize_filename(mesh_data.get("name", "mesh"))
    uv_sets = mesh_data.get("uv_sets", [uvs] if uvs else [])

    num_verts = len(positions)
    num_faces = len(indices) // 3
    has_normals = len(normals) == num_verts
    has_uvs = len(uvs) == num_verts
    # 只保留与 positions 数量一致的 UV 通道
    valid_uv_sets = [us for us in uv_sets if len(us) == num_verts]

    if num_verts == 0 or num_faces == 0:
        return False

    _fbx_id_counter = [1000000000]

    def next_id():
        _fbx_id_counter[0] += 1
        return _fbx_id_counter[0]

    model_id = next_id()
    geom_id = next_id()
    material_id = next_id()

    now = time.gmtime()

    # 安全的浮点数清理
    def safe_float(v):
        if not math.isfinite(v) or abs(v) > 1e9:
            return 0.0
        return v

    # 位置数据（flat float array）
    flat_positions = []
    for p in positions:
        flat_positions.extend(safe_float(v) for v in p)

    # 索引数据（FBX 约定：面的最后一个索引取负再减1）
    fbx_indices = []
    for fi in range(num_faces):
        i0, i1, i2 = indices[fi * 3], indices[fi * 3 + 1], indices[fi * 3 + 2]
        fbx_indices.extend([i0, i1, -(i2 + 1)])

    # 生成边列表
    edges = _generate_edges(indices, num_faces)

    # 格式化数组 — 使用固定小数格式，避免科学计数法
    def fmt_float_arr(arr, per_line=6):
        lines = []
        for i in range(0, len(arr), per_line):
            chunk = arr[i:i + per_line]
            lines.append(",".join(f"{v:.6f}" for v in chunk))
        return ",\n\t\t\t\t".join(lines)

    def fmt_int_arr(arr, per_line=12):
        lines = []
        for i in range(0, len(arr), per_line):
            chunk = arr[i:i + per_line]
            lines.append(",".join(str(v) for v in chunk))
        return ",\n\t\t\t\t".join(lines)

    vertices_str = fmt_float_arr(flat_positions)
    indices_str = fmt_int_arr(fbx_indices)
    edges_str = fmt_int_arr(edges)

    # 法线（ByPolygonVertex / Direct）— 不带 NormalsW
    normals_section = ""
    if has_normals:
        face_normals = []
        for fi in range(num_faces):
            for j in range(3):
                idx = indices[fi * 3 + j]
                if idx < len(normals):
                    face_normals.extend(safe_float(v) for v in normals[idx])
                else:
                    face_normals.extend([0.0, 0.0, 1.0])

        normals_str = fmt_float_arr(face_normals)
        normals_section = f"""\n\t\tLayerElementNormal: 0 {{
\t\t\tVersion: 102
\t\t\tName: "Normals"
\t\t\tMappingInformationType: "ByPolygonVertex"
\t\t\tReferenceInformationType: "Direct"
\t\t\tNormals: *{len(face_normals)} {{
\t\t\t\ta: {normals_str}
\t\t\t}}
\t\t}}"""

    # UV（ByPolygonVertex / IndexToDirect）— 支持多套 UV
    uv_section = ""
    uv_indices = []
    for fi in range(num_faces):
        for j in range(3):
            uv_indices.append(indices[fi * 3 + j])
    uv_idx_str = fmt_int_arr(uv_indices)

    for ui, uv_channel in enumerate(valid_uv_sets):
        flat_uvs = []
        for uv in uv_channel:
            flat_uvs.extend(safe_float(v) for v in uv)
        uv_str = fmt_float_arr(flat_uvs)
        uv_name = "UVMap" if ui == 0 else f"UVMap{ui}"
        uv_section += f"""\n\t\tLayerElementUV: {ui} {{
\t\t\tVersion: 101
\t\t\tName: "{uv_name}"
\t\t\tMappingInformationType: "ByPolygonVertex"
\t\t\tReferenceInformationType: "IndexToDirect"
\t\t\tUV: *{len(flat_uvs)} {{
\t\t\t\ta: {uv_str}
\t\t\t}}
\t\t\tUVIndex: *{len(uv_indices)} {{
\t\t\t\ta: {uv_idx_str}
\t\t\t}}
\t\t}}"""

    # 材质层
    material_section = f"""\n\t\tLayerElementMaterial: 0 {{
\t\t\tVersion: 101
\t\t\tName: ""
\t\t\tMappingInformationType: "AllSame"
\t\t\tReferenceInformationType: "IndexToDirect"
\t\t\tMaterials: *1 {{
\t\t\t\ta: 0
\t\t\t}}
\t\t}}"""

    # Layer 定义 — Layer 0 包含法线、第一套 UV 和材质
    layer_entries = ""
    if has_normals:
        layer_entries += """
\t\t\tLayerElement:  {
\t\t\t\tType: "LayerElementNormal"
\t\t\t\tTypedIndex: 0
\t\t\t}"""
    if valid_uv_sets:
        layer_entries += """
\t\t\tLayerElement:  {
\t\t\t\tType: "LayerElementUV"
\t\t\t\tTypedIndex: 0
\t\t\t}"""
    layer_entries += """
\t\t\tLayerElement:  {
\t\t\t\tType: "LayerElementMaterial"
\t\t\t\tTypedIndex: 0
\t\t\t}"""

    # 额外的 Layer（Layer 1, 2, ...）用于额外的 UV 通道
    extra_layers = ""
    for ui in range(1, len(valid_uv_sets)):
        extra_layers += f"""
\t\tLayer: {ui} {{
\t\t\tVersion: 100
\t\t\tLayerElement:  {{
\t\t\t\tType: "LayerElementUV"
\t\t\t\tTypedIndex: {ui}
\t\t\t}}
\t\t}}"""

    fbx_content = f"""; FBX 7.4.0 project file
; Exported from RenderDoc by Model Extractor v2.0
; Event ID: {mesh_data.get('event_id', 'N/A')}
; Vertices: {num_verts}, Faces: {num_faces}
; -----------------------------------------------
FBXHeaderExtension:  {{
\tFBXHeaderVersion: 1003
\tFBXVersion: 7400
\tCreationTimeStamp:  {{
\t\tVersion: 1000
\t\tYear: {now.tm_year}
\t\tMonth: {now.tm_mon}
\t\tDay: {now.tm_mday}
\t\tHour: {now.tm_hour}
\t\tMinute: {now.tm_min}
\t\tSecond: {now.tm_sec}
\t\tMillisecond: 0
\t}}
\tCreator: "RenderDoc Model Extractor v2.1"
\tSceneInfo: "SceneInfo::GlobalInfo", "UserData" {{
\t\tType: "UserData"
\t\tVersion: 100
\t\tMetaData:  {{
\t\t\tVersion: 100
\t\t\tTitle: ""
\t\t\tSubject: ""
\t\t\tAuthor: "RenderDoc Model Extractor"
\t\t\tKeywords: ""
\t\t\tRevision: ""
\t\t\tComment: ""
\t\t}}
\t}}
}}

GlobalSettings:  {{
\tVersion: 1000
\tProperties70:  {{
\t\tP: "UpAxis", "int", "Integer", "",1
\t\tP: "UpAxisSign", "int", "Integer", "",1
\t\tP: "FrontAxis", "int", "Integer", "",2
\t\tP: "FrontAxisSign", "int", "Integer", "",1
\t\tP: "CoordAxis", "int", "Integer", "",0
\t\tP: "CoordAxisSign", "int", "Integer", "",1
\t\tP: "OriginalUpAxis", "int", "Integer", "",1
\t\tP: "OriginalUpAxisSign", "int", "Integer", "",1
\t\tP: "UnitScaleFactor", "double", "Number", "",1.0
\t\tP: "OriginalUnitScaleFactor", "double", "Number", "",1.0
\t\tP: "AmbientColor", "ColorRGB", "Color", "",0,0,0
\t}}
}}

Documents:  {{
\tCount: 1
\tDocument: 100000000, "", "Scene" {{
\t\tProperties70:  {{
\t\t\tP: "SourceObject", "object", "", ""
\t\t\tP: "ActiveAnimStackName", "KString", "", "", ""
\t\t}}
\t\tRootNode: 0
\t}}
}}

References:  {{
}}

Definitions:  {{
\tVersion: 100
\tCount: 4
\tObjectType: "GlobalSettings" {{
\t\tCount: 1
\t}}
\tObjectType: "Model" {{
\t\tCount: 1
\t\tPropertyTemplate: "FbxNode" {{
\t\t\tProperties70:  {{
\t\t\t\tP: "QuaternionInterpolate", "enum", "", "",0
\t\t\t\tP: "RotationOffset", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "RotationPivot", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "ScalingOffset", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "ScalingPivot", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "TranslationActive", "bool", "", "",0
\t\t\t\tP: "TranslationMin", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "TranslationMax", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "TranslationMinX", "bool", "", "",0
\t\t\t\tP: "TranslationMinY", "bool", "", "",0
\t\t\t\tP: "TranslationMinZ", "bool", "", "",0
\t\t\t\tP: "TranslationMaxX", "bool", "", "",0
\t\t\t\tP: "TranslationMaxY", "bool", "", "",0
\t\t\t\tP: "TranslationMaxZ", "bool", "", "",0
\t\t\t\tP: "RotationOrder", "enum", "", "",0
\t\t\t\tP: "RotationSpaceForLimitOnly", "bool", "", "",0
\t\t\t\tP: "RotationStiffnessX", "double", "Number", "",0
\t\t\t\tP: "RotationStiffnessY", "double", "Number", "",0
\t\t\t\tP: "RotationStiffnessZ", "double", "Number", "",0
\t\t\t\tP: "AxisLen", "double", "Number", "",10
\t\t\t\tP: "PreRotation", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "PostRotation", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "RotationActive", "bool", "", "",0
\t\t\t\tP: "RotationMin", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "RotationMax", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "RotationMinX", "bool", "", "",0
\t\t\t\tP: "RotationMinY", "bool", "", "",0
\t\t\t\tP: "RotationMinZ", "bool", "", "",0
\t\t\t\tP: "RotationMaxX", "bool", "", "",0
\t\t\t\tP: "RotationMaxY", "bool", "", "",0
\t\t\t\tP: "RotationMaxZ", "bool", "", "",0
\t\t\t\tP: "InheritType", "enum", "", "",0
\t\t\t\tP: "ScalingActive", "bool", "", "",0
\t\t\t\tP: "ScalingMin", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "ScalingMax", "Vector3D", "Vector", "",1,1,1
\t\t\t\tP: "ScalingMinX", "bool", "", "",0
\t\t\t\tP: "ScalingMinY", "bool", "", "",0
\t\t\t\tP: "ScalingMinZ", "bool", "", "",0
\t\t\t\tP: "ScalingMaxX", "bool", "", "",0
\t\t\t\tP: "ScalingMaxY", "bool", "", "",0
\t\t\t\tP: "ScalingMaxZ", "bool", "", "",0
\t\t\t\tP: "GeometricTranslation", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "GeometricRotation", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "GeometricScaling", "Vector3D", "Vector", "",1,1,1
\t\t\t\tP: "Lcl Translation", "Lcl Translation", "", "A",0,0,0
\t\t\t\tP: "Lcl Rotation", "Lcl Rotation", "", "A",0,0,0
\t\t\t\tP: "Lcl Scaling", "Lcl Scaling", "", "A",1,1,1
\t\t\t\tP: "Visibility", "Visibility", "", "A",1
\t\t\t\tP: "Visibility Inheritance", "Visibility Inheritance", "", "",1
\t\t\t}}
\t\t}}
\t}}
\tObjectType: "Geometry" {{
\t\tCount: 1
\t\tPropertyTemplate: "FbxMesh" {{
\t\t\tProperties70:  {{
\t\t\t\tP: "Color", "ColorRGB", "Color", "",0.8,0.8,0.8
\t\t\t\tP: "BBoxMin", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "BBoxMax", "Vector3D", "Vector", "",0,0,0
\t\t\t}}
\t\t}}
\t}}
\tObjectType: "Material" {{
\t\tCount: 1
\t\tPropertyTemplate: "FbxSurfacePhong" {{
\t\t\tProperties70:  {{
\t\t\t\tP: "ShadingModel", "KString", "", "", "Phong"
\t\t\t\tP: "MultiLayer", "bool", "", "",0
\t\t\t\tP: "EmissiveColor", "Color", "", "A",0,0,0
\t\t\t\tP: "EmissiveFactor", "Number", "", "A",1
\t\t\t\tP: "AmbientColor", "Color", "", "A",0.2,0.2,0.2
\t\t\t\tP: "AmbientFactor", "Number", "", "A",1
\t\t\t\tP: "DiffuseColor", "Color", "", "A",0.8,0.8,0.8
\t\t\t\tP: "DiffuseFactor", "Number", "", "A",1
\t\t\t\tP: "Bump", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "NormalMap", "Vector3D", "Vector", "",0,0,0
\t\t\t\tP: "BumpFactor", "double", "Number", "",1
\t\t\t\tP: "TransparentColor", "Color", "", "A",0,0,0
\t\t\t\tP: "TransparencyFactor", "Number", "", "A",0
\t\t\t\tP: "DisplacementColor", "ColorRGB", "Color", "",0,0,0
\t\t\t\tP: "DisplacementFactor", "double", "Number", "",1
\t\t\t\tP: "VectorDisplacementColor", "ColorRGB", "Color", "",0,0,0
\t\t\t\tP: "VectorDisplacementFactor", "double", "Number", "",1
\t\t\t\tP: "SpecularColor", "Color", "", "A",0.2,0.2,0.2
\t\t\t\tP: "SpecularFactor", "Number", "", "A",1
\t\t\t\tP: "ShininessExponent", "Number", "", "A",20
\t\t\t\tP: "ReflectionColor", "Color", "", "A",0,0,0
\t\t\t\tP: "ReflectionFactor", "Number", "", "A",1
\t\t\t}}
\t\t}}
\t}}
}}

Objects:  {{
\tGeometry: {geom_id}, "Geometry::{name}", "Mesh" {{
\t\tVertices: *{num_verts * 3} {{
\t\t\ta: {vertices_str}
\t\t}}
\t\tPolygonVertexIndex: *{len(fbx_indices)} {{
\t\t\ta: {indices_str}
\t\t}}
\t\tEdges: *{len(edges)} {{
\t\t\ta: {edges_str}
\t\t}}
\t\tGeometryVersion: 124{normals_section}{uv_section}{material_section}
\t\tLayer: 0 {{
\t\t\tVersion: 100{layer_entries}
\t\t}}{extra_layers}
\t}}
\tModel: {model_id}, "Model::{name}", "Mesh" {{
\t\tVersion: 232
\t\tProperties70:  {{
\t\t\tP: "RotationActive", "bool", "", "",1
\t\t\tP: "InheritType", "enum", "", "",1
\t\t\tP: "ScalingMax", "Vector3D", "Vector", "",0,0,0
\t\t\tP: "DefaultAttributeIndex", "int", "Integer", "",0
\t\t\tP: "Lcl Translation", "Lcl Translation", "", "A+",0,0,0
\t\t\tP: "Lcl Rotation", "Lcl Rotation", "", "A+",0,0,0
\t\t\tP: "Lcl Scaling", "Lcl Scaling", "", "A+",1,1,1
\t\t}}
\t\tShading: T
\t\tCulling: "CullingOff"
\t}}
\tMaterial: {material_id}, "Material::DefaultMaterial", "" {{
\t\tVersion: 102
\t\tShadingModel: "phong"
\t\tMultiLayer: 0
\t\tProperties70:  {{
\t\t\tP: "EmissiveColor", "Color", "", "A",0,0,0
\t\t\tP: "EmissiveFactor", "Number", "", "A",0
\t\t\tP: "AmbientColor", "Color", "", "A",0.2,0.2,0.2
\t\t\tP: "DiffuseColor", "Color", "", "A",0.8,0.8,0.8
\t\t\tP: "DiffuseFactor", "Number", "", "A",1
\t\t\tP: "TransparentColor", "Color", "", "A",0,0,0
\t\t\tP: "TransparencyFactor", "Number", "", "A",0
\t\t\tP: "SpecularColor", "Color", "", "A",0.2,0.2,0.2
\t\t\tP: "SpecularFactor", "Number", "", "A",1
\t\t\tP: "ShininessExponent", "Number", "", "A",20
\t\t\tP: "ReflectionColor", "Color", "", "A",0,0,0
\t\t\tP: "ReflectionFactor", "Number", "", "A",1
\t\t}}
\t}}
}}

Connections:  {{
\tC: "OO",{model_id},0
\tC: "OO",{geom_id},{model_id}
\tC: "OO",{material_id},{model_id}
}}
"""

    with open(filepath, 'w', encoding='ascii') as f:
        f.write(fbx_content)

    return True


# ============================================================
# 导出格式分发
# ============================================================

EXPORT_FUNC_MAP = {
    "obj": (export_obj, ".obj"),
    "ply": (export_ply, ".ply"),
    "gltf": (export_gltf, ".gltf"),
    "csv": (export_csv, ".csv"),
    "fbx": (export_fbx, ".fbx"),
}


def export_mesh(mesh_data, out_dir, fmt="obj", filename_prefix=None):
    """
    导出单个 mesh 到文件。
    文件会按格式归类到对应的子文件夹中（如 fbx/, csv/, obj/ 等）。

    Args:
        mesh_data: extract_mesh_from_draw 返回的字典
        out_dir: 输出根目录
        fmt: 格式 (obj/ply/gltf/csv/fbx)
        filename_prefix: 文件名前缀（默认用 event_id 和 name）

    Returns:
        (success, filepath, message)
    """
    if fmt not in EXPORT_FUNC_MAP:
        return False, "", f"Unsupported format: {fmt}"

    export_func, ext = EXPORT_FUNC_MAP[fmt]

    # 按格式创建子文件夹
    fmt_dir = os.path.join(out_dir, fmt)
    os.makedirs(fmt_dir, exist_ok=True)

    if filename_prefix:
        safe_name = sanitize_filename(filename_prefix)
    else:
        eid = mesh_data.get("event_id", 0)
        name = mesh_data.get("name", "mesh")
        safe_name = sanitize_filename(f"EID{eid}_{name}")

    filepath = os.path.join(fmt_dir, safe_name + ext)

    # 避免文件名冲突
    counter = 1
    base_path = filepath
    while os.path.exists(filepath):
        filepath = os.path.splitext(base_path)[0] + f"_{counter}" + ext
        counter += 1

    try:
        export_func(mesh_data, filepath)
        num_verts = len(mesh_data["positions"])
        num_faces = len(mesh_data["indices"]) // 3
        msg = f"{num_verts:,} verts, {num_faces:,} faces"
        return True, filepath, msg
    except Exception as e:
        return False, filepath, f"Error: {e}"


# ============================================================
# 遍历 DrawCall 树
# ============================================================

def collect_draw_calls(actions, result=None):
    """递归收集所有 DrawCall（带 Drawcall 标志的 action）"""
    if result is None:
        result = []
    for action in actions:
        if action.flags & rd.ActionFlags.Drawcall:
            result.append(action)
        if action.children:
            collect_draw_calls(action.children, result)
    return result


def find_action_by_event_id(actions, event_id):
    """根据 event_id 查找 action"""
    for action in actions:
        if action.eventId == event_id:
            return action
        if action.children:
            found = find_action_by_event_id(action.children, event_id)
            if found:
                return found
    return None


# ============================================================
# 批量提取
# ============================================================

def do_batch_extract(controller, config, out_dir, draw_calls=None):
    """
    批量提取 DrawCall mesh 数据并导出。

    Args:
        controller: ReplayController
        config: 配置字典
        out_dir: 输出目录
        draw_calls: 要提取的 DrawCall 列表（None = 所有）

    Returns:
        (exported, skipped, errors, messages)
    """
    os.makedirs(out_dir, exist_ok=True)

    if draw_calls is None:
        draw_calls = collect_draw_calls(controller.GetRootActions())

    fmt = config.get("format", "obj")
    total = len(draw_calls)

    print(f"\n{'='*60}")
    print(f" Model Extractor - Batch Export")
    print(f"{'='*60}")
    print(f" Draw calls to process: {total}")
    print(f" Output format: {fmt.upper()}")
    print(f" Output directory: {out_dir}/{fmt}/")
    print(f"{'='*60}\n")

    exported = 0
    skipped = 0
    errors = 0
    messages = []

    for i, action in enumerate(draw_calls):
        eid = action.eventId
        draw_name = f"EID_{eid}"

        print(f"  [{i+1}/{total}] Processing EID {eid}: {draw_name}...")

        mesh_data = extract_mesh_from_draw(controller, action, config)

        if mesh_data is None or not mesh_data["positions"]:
            skipped += 1
            print(f"    -> Skipped (no vertex data)")
            continue

        success, filepath, msg = export_mesh(mesh_data, out_dir, fmt)

        if success:
            exported += 1
            print(f"    -> OK: {os.path.basename(filepath)} ({msg})")
            messages.append(f"EID {eid}: {os.path.basename(filepath)} ({msg})")
        else:
            errors += 1
            print(f"    -> FAILED: {msg}")
            messages.append(f"EID {eid}: FAILED - {msg}")

    print(f"\n{'='*60}")
    print(f" Batch export complete!")
    print(f" Exported: {exported} | Skipped: {skipped} | Errors: {errors}")
    print(f" Output: {out_dir}")
    print(f"{'='*60}\n")

    # 写入导出日志
    try:
        log_path = os.path.join(out_dir, "_export_log.txt")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Model Extractor - Export Log\n")
            f.write(f"Format: {fmt.upper()}\n")
            f.write(f"Exported: {exported}, Skipped: {skipped}, Errors: {errors}\n\n")
            for m in messages:
                f.write(f"{m}\n")
    except Exception:
        pass

    return exported, skipped, errors, messages


def do_list_draw_calls(controller):
    """列出所有 DrawCall 信息，返回摘要字符串"""
    draw_calls = collect_draw_calls(controller.GetRootActions())
    total = len(draw_calls)

    print(f"\n{'='*80}")
    print(f" Draw Calls ({total} total)")
    print(f"{'='*80}")
    print(f"{'#':>5} | {'EID':>8} | {'Indices':>10} | {'Instances':>10} | Name")
    print(f"{'-'*80}")

    total_indices = 0
    total_instances = 0

    for i, action in enumerate(draw_calls):
        eid = action.eventId
        n_idx = action.numIndices
        n_inst = action.numInstances
        name = f"EID_{eid}"

        total_indices += n_idx
        total_instances += n_inst

        print(f"{i+1:>5} | {eid:>8} | {n_idx:>10,} | {n_inst:>10,} | {name}")

    print(f"{'='*80}")

    summary_lines = [
        f"Total draw calls: {total}",
        f"Total indices: {format_count(total_indices)}",
        f"Total instances: {format_count(total_instances)}",
    ]

    summary = "\n".join(summary_lines)
    print(summary)
    return summary


# ============================================================
# 菜单回调 - 设置面板
# ============================================================

def _show_extract_panel(ctx, mode="single"):
    """
    提取设置面板。

    Args:
        ctx: CaptureContext
        mode: "single" = 当前 DrawCall, "batch" = 所有 DrawCall
    """
    try:
        if not ctx.IsCaptureLoaded():
            ctx.Extensions().ErrorDialog("No capture is currently loaded.", "Model Extractor")
            return

        ext = ctx.Extensions()
        mqt = ext.GetMiniQtHelper()

        widgets = {}

        def _noop(ctx, widget, text):
            pass

        def _on_dialog_closed(ctx, widget, text):
            pass

        # 对话框
        titles = {
            "single": "Extract Current Draw Call - Settings",
            "batch": "Batch Extract All Draw Calls - Settings",
        }
        dialog = mqt.CreateToplevelWidget(titles.get(mode, "Extract - Settings"), _on_dialog_closed)

        layout = mqt.CreateVerticalContainer()
        mqt.AddWidget(dialog, layout)

        # === 模式提示 ===
        mode_label = mqt.CreateLabel()
        mode_texts = {
            "single": "Mode: Extract Current Draw Call",
            "batch": "Mode: Batch Extract All Draw Calls",
        }
        mqt.SetWidgetText(mode_label, mode_texts.get(mode, ""))
        mqt.AddWidget(layout, mode_label)

        sep = mqt.CreateLabel()
        mqt.SetWidgetText(sep, "─────────────────────────────")
        mqt.AddWidget(layout, sep)

        # === 格式选择 ===
        fmt_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(layout, fmt_row)
        fmt_label = mqt.CreateLabel()
        mqt.SetWidgetText(fmt_label, "Export Format:")
        mqt.AddWidget(fmt_row, fmt_label)

        fmt_state = [_config["format"].lower()]

        def _on_fmt_changed(ctx, widget, text):
            fmt_state[0] = text.strip().lower() if text else fmt_state[0]

        fmt_combo = mqt.CreateComboBox(False, _on_fmt_changed)
        fmt_options = ["obj", "ply", "gltf", "csv", "fbx"]
        mqt.SetComboOptions(fmt_combo, fmt_options)
        current_fmt = _config["format"].lower()
        if current_fmt in fmt_options:
            mqt.SelectComboOption(fmt_combo, current_fmt)
        mqt.AddWidget(fmt_row, fmt_combo)
        widgets["fmt_state"] = fmt_state

        # === 导出选项 ===
        opt_label = mqt.CreateLabel()
        mqt.SetWidgetText(opt_label, "Export Options:")
        mqt.AddWidget(layout, opt_label)

        normals_check = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(normals_check, "Export normals")
        mqt.SetWidgetChecked(normals_check, _config.get("export_normals", True))
        mqt.AddWidget(layout, normals_check)
        widgets["normals_check"] = normals_check

        uvs_check = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(uvs_check, "Export UVs")
        mqt.SetWidgetChecked(uvs_check, _config.get("export_uvs", True))
        mqt.AddWidget(layout, uvs_check)
        widgets["uvs_check"] = uvs_check

        colors_check = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(colors_check, "Export vertex colors (PLY only)")
        mqt.SetWidgetChecked(colors_check, _config.get("export_colors", False))
        mqt.AddWidget(layout, colors_check)
        widgets["colors_check"] = colors_check

        flip_uv_check = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(flip_uv_check, "Flip UV V coordinate")
        mqt.SetWidgetChecked(flip_uv_check, _config.get("flip_uv_v", True))
        mqt.AddWidget(layout, flip_uv_check)
        widgets["flip_uv_check"] = flip_uv_check

        unpack_uv_check = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(unpack_uv_check, "UV unpack (float4 split to 2 UVs, float3 use first 2)")
        mqt.SetWidgetChecked(unpack_uv_check, _config.get("unpack_uv", False))
        mqt.AddWidget(layout, unpack_uv_check)
        widgets["unpack_uv_check"] = unpack_uv_check

        swap_yz_check = mqt.CreateCheckbox(_noop)
        mqt.SetWidgetText(swap_yz_check, "Swap Y/Z axis")
        mqt.SetWidgetChecked(swap_yz_check, _config.get("swap_yz", False))
        mqt.AddWidget(layout, swap_yz_check)
        widgets["swap_yz_check"] = swap_yz_check

        # === 缩放 ===
        scale_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(layout, scale_row)
        scale_label = mqt.CreateLabel()
        mqt.SetWidgetText(scale_label, "Scale Factor:")
        mqt.AddWidget(scale_row, scale_label)
        scale_input = mqt.CreateTextBox(True, _noop)
        mqt.SetWidgetText(scale_input, str(_config.get("scale", 1.0)))
        mqt.AddWidget(scale_row, scale_input)
        widgets["scale_input"] = scale_input

        # === 按钮行 ===
        sep2 = mqt.CreateLabel()
        mqt.SetWidgetText(sep2, "─────────────────────────────")
        mqt.AddWidget(layout, sep2)

        btn_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(layout, btn_row)

        def _do_extract_click(ctx, widget, text):
            try:
                out_dir = ext.OpenDirectoryName("Select output directory for extracted models")
                if not out_dir:
                    return

                config = {
                    "format": widgets["fmt_state"][0],
                    "export_normals": mqt.IsWidgetChecked(widgets["normals_check"]),
                    "export_uvs": mqt.IsWidgetChecked(widgets["uvs_check"]),
                    "export_colors": mqt.IsWidgetChecked(widgets["colors_check"]),
                    "flip_uv_v": mqt.IsWidgetChecked(widgets["flip_uv_check"]),
                    "unpack_uv": mqt.IsWidgetChecked(widgets["unpack_uv_check"]),
                    "swap_yz": mqt.IsWidgetChecked(widgets["swap_yz_check"]),
                    "scale": float(mqt.GetWidgetText(widgets["scale_input"]) or "1.0"),
                }

                _config.update(config)

                result_msg = [""]

                def _run(controller):
                    try:
                        if mode == "single":
                            # 获取当前选中的事件
                            cur_event = ctx.CurEvent()
                            actions = controller.GetRootActions()
                            action = find_action_by_event_id(actions, cur_event)

                            if action is None:
                                # 尝试找最近的 DrawCall
                                all_draws = collect_draw_calls(actions)
                                nearest = None
                                min_dist = float('inf')
                                for d in all_draws:
                                    dist = abs(d.eventId - cur_event)
                                    if dist < min_dist:
                                        min_dist = dist
                                        nearest = d
                                action = nearest

                            if action is None:
                                result_msg[0] = "No draw call found at or near the current event."
                                return

                            if not (action.flags & rd.ActionFlags.Drawcall):
                                # 当前不是 DrawCall，向下找最近的
                                all_draws = collect_draw_calls(actions)
                                nearest = None
                                min_dist = float('inf')
                                for d in all_draws:
                                    dist = abs(d.eventId - cur_event)
                                    if dist < min_dist:
                                        min_dist = dist
                                        nearest = d
                                action = nearest

                            if action is None:
                                result_msg[0] = "No draw call found."
                                return

                            print(f"[Model Extractor] Extracting EID {action.eventId}")

                            mesh_data = extract_mesh_from_draw(controller, action, config)

                            if mesh_data is None or not mesh_data["positions"]:
                                result_msg[0] = f"Failed to extract mesh data from EID {action.eventId}.\nNo vertex data found."
                                return

                            success, filepath, msg = export_mesh(
                                mesh_data, out_dir, config["format"])

                            if success:
                                uv_count = len(mesh_data.get('uv_sets', []))
                                result_msg[0] = (
                                    f"Export successful!\n\n"
                                    f"Event ID: {action.eventId}\n"
                                    f"File: {os.path.basename(filepath)}\n"
                                    f"Vertices: {len(mesh_data['positions']):,}\n"
                                    f"Faces: {len(mesh_data['indices']) // 3:,}\n"
                                    f"Normals: {'Yes' if mesh_data['normals'] else 'No'}\n"
                                    f"UVs: {uv_count} channel(s)\n\n"
                                    f"Saved to: {filepath}"
                                )
                            else:
                                result_msg[0] = f"Export failed: {msg}"

                        else:
                            # 批量模式
                            exported, skipped, errors, msgs = do_batch_extract(
                                controller, config, out_dir)

                            result_msg[0] = (
                                f"Batch export complete!\n\n"
                                f"Exported: {exported}\n"
                                f"Skipped: {skipped}\n"
                                f"Errors: {errors}\n\n"
                                f"Output directory: {out_dir}"
                            )

                    except Exception as e:
                        print(f"[Model Extractor] ERROR in replay thread: {e}")
                        import traceback
                        traceback.print_exc()
                        result_msg[0] = f"Error: {e}"

                ctx.Replay().BlockInvoke(_run)

                mqt.CloseCurrentDialog(True)
                ext.MessageDialog(result_msg[0], "Model Extractor")

            except Exception as e:
                print(f"[Model Extractor] ERROR: {e}")
                import traceback
                traceback.print_exc()

        btn_text = "Select Directory & Extract..." if mode == "single" else "Select Directory & Batch Extract..."
        extract_btn = mqt.CreateButton(_do_extract_click)
        mqt.SetWidgetText(extract_btn, btn_text)
        mqt.AddWidget(btn_row, extract_btn)

        def _do_cancel_click(ctx, widget, text):
            mqt.CloseCurrentDialog(False)

        cancel_btn = mqt.CreateButton(_do_cancel_click)
        mqt.SetWidgetText(cancel_btn, "Cancel")
        mqt.AddWidget(btn_row, cancel_btn)

        mqt.ShowWidgetAsDialog(dialog)
    except Exception as e:
        print(f"[Model Extractor] ERROR: {e}")
        import traceback
        traceback.print_exc()
        try:
            ctx.Extensions().ErrorDialog(f"Error: {e}", "Model Extractor")
        except Exception:
            pass


# ============================================================
# 菜单回调
# ============================================================

def _on_extract_current(ctx, data):
    """菜单回调：提取当前 DrawCall"""
    print("[Model Extractor] Extract Current Draw Call triggered")
    _show_extract_panel(ctx, mode="single")


def _on_batch_extract(ctx, data):
    """菜单回调：批量提取所有 DrawCall"""
    print("[Model Extractor] Batch Extract triggered")
    _show_extract_panel(ctx, mode="batch")


def _on_list_draw_calls(ctx, data):
    """菜单回调：列出所有 DrawCall 信息"""
    try:
        print("[Model Extractor] List Draw Calls triggered")

        if not ctx.IsCaptureLoaded():
            ctx.Extensions().ErrorDialog("No capture is currently loaded.", "Model Extractor")
            return

        result = [None, None]

        def _do_list(controller):
            try:
                result[0] = do_list_draw_calls(controller)
            except Exception as e:
                print(f"[Model Extractor] ERROR in replay thread: {e}")
                import traceback
                traceback.print_exc()
                result[1] = str(e)

        ctx.Replay().BlockInvoke(_do_list)

        if result[1]:
            ctx.Extensions().ErrorDialog(f"Error:\n{result[1]}", "Model Extractor")
        else:
            summary = result[0] or "No draw calls found."
            msg = f"{summary}\n\n(Full list printed to Python Output console)"
            ctx.Extensions().MessageDialog(msg, "Model Extractor - Draw Call Summary")
    except Exception as e:
        print(f"[Model Extractor] ERROR: {e}")
        import traceback
        traceback.print_exc()
        try:
            ctx.Extensions().ErrorDialog(f"Error: {e}", "Model Extractor")
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

    print("[Model Extractor] Extension loaded (v2.1 - multi-UV support)")

    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools,
        ["Model Extractor", "Extract Current Draw Call"],
        _on_extract_current
    )

    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools,
        ["Model Extractor", "Batch Extract All Draw Calls"],
        _on_batch_extract
    )

    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools,
        ["Model Extractor", "List Draw Calls (Console)"],
        _on_list_draw_calls
    )


def unregister():
    """RenderDoc 卸载扩展时调用"""
    global _ctx
    _ctx = None
    print("[Model Extractor] Extension unloaded")
