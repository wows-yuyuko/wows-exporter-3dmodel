"""
data_extractor 命令行入口 —— 用于测试和验证提取功能。

提供与 wowsunpack.exe / pfsunpack2.exe 相似的 CLI 接口。

用法::

    # 列出文件树统计信息
    python -m data_extractor.cli stats D:/Games/Korabli

    # 列出匹配的文件
    python -m data_extractor.cli list D:/Games/Korabli "content/**/*.data"

    # 列出目录内容
    python -m data_extractor.cli ls D:/Games/Korabli content/

    # 提取匹配的文件
    python -m data_extractor.cli extract D:/Games/Korabli "^
        "content/**/*.data" "gui/**/*.png" --output ./extracted

    # 提取单个文件
    python -m data_extractor.cli get D:/Games/Korabli "^
        "content/GameParams.data" --output ./extracted/GameParams.data

    # 提取全部内容（完整解包）
    python -m data_extractor.cli extract D:/Games/Korabli "^
        "**/*" --output ./full_extract

    # 指定版本目录
    python -m data_extractor.cli list D:/Games/Korabli "^
        "gui/**/*" --bin <bin目录名>

    # 只查看会提取哪些文件（dry-run）
    python -m data_extractor.cli extract D:/Games/Korabli "^
        "content/**/*.xml" --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from data_extractor import (
    GameExtractor,
    ExtractorError,
)


def _configured_game_dirs(server: str | None) -> list[str]:
    """config.json 里配置的游戏路径（按 server 过滤，未指定则两服都试）。"""
    from utils.path_utils import get_config_path

    cfg: dict = {}
    path = get_config_path()
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cfg = {}
    keys = (["game_path_lesta", "game_path_wargaming"] if not server
            else ["game_path_lesta"] if server == "lesta" else ["game_path_wargaming"])
    out = []
    for k in keys:
        v = str(cfg.get(k) or "").strip()
        if v:
            out.append(v)
    return out


#: 各子命令的「第二个位置参数」：省略游戏目录时，用户写的第一个值其实属于它
_SECOND_ARG = {
    "list": "patterns",
    "extract": "patterns",
    "ls": "dir",
    "get": "vfs_path",
    "search": "keyword",
}


def _resolve_game_dir(args: argparse.Namespace) -> str:
    """位置参数 > --game > config.json；取不到就提示怎么配。"""
    gd = str(getattr(args, "game", None) or getattr(args, "game_dir", None) or "")

    # 省略了游戏目录、把本来属于第二位置参数的值写在最前面（list "glob" / ls "content"）：
    # 传入值不是已存在的目录时，还回那个参数，再走 config
    if gd and not Path(gd).is_dir():
        slot = _SECOND_ARG.get(str(getattr(args, "command", "") or ""))
        if slot:
            cur = getattr(args, slot, None)
            if isinstance(cur, list):
                setattr(args, slot, [gd] + cur)
            elif cur in (None, ""):
                setattr(args, slot, gd)
            gd = ""

    if gd:
        return gd

    for cand in _configured_game_dirs(getattr(args, "server", None)):
        if Path(cand).is_dir():
            print("[config] 使用游戏目录: %s" % cand, file=sys.stderr)
            return cand

    from utils.path_utils import get_config_path

    raise SystemExit(
        "未指定游戏目录，config.json 里也没有可用的路径。\n"
        "请任选一种：\n"
        "  1) 在 %s 里填 \"game_path_lesta\"（Lesta）或 \"game_path_wargaming\"（WG），值为绝对路径\n"
        "  2) 直接给目录：python -m data_extractor.cli <命令> \"D:/Games/Korabli\" ...\n"
        "  3) 指定用哪个槽位：--server lesta|wg" % get_config_path()
    )


def cmd_stats(args: argparse.Namespace) -> None:
    """显示文件树统计信息"""
    extractor = GameExtractor(args.game_dir, bin_folder=args.bin)
    try:
        extractor.print_stats()
    finally:
        extractor.close()


def cmd_list(args: argparse.Namespace) -> None:
    """按模式列出文件"""
    extractor = GameExtractor(args.game_dir, bin_folder=args.bin)
    try:
        matches = extractor.list_files(args.patterns)
        if not matches:
            print("(无匹配文件)")
            return
        for entry in sorted(matches, key=lambda e: e.path):
            vol = f" [{entry.volume.filename}]" if entry.volume else ""
            size = f" ({entry.file_info.unpacked_size} 字节)" if entry.file_info else ""
            print(f"  {entry.path}{size}{vol}")
        print(f"\n总计: {len(matches)} 个文件")
    finally:
        extractor.close()


def cmd_ls(args: argparse.Namespace) -> None:
    """列出目录内容"""
    extractor = GameExtractor(args.game_dir, bin_folder=args.bin)
    try:
        dir_path = args.dir or ""
        entries = extractor.list_directory(dir_path)
        if not entries:
            print("(空目录)")
            return
        for entry in sorted(entries, key=lambda e: (not e.is_directory, e.path)):
            indicator = "📁 " if entry.is_directory else "📄 "
            size = ""
            if entry.file_info:
                size = f" ({entry.file_info.unpacked_size} 字节)"
            print(f"  {indicator}{Path(entry.path).name}{size}")
        print(f"\n总计: {len(entries)} 个条目")
    finally:
        extractor.close()


def cmd_extract(args: argparse.Namespace) -> None:
    """按模式提取文件"""
    output_dir = Path(args.output)
    extractor = GameExtractor(args.game_dir, bin_folder=args.bin)
    try:
        extracted = extractor.extract(
            args.patterns,
            output_dir,
            flatten=args.flatten,
            strip_prefix=args.strip_prefix,
            dry_run=args.dry_run,
            workers=getattr(args, 'workers', 0),
        )
        if args.dry_run:
            print(f"\n[DRY RUN] 将提取 {len(extracted)} 个文件到: {output_dir}")
            return

        success = [p for p in extracted if p.exists()]
        failed = len(extracted) - len(success)
        total_bytes = sum(p.stat().st_size for p in success)

        print(f"\n提取完成!")
        print(f"  成功: {len(success)} 个文件")
        print(f"  失败: {failed} 个文件")
        print(f"  总大小: {_format_size(total_bytes)}")
        print(f"  输出目录: {output_dir.resolve()}")
    finally:
        extractor.close()


def cmd_get(args: argparse.Namespace) -> None:
    """提取单个文件"""
    output_path = Path(args.output)
    extractor = GameExtractor(args.game_dir, bin_folder=args.bin)
    try:
        result = extractor.extract_single(args.vfs_path, output_path)
        size = result.stat().st_size
        print(f"✅ 已提取: {result} ({_format_size(size)})")
    except ExtractorError as e:
        print(f"❌ {e}")
        sys.exit(1)
    finally:
        extractor.close()


def cmd_search(args: argparse.Namespace) -> None:
    """搜索文件名包含关键字的文件"""
    extractor = GameExtractor(args.game_dir, bin_folder=args.bin)
    try:
        keyword = args.keyword.lower()
        matches = []
        for path, entry in extractor.file_tree.items():
            if not entry.is_directory and keyword in path.lower():
                matches.append(entry)

        if not matches:
            print(f"(未找到包含 '{args.keyword}' 的文件)")
            return

        for entry in sorted(matches, key=lambda e: e.path):
            vol = f" [{entry.volume.filename}]" if entry.volume else ""
            size = f" ({entry.file_info.unpacked_size} 字节)" if entry.file_info else ""
            print(f"  {entry.path}{size}{vol}")
        print(f"\n总计: {len(matches)} 个文件")
    finally:
        extractor.close()


def _format_size(bytes_val: int) -> str:
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="战舰世界资源提取工具 (data_extractor)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 游戏目录可以省略 —— 会自动读项目 config.json 里的
  # game_path_lesta / game_path_wargaming（也可用 --server lesta|wg 指定槽位）
  python -m data_extractor.cli stats

  # 也可以显式给目录（位置参数或 --game 都行）
  python -m data_extractor.cli stats <游戏目录>
  python -m data_extractor.cli list --game <游戏目录> "content/**/*.data"

  # 列出所有 .data 文件（省略目录时可直接把 glob 写在第一个位置参数）
  python -m data_extractor.cli list "content/**/*.data"

  # 提取所有 PNG 图片
  python -m data_extractor.cli extract "gui/**/*.png" --output ./extracted

  # 指定版本
  python -m data_extractor.cli list "content/**/*" --bin <bin目录名>

  # 完整解包（提取全部文件）
  python -m data_extractor.cli extract "**/*" --output ./full_extract
        """,
    )
    parser.add_argument(
        "--bin", "-b",
        help="指定版本文件夹名（如 <bin目录名>），不指定则自动使用最新版本",
    )

    # --game / --server 挂在每个子命令上，这样 "cli stats --game X" 与
    # "cli --bin B stats X" 两种写法都成立
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--game", "-g", default=None,
                        help="游戏根目录（优先级高于位置参数；都不给则读 config.json）")
    common.add_argument("--server", choices=("lesta", "wg"), default=None,
                        help="从 config.json 取哪一服的路径（默认依次试 lesta / wg）")

    sub = parser.add_subparsers(title="命令", dest="command")

    # stats
    p_stats = sub.add_parser("stats", parents=[common], help="显示文件树统计信息")
    p_stats.add_argument("game_dir", nargs="?", default=None,
                        help="游戏根目录（省略时用 --game 或 config.json）")
    p_stats.set_defaults(func=cmd_stats)

    # list
    p_list = sub.add_parser("list", parents=[common], help="按 glob 模式列出文件")
    p_list.add_argument("game_dir", nargs="?", default=None,
                        help="游戏根目录（省略时用 --game 或 config.json）")
    p_list.add_argument("patterns", nargs="+", help="glob 模式（如 content/**/*.data）")
    p_list.set_defaults(func=cmd_list)

    # ls
    p_ls = sub.add_parser("ls", parents=[common], help="列出虚拟目录内容")
    p_ls.add_argument("game_dir", nargs="?", default=None,
                        help="游戏根目录（省略时用 --game 或 config.json）")
    p_ls.add_argument("dir", nargs="?", default="", help="虚拟目录路径")
    p_ls.set_defaults(func=cmd_ls)

    # extract
    p_extract = sub.add_parser("extract", parents=[common], help="按 glob 模式提取文件")
    p_extract.add_argument("game_dir", nargs="?", default=None,
                        help="游戏根目录（省略时用 --game 或 config.json）")
    p_extract.add_argument("patterns", nargs="+", help="glob 模式")
    p_extract.add_argument("--output", "-o", default="./extracted", help="输出目录")
    p_extract.add_argument("--flatten", "-f", action="store_true", help="压平目录结构")
    p_extract.add_argument("--strip-prefix", action="store_true", help="去除匹配前缀")
    p_extract.add_argument("--dry-run", "-n", action="store_true", help="仅预览不写入")
    p_extract.add_argument("--workers", "-w", type=int, default=0,
                           help="并行进程数; 默认0=自动(CPU核数, 上限8), 1=顺序")
    p_extract.set_defaults(func=cmd_extract)

    # get (single file)
    p_get = sub.add_parser("get", parents=[common], help="提取单个文件")
    p_get.add_argument("game_dir", nargs="?", default=None,
                        help="游戏根目录（省略时用 --game 或 config.json）")
    p_get.add_argument("vfs_path", help="虚拟路径（如 content/GameParams.data）")
    p_get.add_argument("--output", "-o", default="./extracted", help="输出文件路径")
    p_get.set_defaults(func=cmd_get)

    # search
    p_search = sub.add_parser("search", parents=[common], help="按文件名关键字搜索")
    p_search.add_argument("game_dir", nargs="?", default=None,
                        help="游戏根目录（省略时用 --game 或 config.json）")
    p_search.add_argument("keyword", help="搜索关键字")
    p_search.set_defaults(func=cmd_search)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    args.game_dir = _resolve_game_dir(args)
    args.func(args)


if __name__ == "__main__":
    main()
