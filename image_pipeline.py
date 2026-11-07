#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片/压缩包统一整理: 校验 -> (缩放)转 JXL / 原样搬运 -> 清空源目录。

用法:
    python image_pipeline.py <源目录> <目标目录> [--dry-run]

处理规则:
  - 静态图片(源目录里的, 以及压缩包里的):
      * 大图(长边超出屏幕分辨率) -> 等比缩放到屏幕内, 存为 .jxl
      * 小图 + 高压缩率格式(webp/jxl/heic/heif/avif) -> 原样搬到目标目录
      * 小图 + 其余格式(jpg/png/bmp/tif/ico...) -> 原分辨率转成 .jxl
  - 破损图片(解码失败) -> <目标目录>/_broken_images/ 下(保留相对路径), 源文件删除
  - 动图(多帧) -> 视为非图片: 目录里的原地不动; 压缩包里的解压回源目录
  - 压缩包 -> 解压到临时目录, 静态图片走上面同一套流程, 其余文件解压回源目录,
    全部成功后才删除压缩包; 加密/损坏/包内没有图片的包保留

目录结构(源目录的相对路径会保留):
  源目录   sub/a.jpg          sub/vol.zip(内含 001.jpg, anim.gif, notes.txt)
  目标目录 sub/a.jxl          sub/vol/001.jxl
  源目录   sub/vol/notes.txt  sub/vol/anim.gif
  目标目录 _broken_images/... (破损图片)

注意:
  - 动图按"非图片"处理, 会留在源目录; 源目录最终不再有静态图片和压缩包
  - 没有对应解码器时(如未装 pillow_heif 的 heic), 无法识别动图/破损,
    高压缩率格式按静态图原样搬运, 其余格式视为破损
  - JXL 用 jxlinfo 读尺寸、用 djxl 解码(仅校验文件头); 大 JXL 同样会缩放
  - 目标目录若在源目录内, 扫描时会自动跳过它, 重复运行安全
  - 源目录里的符号链接/目录联接(junction)一律跳过, 不处理其内容
    (rglob 会跟进联接, 指向父目录会无限深入, 指向树外会误删树外文件)
  - 上次被强杀留下的 __pipeline_tmp_* 目录会在启动扫描时自动清理(超过 24 小时)
  - 空目录不还原
"""

import argparse
import atexit
import ctypes
import errno
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_SUPPORT = True
except ImportError:
    HEIF_SUPPORT = False

try:
    import pyvips
    # 关闭操作缓存: libvips 默认缓存 100 个加载操作, 会持有源文件句柄,
    # 导致 Windows 下转换完成后无法删除源图 (WinError 32)
    pyvips.cache_set_max(0)
    VIPS_SUPPORT = True
except (ImportError, OSError):
    VIPS_SUPPORT = False

# 本地自有文件的批处理工具, 关闭 Pillow 解压炸弹防护(默认约 8900 万像素超限即拒绝),
# 否则扫描件/全景图会被直接判失败
Image.MAX_IMAGE_PIXELS = None

# ── 常量 ────────────────────────────────────────────────────────────────

ARCHIVE_EXTS = {
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".tbz2",
    ".xz", ".txz", ".lzma", ".tlz", ".cbz", ".cbr",
}

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".gif", ".bmp",
    ".webp", ".tif", ".tiff", ".ico", ".heic", ".heif", ".avif", ".jxl",
    ".ppm", ".pnm", ".pfm", ".pam", ".pgx",
}

# 已经压缩得很充分的格式: 小图不转 JXL, 直接搬运
HIGH_COMPRESSION_EXTS = {".webp", ".jxl", ".heic", ".heif", ".avif"}

# cjxl 能直接无损重编码的格式(小图省掉 PNG 中转)
JPEG_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif"}

# 只有这些格式需要真打开看帧数; 多页 TIFF / 多尺寸 ICO 不是动图
ANIMATABLE_EXTS = {".gif", ".png", ".apng", ".webp", ".avif", ".jxl"}

# 生成子目录名时要剥掉的压缩后缀(含 a.tar.tgz 这类叠加后缀)
_STRIP_SUFFIXES = {
    ".tar", ".gz", ".bz2", ".xz", ".lzma", ".zst",
    ".tgz", ".tbz", ".tbz2", ".txz", ".tlz",
}

STAGING_PREFIX = "__pipeline_tmp_"
BROKEN_DIR_NAME = "_broken_images"

BYTES_PER_MB = 1024 * 1024
TIMEOUT_CAP = 21600          # 7z 超时上限 6 小时
_TOOL_TIMEOUT = 600          # cjxl 单次调用超时
_7Z_BASE_TIMEOUT = 600
MAX_NESTED_DEPTH = 3         # tar.gz 这类双层包最多再展开几层

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

# Pillow 不一定带 JXL 解码器, 尺寸/解码分别用 jxlinfo、djxl 兜底
_HAS_JXLINFO = shutil.which("jxlinfo") is not None
_HAS_DJXL = shutil.which("djxl") is not None

# 网络共享(SMB)偶发不可用的报错特征, 命中则重试
_NETWORK_ERROR_HINTS = (
    "无法访问", "网络位置", "网络路径", "找不到网络", "网络名",
    "path not found", "cannot find the path", "network path", "network name",
    "is not available", "unavailable",
)


# ── 平台 ────────────────────────────────────────────────────────────────

def prevent_sleep() -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)


def restore_sleep() -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)


def screen_resolution():
    """主屏物理分辨率(启用 DPI 感知以获取真实像素值)。"""
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except OSError:
            ctypes.windll.user32.SetProcessDPIAware()
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    import tkinter
    tk = tkinter.Tk()
    tk.withdraw()
    try:
        return tk.winfo_screenwidth(), tk.winfo_screenheight()
    finally:
        tk.destroy()


# ── 7z ─────────────────────────────────────────────────────────────────

def _decode_7z(data: bytes) -> str:
    """文件名是 UTF-8, 系统错误信息在中文 Windows 上是 GBK。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gb18030", errors="replace")


