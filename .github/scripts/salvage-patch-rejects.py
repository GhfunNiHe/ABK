#!/usr/bin/env python3
"""补救“纯新增”补丁 hunk 漏打导致的编译失败。

上游内核补丁（例如 SUSFS 的 50_add_susfs_in_gki-*.patch）里，文件头 hunk 通常只做
插入，且完全依赖上下文匹配，例如：

    #include <linux/pkeys.h>
   +#if defined(CONFIG_KSU_SUSFS_SUS_MAP) ...
   +#include <linux/susfs_def.h>
   +#endif // ...

     <空行>
    #include <asm/elf.h>

厂商源码树只要在该位置多出 `#include <trace/hooks/mm.h>` 之类的行，整个 hunk 就会
FAILED 并写入 *.rej，而同一个补丁的其余 hunk（函数体里调用 SUSFS_IS_INODE_SUS_MAP
等宏的地方）照常生效，最终编译报
`implicit declaration of function 'SUSFS_IS_INODE_SUS_MAP'`。

本脚本只重放“新增行 hunk”（hunk 内没有任何 `-` 删除行）：

  1. 用 hunk 内最后一个非空上下文行作为锚点，并要求它在目标文件中唯一；
  2. 按上游原文把该 hunk 的新增行插入锚点之后；
  3. 校验 reject 中所有新增行都已出现在目标文件中，才删除对应的 *.rej。

含删除行、锚点不唯一、锚点找不到、目标文件缺失等情况一律保留 *.rej 并给出
`::warning::`，交给 CI 的 Rejects 产物暴露，绝不猜测性改动源码。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def normalize_patch_path(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def patch_targets(patch_text: str) -> set[str]:
    """补丁涉及的文件路径（去掉 a/ 、b/ 前缀）。"""
    targets: set[str] = set()
    for line in patch_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) >= 4:
            targets.add(normalize_patch_path(parts[2]))
            targets.add(normalize_patch_path(parts[3]))
    return {target for target in targets if target}


def path_suffixes(relative: str) -> list[str]:
    """`config/common/fs/x.c` -> 各层后缀，便于匹配不同工作目录下的 reject。"""
    parts = relative.split("/")
    return ["/".join(parts[index:]) for index in range(len(parts))]


def parse_hunks(reject_text: str) -> list[list[str]]:
    """解析 *.rej，返回每个 hunk 的原始行列表（保留 ' ' / '+' / '-' 前缀）。"""
    hunks: list[list[str]] = []
    current: list[str] | None = None
    for line in reject_text.splitlines():
        if HUNK_HEADER_RE.match(line):
            if current is not None:
                hunks.append(current)
            current = []
            continue
        if current is None:
            # 跳过 ---/+++ 文件头
            continue
        current.append(line)
    if current is not None:
        hunks.append(current)
    return hunks


def split_segments(hunk_lines: list[str]) -> list[tuple[str, list[str]]]:
    segments: list[tuple[str, list[str]]] = []
    for line in hunk_lines:
        if line.startswith("\\"):
            # "\ No newline at end of file"
            continue
        if line.startswith("+"):
            kind, content = "add", line[1:]
        elif line.startswith("-"):
            kind, content = "remove", line[1:]
        elif line.startswith(" "):
            kind, content = "context", line[1:]
        elif line == "":
            kind, content = "context", ""
        else:
            kind, content = "context", line

        if segments and segments[-1][0] == kind:
            segments[-1][1].append(content)
        else:
            segments.append((kind, [content]))
    return segments


def plan_hunk(hunk_lines: list[str]) -> tuple[list[tuple[str, list[str], bool]] | None, str]:
    """返回 (插入计划, 失败原因)；计划元素为 (锚点行, 新增行, 是否补一个空行)。"""
    segments = split_segments(hunk_lines)
    if any(kind == "remove" for kind, _ in segments):
        return None, "hunk 含删除行，需要人工处理"

    plan: list[tuple[str, list[str], bool]] = []
    pending: list[str] = []
    anchor: str | None = None
    blank_before = False
    previous_blank = False

    for kind, lines in segments:
        if kind == "add":
            if not pending:
                blank_before = previous_blank
            pending.extend(lines)
            continue

        if pending:
            if anchor is None:
                return None, "新增行之前没有可定位的上下文行"
            plan.append((anchor, pending, blank_before))
            pending = []

        for content in lines:
            if content.strip():
                anchor = content
                previous_blank = False
            else:
                previous_blank = True

    if pending:
        if anchor is None:
            return None, "新增行之前没有可定位的上下文行"
        plan.append((anchor, pending, blank_before))

    if not plan:
        return None, "没有可重放的新增行"
    return plan, ""


def apply_plan(lines: list[str], plan: list[tuple[str, list[str], bool]]) -> tuple[list[str] | None, str]:
    result = list(lines)
    for anchor, additions, blank_before in plan:
        positions = [index for index, line in enumerate(result) if line.rstrip() == anchor.rstrip()]
        if len(positions) != 1:
            return None, f"锚点定位不唯一（命中 {len(positions)} 处）: {anchor.strip()}"
        index = positions[0]
        block = ([""] if blank_before else []) + additions
        result[index + 1:index + 1] = block
    return result, ""


def salvage(root: Path, patch_text: str, dry_run: bool = False, log=print) -> dict[str, int]:
    """重放 root 下与补丁相关的 *.rej。返回统计信息。"""
    targets = patch_targets(patch_text)
    stats = {"repaired": 0, "unresolved": 0, "ignored": 0}

    for reject in sorted(root.rglob("*.rej")):
        relative = reject.relative_to(root).with_suffix("").as_posix()
        if targets and not (set(path_suffixes(relative)) & targets):
            stats["ignored"] += 1
            continue

        target = reject.with_suffix("")
        if not target.is_file():
            log(f"::warning::保留 {reject}：找不到目标文件 {target}")
            stats["unresolved"] += 1
            continue

        hunks = parse_hunks(reject.read_text(encoding="utf-8", errors="surrogateescape"))
        if not hunks:
            log(f"::warning::保留 {reject}：没有解析到 hunk")
            stats["unresolved"] += 1
            continue

        plan: list[tuple[str, list[str], bool]] = []
        reason = ""
        for hunk in hunks:
            hunk_plan, reason = plan_hunk(hunk)
            if hunk_plan is None:
                break
            plan.extend(hunk_plan)

        if not plan:
            log(f"::warning::保留 {reject}：{reason or '无法重放'}")
            stats["unresolved"] += 1
            continue

        additions = [line for _, lines, _ in plan for line in lines]
        original = target.read_text(encoding="utf-8", errors="surrogateescape")
        new_lines, reason = apply_plan(original.splitlines(), plan)
        if new_lines is None:
            log(f"::warning::保留 {reject}：{reason}")
            stats["unresolved"] += 1
            continue

        present = {line.rstrip() for line in new_lines}
        missing = [line for line in additions if line.strip() and line.rstrip() not in present]
        if missing:
            log(f"::warning::保留 {reject}：仍有 {len(missing)} 行新增内容未落盘")
            stats["unresolved"] += 1
            continue

        stats["repaired"] += 1
        if dry_run:
            log(f"可重放 {relative}（新增 {len(additions)} 行），对应 {reject.name} 可移除")
            continue

        target.write_text("\n".join(new_lines) + "\n", encoding="utf-8", errors="surrogateescape")
        reject.unlink()
        log(f"已重放 {relative} 的头部 hunk（新增 {len(additions)} 行），移除 {reject.name}")

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重放“纯新增”补丁 hunk 并清理 *.rej")
    parser.add_argument("--root", default=".", help="运行 patch -p1 的目录，reject 路径相对它")
    parser.add_argument("--patch", required=True, help="补丁文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不修改文件")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"::warning::salvage-patch-rejects: 目录不存在 {root}")
        return 0

    patch_path = Path(args.patch)
    if not patch_path.is_absolute():
        patch_path = root / patch_path
    if not patch_path.is_file():
        print(f"::warning::salvage-patch-rejects: 找不到补丁文件 {patch_path}")
        return 0

    patch_text = patch_path.read_text(encoding="utf-8", errors="surrogateescape")
    stats = salvage(root, patch_text, dry_run=args.dry_run)
    print(
        "salvage-patch-rejects: "
        f"已重放 {stats['repaired']} 个 reject，未处理 {stats['unresolved']} 个，"
        f"跳过无关 reject {stats['ignored']} 个"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
