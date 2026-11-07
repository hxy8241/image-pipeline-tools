#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
folder_balance.py — 文件夹容量均衡工具（合并小目录 + 平分大目录）

对一个目录树自动执行两步处理（直接执行，无预览、无确认）：
  1. 合并：自底向上递归，递归文件数 < --min-files 的非 root 目录触发合并：
     - 总数仍 < --min-files：并入大文件夹 / 平铺到 root / 交由父层收集；
     - 总数足够：成组合并，每组 >= MERGE_GROUP_MIN(默认 1000)；
       总数不足 1000 时合并为一个目录。
     处理后所有非 root 目录的文件数 >= --min-files。
  2. 拆分：自底向上扫描，把直接文件数 > --max-files 的目录平分为纯数字子目录
    （0、1、2……），使每个新子目录的直接文件数严格 < --max-files。

执行顺序说明：
  先合并后拆分。合并可能把文件推入大文件夹使其超过 --max-files，
  因此拆分必须在合并之后。拆分为均分（各桶数量差不超过 1），
  桶的最小文件数为 floor((max_files + 1) / 2)，
  本脚本会强制校验 max_files >= 2 * min_files，
  保证拆出的桶不会小于 --min-files，即拆分不会破坏合并的结果。

用法：
  python folder_balance.py "D:\\Photos"
  python folder_balance.py "D:\\Photos" --min-files 500 --max-files 5000

注意：
  所有文件移动/改名/删空目录操作对 WinError 5（拒绝访问）/ 32（共享冲突）
  自动指数退避重试（约 10 秒）——覆盖 Windows 索引器、杀软实时扫描、
  OneDrive 等同步盘、SMB 网络共享 oplock 造成的瞬时锁。

  Windows 默认路径长度限制 260 字符。若目录层级极深导致移动失败，
  请以管理员身份运行以下命令开启长路径支持后重试：
    New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
      -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