def _is_encrypted_error(err_msg: str) -> bool:
    msg = err_msg.lower()
    return any(k in msg for k in (
        "wrong password", "cannot open encrypted", "can not open encrypted",
        "encrypted archive", "enter password",
    ))


def _short_error(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


def _run_7z(cmd, timeout):
    """执行 7z; 网络共享瞬时抽断时重试(最多 3 次)。

    统一附加 -p(空密码)并把 stdin 指向 DEVNULL: 加密包会立刻以
    "Wrong password" 失败, 而不是停在密码提示上等人输入。
    """
    cmd = [*cmd, "-p"]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)
    for attempt in range(2):
        if proc.returncode == 0:
            return proc
        msg = _decode_7z(proc.stderr).lower()
        if not any(h in msg for h in _NETWORK_ERROR_HINTS):
            return proc
        time.sleep(5 * (attempt + 1))
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    return proc


def _timeout_for(path: Path, base: float, sec_per_mb: float = 0.0,
                 entries: int = 0, cap: int = TIMEOUT_CAP) -> int:
    try:
        size_mb = path.stat().st_size / BYTES_PER_MB
    except OSError:
        size_mb = 0
    return int(min(cap, max(base, size_mb * sec_per_mb, entries * 0.02)))


def _parse_slt(text: str):
    """解析 `7z l -slt`, 返回 (条目路径, 是否含加密条目)。

    第一个块是压缩包自身的头信息(含 Physical Size, 其余条目没有), 跳过。
    """
    entries = []
    encrypted = False
    block = {}

    def flush(b):
        if b.get("Path") and b.get("Folder") != "+" and "Physical Size" not in b:
            entries.append(b["Path"])
            return b.get("Encrypted") == "+"
        return False

    for line in text.splitlines():
        if not line.strip():
            if block and "Path" in block:
                encrypted = flush(block) or encrypted
                block = {}
            continue
        if " = " in line:
            k, v = line.split(" = ", 1)
            block[k] = v
    if block and "Path" in block:
        encrypted = flush(block) or encrypted
    return entries, encrypted


def archive_label(path: Path) -> str:
    """a.tar.gz / a.tgz / a.tar.bz2 -> a (循环剥离叠加后缀)。"""
    name = path.stem
    while True:
        p = Path(name)
        if p.suffix.lower() in _STRIP_SUFFIXES:
            name = p.stem
        else:
            return name


# ── 文件系统 ────────────────────────────────────────────────────────────

def unique_path(dest: Path) -> Path:
    """目标已存在时追加 _1、_2..., 绝不覆盖。"""
    if not os.path.lexists(dest):
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(1, 10000):
        candidate = dest.with_name(f"{stem}_{i}{suffix}")
        if not os.path.lexists(candidate):
            return candidate
    raise OSError(f"无法为 {dest.name} 生成唯一文件名")


def _is_cross_device(exc: OSError) -> bool:
    """跨卷 rename: POSIX 是 EXDEV, Windows 是 ERROR_NOT_SAME_DEVICE(17)。"""
    return (getattr(exc, "errno", None) == errno.EXDEV
            or getattr(exc, "winerror", None) == 17)


