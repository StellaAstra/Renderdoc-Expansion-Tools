"""
RenderDoc 扩展安装脚本
================================
自动将扩展安装到 RenderDoc 的扩展目录中。

支持的扩展:
  - texture_exporter  (贴图导出工具)
  - model_extractor   (模型提取工具)

使用方法:
  python install_extension.py                  # 安装所有扩展
  python install_extension.py --ext texture_exporter  # 仅安装贴图导出
  python install_extension.py --ext model_extractor   # 仅安装模型提取

或指定自定义目录:
  python install_extension.py --target "C:\\path\\to\\extensions"
"""

import os
import sys
import shutil
import argparse

# 所有可安装的扩展
EXTENSIONS = [
    {
        "dir": "texture_exporter",
        "name": "Texture Exporter",
        "menu": "Tools > Texture Exporter",
    },
    {
        "dir": "model_extractor",
        "name": "Model Extractor",
        "menu": "Tools > Model Extractor",
    },
]


def get_renderdoc_extensions_dir():
    """自动检测 RenderDoc 扩展目录"""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return os.path.join(appdata, "qrenderdoc", "extensions")
    elif sys.platform == "darwin":
        home = os.path.expanduser("~")
        return os.path.join(home, "Library", "Application Support", "qrenderdoc", "extensions")
    else:
        # Linux
        xdg_data = os.environ.get("XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share"))
        return os.path.join(xdg_data, "qrenderdoc", "extensions")
    return None


def install_extension(ext_info, base_dir, target_dir):
    """安装单个扩展"""
    src_dir = os.path.join(base_dir, ext_info["dir"])
    dest_dir = os.path.join(target_dir, ext_info["dir"])

    if not os.path.exists(src_dir):
        print(f"  [跳过] 找不到源目录: {src_dir}")
        return False

    # 如果已存在，先删除
    if os.path.exists(dest_dir):
        print(f"  移除旧版本: {dest_dir}")
        shutil.rmtree(dest_dir)

    shutil.copytree(src_dir, dest_dir)
    print(f"  [成功] {ext_info['name']} -> {dest_dir}")
    return True


def install(target_dir=None, ext_filter=None):
    """安装扩展"""
    if target_dir is None:
        target_dir = get_renderdoc_extensions_dir()

    if target_dir is None:
        print("错误: 无法检测 RenderDoc 扩展目录。")
        print("请使用 --target 参数手动指定。")
        return False

    base_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(target_dir, exist_ok=True)

    print(f"目标目录: {target_dir}\n")

    # 筛选要安装的扩展
    to_install = EXTENSIONS
    if ext_filter:
        to_install = [e for e in EXTENSIONS if e["dir"] == ext_filter]
        if not to_install:
            print(f"错误: 未找到扩展 '{ext_filter}'")
            print(f"可用扩展: {', '.join(e['dir'] for e in EXTENSIONS)}")
            return False

    success_count = 0
    for ext_info in to_install:
        print(f"安装 {ext_info['name']}...")
        if install_extension(ext_info, base_dir, target_dir):
            success_count += 1

    print(f"\n{'='*50}")
    print(f"安装完成! 成功: {success_count}/{len(to_install)}")
    print(f"\n请重启 RenderDoc，然后在 Tools > Manage Extensions 中启用扩展。")
    print(f"启用后可在以下菜单使用:")
    for ext_info in to_install:
        print(f"  - {ext_info['menu']}")
    print(f"{'='*50}")

    return success_count > 0


def main():
    parser = argparse.ArgumentParser(description="安装 RenderDoc 扩展")
    parser.add_argument("--target", type=str, default=None,
                        help="RenderDoc 扩展目录路径 (默认自动检测)")
    parser.add_argument("--ext", type=str, default=None,
                        choices=[e["dir"] for e in EXTENSIONS],
                        help="仅安装指定扩展 (默认安装全部)")
    args = parser.parse_args()

    success = install(args.target, args.ext)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