"""

import argparse
import os
import random
import re
import shutil
import stat
import sys
import time

STAGING_NAME = "__consolidate_staging__"
MERGE_GROUP_MIN = 2000  # 合并成组时每组至少的文件数；总数不足时合并为一个目录
RECOVER_PREFIX = "__recover_"
BUCKET_NAME_RE = re.compile(r"^\d+$")


class ConsolidationError(Exception):
    """合并过程中遇到不可恢复的错误（如遗留暂存区、目标路径并发冲突）。"""
    pass


def _is_link_or_junction(path):
    """符号链接或 Windows 目录联接（junction）/挂载点检测。

    注意：os.path.islink 对 Windows junction 返回 False，必须检查 reparse 标记，
    否则 junction 指向的树外目录会被当作真实子目录遍历并搬走其中的文件。
    """
    if os.path.islink(path):
        return True
    if os.name == "nt":
        try:
            attrs = os.lstat(path).st_file_attributes
        except (OSError, AttributeError):
            return False
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _is_script_artifact(name):
    """判断是否为脚本暂存区目录名。

    __recover_*__ 恢复目录不在豁免之列——其中的文件是真实数据，
    会重新参与后续的合并/拆分，全自动闭环，无需手工介入。
    consolidated_* 前缀是旧版产物命名，本版已不再使用，不再豁免。
    """
    return name == STAGING_NAME


def _is_transient_lock_error(exc):
    """Windows/SMB 常见的瞬时文件锁错误：WinError 5（拒绝访问）/ 32（共享冲突）。

    触发源：Windows 索引器、杀毒实时扫描、OneDrive 等同步盘、网络共享的
    oplock。这类句柄通常很快释放，适合重试而不是立即报错。
    """
    return getattr(exc, "winerror", None) in (5, 32)


def _with_retry(func, *args, attempts=6, base_delay=0.3):
    """执行文件系统操作，对瞬时锁错误做指数退避重试（最多约 10 秒）。

    只有 WinError 5/32 才重试；其余错误（路径不存在、权限真实不足等）直接抛出。
    """
    for i in range(attempts):
        try:
            return func(*args)
        except OSError as e:
            if not _is_transient_lock_error(e) or i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))


def _preserve_dir(path, parent):
    """出错时绝不删除数据：把目录改名为 __recover_<时间戳>[_N]__ 保留，返回新路径。"""
    ts = int(time.time())
    for seq in range(100):
        suffix = f"_{seq}" if seq else ""
        recover = os.path.join(parent, f"{RECOVER_PREFIX}{ts}{suffix}__")
        try:
            _with_retry(os.rename, path, recover)
            return recover
        except OSError:
            continue
    return path  # 全部失败则原地保留


def _discard_or_preserve(staging, parent):
    """暂存区收尾：空则删除；非空（含移动失败的文件）则改名保留，绝不丢数据。"""
    try:
        if os.listdir(staging):
            recover = _preserve_dir(staging, parent)
            print(f"  [警告] 暂存区内仍有文件，已保留为: {recover}", file=sys.stderr)
        else:
            _with_retry(os.rmdir, staging, attempts=3)
    except OSError:
        pass


def _cleanup_all_staging(root):
    """收尾处理 root 下所有遗留暂存区：空的删除，非空的改名保留（绝不丢数据）。"""
    for dp, _dn, _fns in os.walk(root, topdown=False):
        if os.path.basename(dp) == STAGING_NAME:
            _discard_or_preserve(dp, os.path.dirname(dp))


# ────────────────────────── 合并模块 ──────────────────────────

def scan_dir(dir_path, min_files):
    """扫描 dir_path 的直接子目录，区分大文件夹（保留）与小文件夹（合并后平铺）。"""
    large_dirs = []
    small_folders = []
    direct_files = []

    def onerror(err):
        print(f"  [警告] 无法读取目录，已跳过：{err}", file=sys.stderr)

    try:
        entries = sorted(os.listdir(dir_path))
    except OSError as e:
        # 权限不足等情况：返回空列表，本层视为无可合并项（不产生 staging）
        print(f"  [警告] 无法列出目录 {dir_path}，跳过：{e}", file=sys.stderr)
        return large_dirs, small_folders, direct_files

    for name in entries:
        if _is_script_artifact(name):
            continue
        path = os.path.join(dir_path, name)
        if os.path.isdir(path) and not _is_link_or_junction(path):
            files = []
            for dp, dn, fns in os.walk(path, onerror=onerror):
                # 跳过脚本产物目录与 junction（防止搬动树外文件）。不跳过合并文件夹
                #（0,1,2...），它们由 group_small_folders 产生，必然 >= min_files，
                # 因此含合并子文件夹的目录一定是大文件夹，不会被当作小文件夹重新合并。
                dn[:] = [d for d in dn
                         if not _is_script_artifact(d)
                         and not _is_link_or_junction(os.path.join(dp, d))]
                for f in fns:
                    files.append(os.path.join(dp, f))
            count = len(files)
            if count >= min_files:
                large_dirs.append((name, path, count))
            else:
                small_folders.append((name, path, count, files))
        elif _is_link_or_junction(path) and os.path.isdir(path):
            # junction/符号链接目录：只告警，绝不遍历或移动其内容
            print(f"  [警告] 跳过链接/联接目录（不处理其内容）: {path}", file=sys.stderr)
        else:
            direct_files.append(path)

    return large_dirs, small_folders, direct_files


def group_small_folders(small_folders, target):
    """把小文件夹贪心累加分组，使每组文件总数 >= target。

    总数不足 target 时循环中不会闭合任何组，收尾自然合为单组；
    最后一组不足 target 时并入前一组，保证不会产生小尾组。
    """
    groups = []
    current = []
    current_sum = 0
    for sf in small_folders:
        current.append(sf)
        current_sum += sf[2]
        if current_sum >= target:
            groups.append(current)
            current = []
            current_sum = 0
    if current:
        if groups:
            groups[-1].extend(current)
        else:
            groups.append(current)
    return groups


class NameRegistry:
    """按目录跟踪已占用的文件名，平铺合并时避免重名覆盖，且避免 O(N^2) 磁盘 I/O。"""

    def __init__(self):
        self._used = {}

    def unique(self, target_dir, filename):
        key = os.path.abspath(target_dir)
        used = self._used.get(key)
        if used is None:
            used = set(os.listdir(key)) if os.path.isdir(key) else set()
            self._used[key] = used
        name, ext = os.path.splitext(filename)
        candidate = filename
        counter = 1
        while candidate in used:
            candidate = f"{name}_{counter}{ext}"
            counter += 1
        used.add(candidate)
        return candidate

    def release(self, target_dir, filename):
        """释放已预留但实际未写入的文件名（移动失败时调用，避免序号空跳）。"""
        key = os.path.abspath(target_dir)
        used = self._used.get(key)
        if used is not None:
            used.discard(filename)


def remove_empty_dirs(root):
    """自底向上删除 root 下所有空子目录（不删 root 本身，跳过脚本产物与 junction）。

    junction 的 rmdir 会删除联接本身或其目标（取决于系统），因此遍历阶段直接剪枝。
    """
    root_abs = os.path.abspath(root)

    def onerror(err):
        print(f"  [警告] 清理空目录时跳过：{err}", file=sys.stderr)

    candidates = []
    for dirpath, dirnames, _fns in os.walk(root, topdown=True, onerror=onerror):
        dirnames[:] = [d for d in dirnames
                       if not _is_script_artifact(d)
                       and not _is_link_or_junction(os.path.join(dirpath, d))]
        candidates.append(dirpath)
    for dirpath in reversed(candidates):  # reversed(topdown) == 自底向上
        if os.path.abspath(dirpath) == root_abs:
            continue
        try:
            _with_retry(os.rmdir, dirpath, attempts=3)
        except OSError:
            pass


def gen_merged_names(reserved, count):
    """生成 count 个数字文件夹名（0,1,2...），跳过 reserved 中的名字。"""
    reserved_set = set(reserved)
    names = []
    i = 0
    while len(names) < count:
        candidate = str(i)
        if candidate not in reserved_set:
            names.append(candidate)
        i += 1
    return names


def merge_level(dir_path, min_files, root_path, stats):
    """合并 dir_path 下的小直接子目录及直放文件（文件平铺）。

    stats["failures"] 累计文件移动失败数，用于退出码。
    """
    staging = os.path.join(dir_path, STAGING_NAME)

    # Phase 0: 遗留暂存区自动改名保留。改名后 scan_dir 会把它当作普通
    # 子目录纳入本轮合并，真正全自动闭环，无需手工介入。
    if os.path.lexists(staging):
        recover = _preserve_dir(staging, dir_path)
        if recover == staging:
            raise ConsolidationError(
                f"遗留暂存区 {staging} 无法改名保留，请手动处理后重试。"
            )
        print(f"  [提示] 发现遗留暂存区，已自动改名为 {recover}，本轮重新纳入合并。")

    large_dirs, small_folders, direct_files = scan_dir(dir_path, min_files)

    items = list(small_folders)
    if direct_files and (small_folders or len(direct_files) < min_files):
        items.append(("__direct_files__", dir_path, len(direct_files), direct_files))

    if not items:
        return

    total = sum(it[2] for it in items)

    failures = []

    if total < min_files:
        # ---- 总和不足：不新建 < min_files 的子目录 ----
        if large_dirs:
            target_dir = large_dirs[0][1]
            print(f"\n[{dir_path}] 零散文件 {total} < {min_files}，"
                  f"并入大文件夹 {large_dirs[0][0]}/")
            registry = NameRegistry()
            for _name, _path, _cnt, files in items:
                for src in files:
                    fn = os.path.basename(src)
                    reserved = registry.unique(target_dir, fn)
                    dest = os.path.join(target_dir, reserved)
                    try:
                        _with_retry(shutil.move, src, dest)
                    except Exception as e:
                        registry.release(target_dir, reserved)
                        failures.append((src, str(e)))
                        print(f"  [失败] {src}: {e}", file=sys.stderr)
            remove_empty_dirs(dir_path)
        elif dir_path == root_path:
            print(f"\n[{dir_path}] 零散文件 {total} < {min_files}，"
                  f"平铺到 root 并删除所有子目录")
            if small_folders:
                os.makedirs(staging)
                registry = NameRegistry()
                for _name, _path, _cnt, files in small_folders:
                    for src in files:
                        fn = os.path.basename(src)
                        reserved = registry.unique(staging, fn)
                        dest = os.path.join(staging, reserved)
                        try:
                            _with_retry(shutil.move, src, dest)
                        except Exception as e:
                            registry.release(staging, reserved)
                            failures.append((src, str(e)))
                            print(f"  [失败] {src}: {e}", file=sys.stderr)
                remove_empty_dirs(dir_path)
                registry2 = NameRegistry()
                for f in os.listdir(staging):
                    src = os.path.join(staging, f)
                    if os.path.isfile(src) or os.path.islink(src):
                        reserved = registry2.unique(dir_path, f)
                        dest = os.path.join(dir_path, reserved)
                        try:
                            _with_retry(shutil.move, src, dest)
                        except Exception as e:
                            registry2.release(dir_path, reserved)
                            failures.append((src, str(e)))
                            print(f"  [失败] {src}: {e}", file=sys.stderr)
                # 移动失败的文件仍留在 staging，绝不能 rmtree —— 非空则改名保留
                _discard_or_preserve(staging, dir_path)
                remove_empty_dirs(dir_path)
                for name in os.listdir(dir_path):
                    sub = os.path.join(dir_path, name)
                    if (os.path.isdir(sub) and not _is_link_or_junction(sub)
                            and not _is_script_artifact(name)):
                        leftover = sum(1 for _ in os.walk(sub) for __ in _[2])
                        if leftover > 0:
                            print(f"  [警告] 子目录 {name}/ 仍残留 {leftover} 个文件"
                                  f"（可能因权限/锁定未被收集），请手动检查。",
                                  file=sys.stderr)
        else:
            print(f"\n[{dir_path}] 零散文件 {total} < {min_files}，"
                  f"保持原样（由父层合并）")
        if failures:
            stats["failures"] += len(failures)
            print(f"  警告：{len(failures)} 个文件移动失败，已保留在原位置。",
                  file=sys.stderr)
        return

    # ---- 总和足够：分组合并 ----
    # 触发合并看 min_files，成组目标为 max(MERGE_GROUP_MIN, min_files)：
    # 默认即 2000 —— 每组 >= 2000；总数不足 2000 时自然合为单组（单目录）。
    groups = group_small_folders(items, max(MERGE_GROUP_MIN, min_files))
    k = len(groups)
    group_totals = [sum(it[2] for it in g) for g in groups]

    reserved = os.listdir(dir_path)  # 排除所有已有名字（含文件），避免 Phase 4 冲突
    merged_names = gen_merged_names(reserved, k)

    n_small = len(small_folders)
    print(f"\n[{dir_path}] 合并 {n_small} 个小文件夹 + "
          f"{len(direct_files)} 个直放文件 -> {k} 个合并文件夹")
    for i, (g, gtotal) in enumerate(zip(groups, group_totals)):
        names = ", ".join(it[0] for it in g if it[0] != "__direct_files__")
        has_direct = any(it[0] == "__direct_files__" for it in g)
        extra = f" + {len(direct_files)} 直放" if has_direct else ""
        print(f"  组{i}: [{names}]{extra} -> {gtotal} 个文件")

    # Phase 1: 建暂存区及分组目录
    os.makedirs(staging)
    for i in range(k):
        os.makedirs(os.path.join(staging, str(i)))

    # Phase 2: 把各 item 的文件平铺搬进暂存区分组
    registry = NameRegistry()
    for i, g in enumerate(groups):
        target = os.path.join(staging, str(i))
        for _name, _path, _cnt, files in g:
            for src in files:
                fn = os.path.basename(src)
                reserved = registry.unique(target, fn)
                dest = os.path.join(target, reserved)
                try:
                    _with_retry(shutil.move, src, dest)
                except Exception as e:
                    registry.release(target, reserved)
                    failures.append((src, str(e)))
                    print(f"  [失败] {src}: {e}", file=sys.stderr)

    # Phase 3: 清理本层被搬空的目录
    remove_empty_dirs(dir_path)

    # Phase 4+5: 预校验并逐组移回（校验与移动紧挨着，最小化 TOCTOU 缝隙）
    # 用 os.rename 而非 shutil.move：src/dst 在同一文件系统，os.rename 是原子的，
    # 且 dst 已存在时直接报错（绝不把 src 嵌套进 dst 内部）。
    # 出错时绝不 rmtree(staging) —— 此时 staging 是数据的唯一副本，改名保留。
    for i in range(k):
        src_folder = os.path.join(staging, str(i))
        dest_folder = os.path.join(dir_path, merged_names[i])
        if os.path.lexists(dest_folder):
            if os.path.isdir(dest_folder) and not os.listdir(dest_folder):
                _with_retry(os.rmdir, dest_folder)
            else:
                recover = _preserve_dir(staging, dir_path)
                raise ConsolidationError(
                    f"目标路径 {dest_folder} 已存在且为文件/非空目录/符号链接。"
                    f"已搬走的数据完整保留在 {recover}，请手动处理后重试。"
                )
        try:
            _with_retry(os.rename, src_folder, dest_folder)
        except OSError as e:
            recover = _preserve_dir(staging, dir_path)
            raise ConsolidationError(
                f"移动 {src_folder} -> {dest_folder} 重试后仍失败: {e}。"
                f"已搬走的数据完整保留在 {recover}，请手动处理后重试。"
            )

    _discard_or_preserve(staging, dir_path)  # 正常时只剩空分组目录；异常遗留则保留

    if failures:
        stats["failures"] += len(failures)
        print(f"  警告：{len(failures)} 个文件移动失败，已保留在原位置。",
              file=sys.stderr)


def _list_subdirs(dir_path):
    """返回 dir_path 下需要递归处理的直接子目录路径列表（跳过脚本产物与 junction）。"""
    subdirs = []
    try:
        entries = sorted(os.listdir(dir_path))
    except OSError as e:
        print(f"  [警告] 无法列出目录 {dir_path}，跳过：{e}", file=sys.stderr)
        return subdirs
    for name in entries:
        if _is_script_artifact(name):
            continue
        path = os.path.join(dir_path, name)
        if os.path.isdir(path) and not _is_link_or_junction(path):
            subdirs.append(path)
    return subdirs


def consolidate_dir(root, min_files, root_path, stats):
    """自底向上处理所有层级（显式栈，无递归深度限制）。

    用显式栈收集先序，再逆序处理，保证子目录先于父目录。
    """
    # 第一趟：显式栈收集先序（父先子后）
    order = []
    stack = [root]
    while stack:
        d = stack.pop()
        order.append(d)
        stack.extend(_list_subdirs(d))
    # 第二趟：逆序 == 自底向上
    for d in reversed(order):
        merge_level(d, min_files, root_path, stats)


def verify_result(root, min_files):
    """校验结果：所有非 root 的目录递归文件数都应 >= min_files。返回违规目录列表。

    单趟实现：先自顶向下收集目录（剪枝脚本产物与 junction），再自底向上汇总
    递归计数，避免 O(目录数 × 文件数) 的嵌套 walk。
    """
    dirs = []
    for dp, dn, _fns in os.walk(root, topdown=True):
        dn[:] = [d for d in dn
                 if not _is_script_artifact(d)
                 and not _is_link_or_junction(os.path.join(dp, d))]
        dirs.append(dp)

    counts = {}
    skip_subtrees = set()  # listdir 失败的目录，其子树计数不可靠
    for dp in reversed(dirs):  # 自底向上：子目录计数先算好
        if dp in skip_subtrees:
            continue
        try:
            entries = os.listdir(dp)
        except OSError:
            skip_subtrees.add(dp)
            continue
        nfiles = 0
        subtotal = 0
        reliable = True
        for name in entries:
            if _is_script_artifact(name):
                continue
            p = os.path.join(dp, name)
            if os.path.isdir(p) and not _is_link_or_junction(p):
                if p in skip_subtrees:
                    reliable = False  # 子目录计数不可靠，父目录也不可靠
                else:
                    subtotal += counts.get(p, 0)
            else:
                nfiles += 1
        if reliable:
            counts[dp] = nfiles + subtotal
        else:
            skip_subtrees.add(dp)

    return [(dp, counts[dp]) for dp in dirs
            if dp != root and dp in counts and counts[dp] < min_files]


# ────────────────────────── 拆分模块 ──────────────────────────

def plan_buckets(file_count, threshold):
    """返回每个桶应放入的文件数列表，保证最大值严格小于 threshold。"""
    capacity = threshold - 1
    bucket_count = (file_count + capacity - 1) // capacity
    base, extra = divmod(file_count, bucket_count)
    return [base + 1 if i < extra else base for i in range(bucket_count)]


def next_bucket_index(existing_dirnames):
    """根据已有目录名，返回下一个可用的数字桶编号。"""
    used = [int(name) for name in existing_dirnames if BUCKET_NAME_RE.match(name)]
    return max(used) + 1 if used else 0


def build_destination(bucket_dir, filename):
    """构造目标路径；若同名文件已存在则追加序号，绝不覆盖。"""
    dest = os.path.join(bucket_dir, filename)
    if not os.path.lexists(dest):
        return dest
    stem, ext = os.path.splitext(filename)
    for i in range(1, 10000):
        candidate = os.path.join(bucket_dir, f"{stem}__dup{i}{ext}")
        if not os.path.lexists(candidate):
            return candidate
    raise RuntimeError(f"为 {filename} 寻找空闲目标名时重名超过 9999 个，放弃移动")


def split_tree(root, threshold, execute):
    """递归扫描 root，按需平分。返回统计字典。

    先 topdown=True 收集目录并剪枝（脚本产物 / junction），再按先序逆序处理
    （等价自底向上）。topdown=False 时改 dirnames 无效，必须分两趟。
    """
    stats = {"dirs_scanned": 0, "dirs_split": 0,
             "moved": 0, "renamed": 0, "errors": 0}

    def on_walk_error(error):
        print(f"  [警告] 扫描失败: {error}", file=sys.stderr)
        stats["errors"] += 1

    # 第一趟：topdown 收集 + 剪枝
    dirs = []
    for dirpath, dirnames, _fns in os.walk(root, topdown=True, onerror=on_walk_error):
        dirnames[:] = [d for d in dirnames
                       if not _is_script_artifact(d)
                       and not _is_link_or_junction(os.path.join(dirpath, d))]
        dirs.append(dirpath)

    # 第二趟：先序逆序 == 自底向上
    for dirpath in reversed(dirs):
        stats["dirs_scanned"] += 1
        try:
            entries = os.listdir(dirpath)
        except OSError as e:
            print(f"  [警告] 无法列出目录 {dirpath}，跳过：{e}", file=sys.stderr)
            stats["errors"] += 1
            continue
        filenames = [f for f in entries
                     if not os.path.isdir(os.path.join(dirpath, f))]
        file_count = len(filenames)
        if file_count <= threshold:
            continue

        sizes = plan_buckets(file_count, threshold)
        # 预留所有已有名字（含 junction、文件），防止桶名与任何现有条目冲突。
        start = next_bucket_index(entries)
        bucket_names = [str(start + i) for i in range(len(sizes))]

        tag = "执行" if execute else "预演"
        detail = "、".join(f"{name}/({size})"
                          for name, size in zip(bucket_names, sizes))
        print(f"[{tag}] {dirpath}")
        print(f"        {file_count} 个文件 -> 新建 {len(sizes)} 个目录: {detail}")
        stats["dirs_split"] += 1

        if not execute:
            stats["moved"] += file_count
            continue

        ordered = random.sample(filenames, k=len(filenames))
        cursor = 0
        for name, size in zip(bucket_names, sizes):
            bucket_dir = os.path.join(dirpath, name)
            if os.path.lexists(bucket_dir):
                # 正常不会触发（桶名已预留），这里防御并发/同名冲突，
                # 防止 exist_ok=True 静默吞掉冲突、文件被写进意外目录（如 junction 目标）。
                print(f"  [错误] 桶目录 {bucket_dir} 已存在，跳过该桶", file=sys.stderr)
                stats["errors"] += 1
                cursor += size
                continue
            try:
                os.makedirs(bucket_dir)
            except OSError as exc:
                print(f"  [错误] 无法创建目录 {bucket_dir}: {exc}", file=sys.stderr)
                stats["errors"] += 1
                cursor += size
                continue

            for filename in ordered[cursor:cursor + size]:
                source = os.path.join(dirpath, filename)
                try:
                    target = build_destination(bucket_dir, filename)
                    _with_retry(shutil.move, source, target)
                    stats["moved"] += 1
                    if os.path.basename(target) != filename:
                        stats["renamed"] += 1
                        print(f"  [提示] 目标重名，已改名: {filename} "
                              f"-> {os.path.basename(target)}")
                except (OSError, RuntimeError) as exc:
                    print(f"  [错误] 移动失败 {source}: {exc}", file=sys.stderr)
                    stats["errors"] += 1
            cursor += size

    return stats


# ────────────────────────── 主入口 ──────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="目录容量均衡：< --min-files 的目录合并，> --max-files 的目录平分（直接执行）",
    )
    parser.add_argument("root", help="要处理的根目录")
    parser.add_argument("--min-files", type=int, default=500,
                        help="每个非 root 文件夹的最少文件数（默认 500）")
    parser.add_argument("--max-files", type=int, default=5000,
                        help="每个文件夹直接文件数上限，超过则平分（默认 5000）")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        parser.error(f"路径不存在或不是目录: {root}")

    root_base = os.path.basename(root)
    if root_base == STAGING_NAME:
        parser.error(f"根目录 {root} 是脚本产物目录（{root_base}），已拒绝执行。")

    min_files = args.min_files
    max_files = args.max_files
    if min_files < 1:
        parser.error("--min-files 必须 >= 1")
    if max_files < 2:
        parser.error("--max-files 必须 >= 2")
    if max_files < 2 * min_files:
        parser.error(
            f"--max-files({max_files}) 必须 >= 2 * --min-files({min_files})，"
            f"否则拆分产生的桶可能小于 --min-files，与合并目标冲突。"
        )

    print(f"根目录: {root}")
    merge_target = max(MERGE_GROUP_MIN, min_files)
    print(f"合并阈值: < {min_files} 触发，成组 >= {merge_target}    "
          f"拆分阈值: > {max_files} 平分")

    # ---------- 第 1 步：合并 ----------
    print("\n===== 第 1 步：合并小目录 =====")
    merge_stats = {"failures": 0}
    try:
        consolidate_dir(root, min_files, root, merge_stats)
    except ConsolidationError as e:
        print(f"\n错误：{e}", file=sys.stderr)
        _cleanup_all_staging(root)
        sys.exit(1)
    except KeyboardInterrupt:
        # P1-4：Ctrl-C 中断时保护暂存区内的数据（改名保留，绝不删除）
        print("\n用户中断，正在保护暂存区数据...", file=sys.stderr)
        _cleanup_all_staging(root)
        sys.exit(130)
    except Exception as e:
        # P1-4：意外异常（权限/OSError 等）同样保护暂存区数据
        print(f"\n意外错误（{type(e).__name__}: {e}），正在保护暂存区数据...",
              file=sys.stderr)
        _cleanup_all_staging(root)
        sys.exit(1)
    violations = verify_result(root, min_files)
    if violations:
        print(f"警告：以下 {len(violations)} 个非 root 目录文件数仍 < {min_files}：",
              file=sys.stderr)
        for dp, total in violations:
            print(f"  {dp}: {total} 个文件", file=sys.stderr)
        print("（这些目录通常是文件移动失败后残留的空壳目录，或因权限/锁定无法被收集。"
              "请手动检查并清理。）", file=sys.stderr)
    else:
        print(f"合并校验通过：所有非 root 目录都 >= {min_files} 个文件。")

    # ---------- 第 2 步：拆分 ----------
    print("\n===== 第 2 步：平分大目录 =====")
    stats = split_tree(root, max_files, execute=True)
    if stats["dirs_split"] == 0:
        print("没有超过阈值的目录，无需拆分。")

    # ---------- 汇总 ----------
    # M-8：退出码计入合并失败数与校验违规数，保证批处理/任务计划可用
    total_errors = stats["errors"] + merge_stats["failures"] + len(violations)
    print("\n" + "=" * 50)
    print(f"拆分目录数: {stats['dirs_split']}  |  已移动文件: {stats['moved']}  |  "
          f"重名改名: {stats['renamed']}  |  错误: {total_errors}"
          f"（拆分 {stats['errors']} + 合并失败 {merge_stats['failures']}"
          f" + 校验违规 {len(violations)}）")
    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