def move_to(src: Path, target_root: Path, rel: Path) -> Path:
    """把 src 搬到 target_root/rel, 保留相对结构, 重名自动加序号。

    同卷走 os.replace(原子); 跨卷先拷到同目录的 .part 再原子改名,
    避免中断后目标目录里留下半截文件。
    """
    dest = target_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = unique_path(dest)
    try:
        os.replace(src, dest)
        return dest
    except OSError as e:
        if not _is_cross_device(e):
            raise
    part = dest.with_name(f"{dest.name}.part{os.getpid()}")
    try:
        shutil.copy2(src, part)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if not unlink_robust(src):
        raise RuntimeError(f"跨卷搬运完成但源文件删除失败(目标已生成): {src}")
    return dest


def make_temp_dir(parent: Path) -> Path:
    """在 parent 下建唯一临时目录(不用 mkdtemp: 其私有 ACL 在受限环境下挡住子进程)。"""
    for i in range(1000):
        candidate = parent / f"{STAGING_PREFIX}{os.getpid()}_{i}__"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"无法在 {parent} 下创建临时目录")


def _discard_created(paths) -> None:
    """回滚某个压缩包本次产出的文件。

    压缩包保留时这些产物在包里仍有原件; 留着只会在重跑时变成 _1、_2 副本,
    所以失败即清掉, 让重跑从干净状态开始。清理失败只影响整洁, 不影响数据。
    """
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def unlink_robust(path: Path, attempts: int = 5, delay: float = 0.3) -> bool:
    """删除文件, 对只读属性与瞬时占用(OneDrive/缩略图/杀软)重试。"""
    for _ in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            try:
                os.chmod(path, stat.S_IWRITE)
            except OSError:
                pass
            time.sleep(delay)
        except OSError:
            time.sleep(delay)
    return False


def replace_robust(src: Path, dst: Path, attempts: int = 3, delay: float = 0.2) -> None:
    """原子替换目标文件; 目标被占用或只读时重试。"""
    last = None
    for _ in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last = e
            try:
                if dst.exists():
                    os.chmod(dst, stat.S_IWRITE)
            except OSError:
                pass
            time.sleep(delay)
        except OSError as e:
            last = e
            time.sleep(delay)
    raise RuntimeError(f"目标文件被占用, 替换失败: {dst.name} ({last})")


# ── 图片 ────────────────────────────────────────────────────────────────

def probe_image(path: Path, validate: bool = True):
    """返回 (kind, w, h, reason); kind ∈ static / animated / broken / unverified。

    validate=False 只看文件头(预演用); True 会完整解码, 截断/损坏在这时暴露。
    w/h 已按 EXIF 方向换算成显示尺寸。
    JXL 走 jxlinfo 读尺寸(Pillow 通常没有 JXL 解码器), 只校验文件头。
    """
    ext = path.suffix.lower()
    try:
        with Image.open(path) as im:
            frames = getattr(im, "n_frames", 1) if ext in ANIMATABLE_EXTS else 1
            w, h = im.size
            orientation = 1
            try:
                orientation = im.getexif().get(274, 1)
            except (OSError, ValueError, AttributeError):
                pass
            if validate and frames <= 1:
                im.load()
    except Image.UnidentifiedImageError as e:
        if ext == ".jxl":
            info = probe_jxl_info(path)
            if info:
                jw, jh, animated = info
                return ("animated" if animated else "static"), jw, jh, ""
        return "unverified", 0, 0, f"Pillow 无法识别: {e}"
    except MemoryError:
        return "broken", 0, 0, "解码时内存不足"
    except (OSError, ValueError, SyntaxError) as e:
        return "broken", 0, 0, f"{type(e).__name__}: {e}"
    if orientation in (5, 6, 7, 8):
        w, h = h, w
    return ("animated" if frames > 1 else "static"), w, h, ""


def probe_jxl_info(path: Path):
    """用 jxlinfo -v 读 JXL 的尺寸/方向/是否动画(只解析头, 不解码像素)。失败返回 None。

    返回 (w, h, animated)。JXL 支持动画, 必须看 "Have animation" 字段,
    否则动画 JXL 会被当成静态图缩放, 动态内容静默丢失。
    """
    if not _HAS_JXLINFO:
        return None
    try:
        proc = subprocess.run(["jxlinfo", "-v", str(path)],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", "replace")
    m = re.search(r"(\d+)x(\d+)", out)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    o = re.search(r"Orientation:\s*(\d+)", out)
    if o and int(o.group(1)) in (5, 6, 7, 8):
        w, h = h, w
    animated = bool(re.search(r"Have animation:\s*1", out))
    return w, h, animated


def _djxl_to_png(src: Path, dst_png: Path) -> None:
    """用 djxl 把 JXL 解码成 PNG(Pillow 没装 JXL 插件时的解码通路)。"""
    if not _HAS_DJXL:
        raise RuntimeError("需要 djxl 才能解码 JXL, 请安装 libjxl")
    try:
        proc = subprocess.run(["djxl", str(src), str(dst_png)],
                              capture_output=True, timeout=_TOOL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"djxl 解码超时({_TOOL_TIMEOUT} 秒)")
    if proc.returncode != 0:
        raise RuntimeError("djxl 解码失败: "
                           + proc.stderr.decode("utf-8", "replace").strip()[:160])


def fit_size(w: int, h: int, sw: int, sh: int):
    """等比缩放到屏幕内的尺寸。"""
    scale = min(sw / w, sh / h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def _vips_to_png(src: Path, dst_png: Path) -> None:
    im = pyvips.Image.new_from_file(str(src), access="sequential")
    try:
        im = im.autorot()
        if im.interpretation not in ("b-w", "rgb", "srgb"):
            im = im.colourspace("srgb")
        im.write_to_file(str(dst_png))
    finally:
        del im


def _pillow_to_png(src: Path, dst_png: Path) -> None:
    from PIL import ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode == "P":
            im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        elif im.mode == "LA":
            im = im.convert("RGBA")
        elif im.mode not in ("L", "RGB", "RGBA"):
            im = im.convert("RGB")
        im.save(dst_png, format="PNG")


def make_png(src: Path, dst_png: Path) -> None:
    if VIPS_SUPPORT:
        try:
            _vips_to_png(src, dst_png)
            return
        except (pyvips.Error, OSError):
            pass
    if src.suffix.lower() == ".jxl":
        _djxl_to_png(src, dst_png)      # Pillow 没有 JXL 解码器时走 djxl
        return
    _pillow_to_png(src, dst_png)


def _vips_resize(src: Path, dst_png: Path, nw: int, nh: int) -> None:
    im = pyvips.Image.thumbnail(str(src), nw, height=nh,
                                size=pyvips.enums.Size.DOWN, auto_rotate=True)
    try:
        if im.interpretation not in ("b-w", "rgb", "srgb"):
            im = im.colourspace("srgb")
        im.write_to_file(str(dst_png))
    finally:
        del im


def _pillow_resize(src: Path, dst_png: Path, nw: int, nh: int) -> None:
    from PIL import ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode == "P":
            im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        elif im.mode == "LA":
            im = im.convert("RGBA")
        elif im.mode not in ("L", "RGB", "RGBA"):
            im = im.convert("RGB")
        im.resize((nw, nh), Image.Resampling.LANCZOS).save(dst_png, format="PNG")


def make_resized_png(src: Path, dst_png: Path, nw: int, nh: int) -> None:
    if VIPS_SUPPORT:
        try:
            _vips_resize(src, dst_png, nw, nh)
            return
        except (pyvips.Error, OSError):
            pass
    if src.suffix.lower() == ".jxl":
        # Pillow 打不开 JXL: 先 djxl 解码成原尺寸 PNG, 再缩放
        full = dst_png.with_name(dst_png.name + ".full.png")
        try:
            _djxl_to_png(src, full)
            _pillow_resize(full, dst_png, nw, nh)
        finally:
            full.unlink(missing_ok=True)
        return
    _pillow_resize(src, dst_png, nw, nh)


def _cjxl_run(src: Path, out: Path, extra=()) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["cjxl", str(src), str(out), *extra],
                              capture_output=True, timeout=_TOOL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"cjxl 编码超时({_TOOL_TIMEOUT} 秒)")


def _cjxl_error(proc) -> str:
    return proc.stderr.decode("utf-8", "replace").strip()[:160]


def encode_jxl_to(src: Path, dest: Path, nw=None, nh=None) -> None:
    """把 src 编码成 dest(.jxl)。给了 nw/nh 时先解码缩放成 PNG 再编码。

    JPEG 优先让 cjxl 无损重编码; cjxl 不认的 JPEG(CMYK、异常尾部数据)逐级降级:
    无损重编码 -> 按像素无损编码(--lossless_jpeg=0) -> Pillow 解码成 PNG 再编码。
    整个中间过程都在 dest 所在目录的临时目录里完成, 最后一步是原子替换。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = make_temp_dir(dest.parent)
    try:
        out = work / "out.jxl"
        if nw is None and src.suffix.lower() in JPEG_EXTS:
            proc = _cjxl_run(src, out)
            if proc.returncode != 0:
                proc = _cjxl_run(src, out, ["--lossless_jpeg=0"])
            if proc.returncode != 0:
                png = work / "in.png"
                _pillow_to_png(src, png)        # CMYK 等 cjxl 解不了的交给 Pillow
                proc = _cjxl_run(png, out)
            if proc.returncode != 0:
                raise RuntimeError(f"cjxl 编码失败: {_cjxl_error(proc)}")
        else:
            feed = work / "in.png"
            if nw is None:
                make_png(src, feed)
            else:
                make_resized_png(src, feed, nw, nh)
            proc = _cjxl_run(feed, out)
            if proc.returncode != 0:
                raise RuntimeError(f"cjxl 编码失败: {_cjxl_error(proc)}")
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("cjxl 输出为空文件")
        replace_robust(out, dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def emit_image(src: Path, w: int, h: int, target_dir: Path, stem: str,
               ext: str, sw: int, sh: int, stats: dict):
    """把一张静态图片落到 target_dir, 返回 (落地路径, 动作说明)。源文件由调用方删除。"""
    target_dir.mkdir(parents=True, exist_ok=True)

    if w and h and (w > sw or h > sh):
        nw, nh = fit_size(w, h, sw, sh)
        dest = unique_path(target_dir / f"{stem}.jxl")
        encode_jxl_to(src, dest, nw, nh)
        stats["resized"] += 1
        return dest, f"缩放 {w}x{h} -> {nw}x{nh} 存 JXL"

    if ext in HIGH_COMPRESSION_EXTS:
        dest = move_to(src, target_dir, Path(f"{stem}{ext}"))
        stats["moved"] += 1
        return dest, "原样搬运"

    dest = unique_path(target_dir / f"{stem}.jxl")
    encode_jxl_to(src, dest)
    stats["converted"] += 1
    return dest, "转 JXL"


def classify_extracted(tmp: Path):
    """把解压出来的文件分成 (静态图片, 其他, 破损); 每项带相对路径。"""
    images, others, broken = [], [], []
    for path in sorted(p for p in tmp.rglob("*") if p.is_file()):
        rel = path.relative_to(tmp)
        ext = path.suffix.lower()
        if ext not in IMAGE_EXTS:
            others.append((path, rel))
            continue
        kind, w, h, reason = probe_image(path)
        if kind == "animated":
            others.append((path, rel))
        elif kind == "broken":
            broken.append((path, rel, reason))
        elif kind == "unverified":
            if ext in HIGH_COMPRESSION_EXTS:
                images.append((path, rel, 0, 0))   # 解不了但已压好, 直接搬
            else:
                broken.append((path, rel, reason))
        else:
            images.append((path, rel, w, h))
    return images, others, broken


# ── 压缩包 ──────────────────────────────────────────────────────────────

def process_archive(archive_path: Path, source_root: Path, target_root: Path,
                    sw: int, sh: int, dry_run: bool, stats: dict):
    """处理单个压缩包, 返回 (结果代码, 说明)。

    结果代码: deleted / preview / encrypted / no_image / error
    """
    listing = _run_7z(["7z", "l", "-slt", str(archive_path)],
                      timeout=_timeout_for(archive_path, base=120, sec_per_mb=0.5))
    if listing.returncode != 0:
        err = _decode_7z(listing.stderr)
        if _is_encrypted_error(err):
            return "encrypted", "密码保护/加密, 已跳过"
        return "error", f"无法列出压缩包: {_short_error(err)}"

    entries, encrypted = _parse_slt(_decode_7z(listing.stdout))
    if encrypted:
        return "encrypted", "密码保护/加密, 已跳过"

    image_candidates = [n for n in entries if Path(n).suffix.lower() in IMAGE_EXTS]
    nested = [n for n in entries if Path(n).suffix.lower() in ARCHIVE_EXTS]
    # tar.gz 这类双层包: 7z 只把内层 vol1.tar 列为条目, 图片在它里面
    if not image_candidates and not nested:
        return "no_image", "包内没有图片, 已保留"

    if dry_run:
        if not image_candidates:
            return "preview", "预演: 包内是嵌套压缩包(如 tar.gz), 需解压后才能确定"
        others = len(entries) - len(image_candidates)
        return "preview", (f"预演: 图片候选 {len(image_candidates)} 个"
                           f"(含动图, 解压后才知道), 其他 {others} 个")

    rel_parent = archive_path.parent.relative_to(source_root)
    label = archive_label(archive_path)
    img_target = target_root / rel_parent / label
    other_target = archive_path.parent / label
    broken_target = target_root / BROKEN_DIR_NAME / rel_parent / label

    tmp = make_temp_dir(archive_path.parent)
    try:
        proc = _run_7z(
            ["7z", "x", str(archive_path), f"-o{tmp}", "-y"],
            timeout=_timeout_for(archive_path, base=_7Z_BASE_TIMEOUT,
                                 sec_per_mb=2.0, entries=len(entries)))
        if proc.returncode != 0:
            err = _decode_7z(proc.stderr)
            if _is_encrypted_error(err):
                return "encrypted", "密码保护/加密, 已跳过"
            return "error", f"解压失败: {_short_error(err)}"

        images, others, broken = classify_extracted(tmp)

        # 双层包: 外层一张静态图都没有时才继续展开内层, 避免拆开包里附带的 zip
        for _depth in range(MAX_NESTED_DEPTH):
            if images:
                break
            inner = [(p, rel) for p, rel in others
                     if p.suffix.lower() in ARCHIVE_EXTS]
            if not inner:
                break
            for p, _rel in inner:
                # -aou: 内层解到外层目录时若重名, 自动改名而不是静默覆盖
                proc = _run_7z(
                    ["7z", "x", str(p), f"-o{p.parent}", "-y", "-aou"],
                    timeout=_timeout_for(p, base=_7Z_BASE_TIMEOUT, sec_per_mb=2.0))
                if proc.returncode != 0:
                    return "error", (f"展开内层压缩包 {p.name} 失败: "
                                     f"{_short_error(_decode_7z(proc.stderr))}")
                p.unlink()
            images, others, broken = classify_extracted(tmp)

        if not images and not broken:
            return "no_image", "包内没有静态图片(可能全是动图), 已保留压缩包"

        failures = []
        created = []        # 本次为这个包产出的文件, 失败时回滚, 保证重跑不产生 _1 副本

        for path, rel, w, h in images:
            try:
                dest, _action = emit_image(path, w, h, img_target / rel.parent,
                                           rel.stem, rel.suffix.lower(), sw, sh, stats)
                created.append(dest)
                path.unlink(missing_ok=True)
            except (OSError, RuntimeError, ValueError, MemoryError) as e:
                failures.append((rel.as_posix(), str(e)))

        for path, rel, reason in broken:
            try:
                created.append(move_to(path, broken_target, rel))
                stats["broken"] += 1
                print(f"    破损: {rel.as_posix()} ({_short_error(reason, 80)})",
                      file=sys.stderr)
            except OSError as e:
                failures.append((rel.as_posix(), str(e)))

        for path, rel in others:
            try:
                created.append(move_to(path, other_target, rel))
            except OSError as e:
                failures.append((rel.as_posix(), str(e)))

        if failures:
            for name, err in failures[:5]:
                print(f"    失败: {name} ({err})", file=sys.stderr)
            if len(failures) > 5:
                print(f"    ... 另有 {len(failures) - 5} 个失败未显示", file=sys.stderr)
            _discard_created(created)
            return "error", (f"{len(failures)} 个文件处理失败, 压缩包已保留"
                             f"(本次产出已回滚, 修好后重跑不会产生重复文件)")

        try:
            archive_path.unlink()
        except OSError as e:
            _discard_created(created)
            return "error", (f"文件已全部处理, 但压缩包删除失败, 本次产出已回滚"
                             f"(避免重跑产生重复): {e}")

        return "deleted", (f"图片 {len(images)} 个, 其他 {len(others)} 个"
                           + (f", 破损 {len(broken)} 个" if broken else ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 主流程 ──────────────────────────────────────────────────────────────

def _is_link_or_junction(path: Path) -> bool:
    """符号链接或 Windows 目录联接(junction)/挂载点。

    Path.rglob 会跟进 junction(符号链接则不会): 指向父目录的联接会让扫描无限
    深入同一个目录, 指向树外的联接会让脚本把树外的图片搬走并删除 —— 必须剪枝。
    """
    try:
        if path.is_symlink():
            return True
        if os.name == "nt":
            attrs = os.lstat(path).st_file_attributes
            return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return False
    return False


def _drop_stale_temp(path: Path, max_age: float = 24 * 3600) -> None:
    """清理上次被强杀留下的临时目录; 正在运行的实例目录很新, 不会被误删。"""
    try:
        if time.time() - path.stat().st_mtime > max_age:
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def collect_files(source_root: Path, target_root: Path):
    """递归收集待处理的图片与压缩包。

    用 os.walk 而不是 rglob: 必须在进入目录前剪掉链接/联接, rglob 做不到
    (实测 junction 会被反复跟进, 路径层层变长)。同时跳过目标目录(若在源目录内)
    和本脚本的临时目录, 顺手清掉过期的临时目录。
    """
    images, archives = [], []
    target_abs = os.path.normcase(os.path.abspath(target_root))
    skipped_links = []

    for dirpath, dirnames, filenames in os.walk(source_root, topdown=True):
        keep = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if name.startswith(STAGING_PREFIX):
                _drop_stale_temp(Path(full))
                continue
            if _is_link_or_junction(Path(full)):
                skipped_links.append(full)
                continue
            if os.path.normcase(os.path.abspath(full)) == target_abs:
                continue
            keep.append(name)
        dirnames[:] = keep

        for name in filenames:
            p = Path(dirpath) / name
            ext = p.suffix.lower()
            if ext not in ARCHIVE_EXTS and ext not in IMAGE_EXTS:
                continue
            if _is_link_or_junction(p):
                skipped_links.append(str(p))
                continue
            if ext in ARCHIVE_EXTS:
                archives.append(p)
            else:
                images.append(p)

    if skipped_links:
        print(f"提示: 跳过 {len(skipped_links)} 个链接/联接(不处理其内容):",
              file=sys.stderr)
        for s in skipped_links[:5]:
            print(f"  · {s}", file=sys.stderr)
        if len(skipped_links) > 5:
            print(f"  ... 另有 {len(skipped_links) - 5} 个", file=sys.stderr)

    images.sort()
    archives.sort(key=lambda p: (len(p.relative_to(source_root).parts), str(p)))
    return images, archives


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="图片校验/缩放/转 JXL 并搬到目标目录, 压缩包解包后删除, 最终清空源目录")
    parser.add_argument("source", type=Path, help="源目录(递归扫描)")
    parser.add_argument("dest", type=Path, help="目标目录")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出计划, 不解压、不转换、不删除")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    source_root = args.source.resolve()
    dest_root = args.dest.resolve()

    if not source_root.is_dir():
        print(f"错误: 源目录不存在或不是目录: {source_root}", file=sys.stderr)
        return 2
    if dest_root.exists() and not dest_root.is_dir():
        print(f"错误: 目标路径已存在且不是目录: {dest_root}", file=sys.stderr)
        return 2
    if not shutil.which("7z"):
        print("错误: 未在 PATH 中找到 7z, 请先安装 7-Zip。", file=sys.stderr)
        return 2
    if not shutil.which("cjxl"):
        print("错误: 未在 PATH 中找到 cjxl, 请先安装 libjxl。", file=sys.stderr)
        return 2
    if source_root == dest_root:
        print("错误: 目标目录不能与源目录相同。", file=sys.stderr)
        return 2

    sw, sh = screen_resolution()
    # 整个运行期间(含扫描)阻止系统空闲睡眠, 正常结束或 Ctrl+C 时在 finally 里恢复。
    # 该状态是线程级的: 万一进程先退出, 线程结束时系统也会自动清除。
    # atexit 兜住"还没走到 finally 就 return/抛异常"的路径。
    prevent_sleep()
    atexit.register(restore_sleep)
    images, archives = collect_files(source_root, dest_root)

    if not images and not archives:
        print(f"源目录 {source_root} 下没有图片或压缩包。")
        return 0

    if not args.dry_run:
        dest_root.mkdir(parents=True, exist_ok=True)

    print(f"源目录: {source_root}")
    print(f"目标目录: {dest_root}")
    print(f"屏幕分辨率: {sw}x{sh}")
    if args.dry_run:
        print("模式: 预演(不解压、不转换、不删除; 也不做解码校验, 破损图会显示为正常)")
    if not HEIF_SUPPORT:
        print("提示: 未安装 pillow_heif, heic/heif 无法解码校验, 将按原样搬运。")
    if not VIPS_SUPPORT:
        print("提示: 未安装 pyvips, 大图缩放回退 Pillow(内存占用较高)。")
    print(f"待处理: 图片 {len(images)} 个, 压缩包 {len(archives)} 个。\n")

    stats = {"resized": 0, "converted": 0, "moved": 0, "broken": 0,
             "animated": 0, "deleted": 0, "planned": 0, "preview": 0,
             "encrypted": 0, "no_image": 0, "error": 0}
    kept = []
    errors = []

    try:
        # ---- 1. 目录里的散图 ----
        for idx, path in enumerate(images, 1):
            rel = path.relative_to(source_root)
            prefix = f"[图片 {idx}/{len(images)}] {rel}"
            try:
                kind, w, h, reason = probe_image(path, validate=not args.dry_run)
                if kind == "animated":
                    stats["animated"] += 1
                    print(f"{prefix}  动图, 留在源目录")
                    continue

                if kind == "unverified" and path.suffix.lower() in HIGH_COMPRESSION_EXTS:
                    # 没有解码器(如未装 pillow_heif): 校验不了, 但格式本身已压好, 按原样搬运
                    kind, w, h = "static", 0, 0

                if kind in ("broken", "unverified"):
                    if args.dry_run:
                        print(f"{prefix}  预演: 疑似破损或无法识别, 将移到 {BROKEN_DIR_NAME}/")
                        stats["planned"] += 1
                        continue
                    move_to(path, dest_root / BROKEN_DIR_NAME, rel)
                    stats["broken"] += 1
                    print(f"{prefix}  破损, 已移到 {BROKEN_DIR_NAME}/  "
                          f"({_short_error(reason, 80)})")
                    continue

                if args.dry_run:
                    print(f"{prefix}  预演: {describe_plan(w, h, path.suffix.lower(), sw, sh)}")
                    stats["planned"] += 1
                    continue

                _dest, action = emit_image(path, w, h, dest_root / rel.parent,
                                            rel.stem, path.suffix.lower(), sw, sh, stats)
                if not unlink_robust(path):
                    print(f"{prefix}  警告: 源文件被占用, 删除失败, 请手动删除",
                          file=sys.stderr)
                print(f"{prefix}  {action}")
            except (OSError, RuntimeError, ValueError, MemoryError) as e:
                errors.append((rel.as_posix(), str(e)))
                print(f"{prefix}  失败: {e}", file=sys.stderr)
            except KeyboardInterrupt:
                raise

        # ---- 2. 压缩包 ----
        for idx, path in enumerate(archives, 1):
            rel = path.relative_to(source_root)
            print(f"[包 {idx}/{len(archives)}] {rel}")
            try:
                code, detail = process_archive(path, source_root, dest_root,
                                               sw, sh, args.dry_run, stats)
            except subprocess.TimeoutExpired:
                code, detail = "error", "7z 执行超时"
            except KeyboardInterrupt:
                raise
            except Exception as e:
                code, detail = "error", f"{type(e).__name__}: {e}"
            print(f"        {detail}")
            stats[code] += 1
            if code not in ("deleted", "preview"):
                kept.append((rel, detail))
    except KeyboardInterrupt:
        print("\n用户中断: 已处理的文件不受影响, 未处理的保持原样。", file=sys.stderr)
        return 130
    finally:
        restore_sleep()

    # ---- 汇总 ----
    print("\n" + "=" * 60)
    if args.dry_run:
        print(f"预演结束: 将处理图片 {stats['planned']} 个, 压缩包 "
              f"{stats['preview']} 个可处理 / {stats['encrypted']} 个加密 / "
              f"{stats['no_image']} 个无图片; 动图 {stats['animated']} 个留在源目录。")
        print("未做任何修改。去掉 --dry-run 实际执行。")
    else:
        print(f"完成: 缩放 {stats['resized']} 张, 转 JXL {stats['converted']} 张, "
              f"原样搬运 {stats['moved']} 张, 破损 {stats['broken']} 张, "
              f"动图 {stats['animated']} 张留在源目录。")
        print(f"压缩包: 删除 {stats['deleted']} 个, 保留 {stats['encrypted'] + stats['no_image']} 个, "
              f"失败 {stats['error']} 个。")
        print(f"目标目录: {dest_root}")

    if kept:
        print("\n以下压缩包被保留:")
        for rel, reason in kept:
            print(f"  · {rel}  ({reason})")

    if errors:
        print("\n处理失败的文件(源文件已保留):", file=sys.stderr)
        for rel, reason in errors[:20]:
            print(f"  · {rel}  ({reason})", file=sys.stderr)
        if len(errors) > 20:
            print(f"  ... 另有 {len(errors) - 20} 个", file=sys.stderr)

    total_errors = stats["error"] + len(errors)
    return 1 if total_errors else 0


def describe_plan(w: int, h: int, ext: str, sw: int, sh: int) -> str:
    if w and h and (w > sw or h > sh):
        nw, nh = fit_size(w, h, sw, sh)
        return f"缩放 {w}x{h} -> {nw}x{nh} 存 JXL"
    if ext in HIGH_COMPRESSION_EXTS:
        return "高压缩率小图, 原样搬运"
    return "小图, 原分辨率转 JXL"


if __name__ == "__main__":
    sys.exit(main())
