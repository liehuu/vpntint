#!/usr/bin/env python3
"""一把梭校验 VpnTint 的 deb：包结构 + 无脚本 + 双切片 + hook 目标字符串。

为什么单独写一个：
  verify_deb.py 把 dylib 导出到 /tmp，check_arch.py 再从 /tmp 读 ——
  在 Git Bash（Windows）下 `/tmp` 与 Python 看到的路径不一致，两者对不上。
  这里全程用 tempfile 建目录并把路径显式传给两边，不依赖任何隐式约定。

结构：
  parse_deb() 只负责取字节，全部断言集中在 CHECKS 表里，对 ctx（解析结果）操作。
  这样 --selftest 可以拿真实 deb 的 ctx 逐项投毒，验证「每条断言真的会失败」——
  否则「全部通过」只是一排永远为真的装饰。

用法:
  verify_release.py <deb> [<deb> ...]          校验
  verify_release.py --selftest <deb>           反向自检（投毒必须被抓到）
  verify_release.py --version 1.0.3 <deb>      指定期望版本（默认 1.0.3）
"""
import hashlib
import io
import lzma
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile

CPU_ARM64 = 0x0100000C
# 期望版本从仓库根的 control 读（单一真源），读不到才退回这个字面量。
DEFAULT_VERSION = "1.0.3"


def version_from_control():
    """读 ../control 或 ./control 里的 Version 字段，读不到返回 None。"""
    for cand in ("control", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "control")):
        if os.path.isfile(cand):
            for line in open(cand, encoding="utf-8", errors="replace"):
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip()
    return None

# 包内必须出现的字符串（hook 目标 + 关键选择器）。
# 1.0.0/1.0.1 的失效根因就是 hook 了 iOS 16 的 _UIStatusBarItemView，
# 所以这里把 iOS 17 的目标钉死：源码改对了但产物没跟着变，也能抓出来。
MUST_HAVE = [
    b"STUIStatusBar",
    b"STUIStatusBarSignalView",
    b"setActiveColor:",
    b"setInactiveColor:",
    b"vpnEntry",
    b"StatusStatusUI",
    b"_updateWithAggregatedData:",
]
MUST_NOT_HAVE = [
    b"_UIStatusBarItemView",     # 1.0.1 的错误入口，iOS 17 上不会实例化
]


# ---------------------------------------------------------------- 解析

def ar_members(data):
    if not data.startswith(b"!<arch>\n"):
        raise SystemExit("不是 ar 归档")
    off, out = 8, []
    while off + 60 <= len(data):
        hdr = data[off:off + 60]
        name = hdr[0:16].decode().strip()
        size = int(hdr[48:58].decode().strip())
        out.append((name, data[off + 60:off + 60 + size]))
        off += 60 + size + (size & 1)
    return out


def _zstd_decompress(body):
    """解压 data.tar.zst：内置 → 第三方库 → zstd 子进程。

    每一步的失败原因都要留住。早先的版本用 `except Exception: pass` 把真实原因吞了，
    结果「zstandard 装了但解压失败」被报成「没装 zstd」，把环境问题误判成包装坏了。
    另外 dpkg 生成的 zstd 帧通常不带 content size，
    ZstdDecompressor().decompress() 会抛「could not determine content size in frame header」，
    必须用 stream_reader 流式读——这是 CI 上 3.12/3.13 唯一能走通的分支。
    """
    why = []

    # CPython 3.14+ 内置 compression.zstd
    try:
        from compression import zstd          # noqa: PLC0415
        out = zstd.decompress(body)
        print("   data.tar.zst 解压路径: 内置 compression.zstd")
        return out
    except Exception as e:
        why.append("compression.zstd: %r" % (e,))

    try:
        import zstandard                       # noqa: PLC0415
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)) as r:
            out = r.read()
        print("   data.tar.zst 解压路径: zstandard 模块")
        return out
    except Exception as e:
        why.append("zstandard: %r" % (e,))

    exe = shutil.which("zstd") or shutil.which("zstd.exe")
    if exe:
        p = subprocess.run([exe, "-dc"], input=body, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        if p.returncode == 0:
            # 打印实际用的可执行文件：CI 上走的就是这条（apt 装的 /usr/bin/zstd）。
            # 只有日志里出现这行，才能证明兜底分支真被执行过，而不是被前面的分支顶掉。
            print("   data.tar.zst 解压路径: zstd 子进程 %s" % exe)
            return p.stdout
        why.append("zstd -dc(%s): %s" % (exe, p.stderr.decode(errors="replace")[:200]))
    else:
        why.append("PATH 里没有 zstd")

    raise SystemExit("解压 data.tar.zst 失败，三条路都不通：\n  " + "\n  ".join(why))


def open_tar(body):
    if body[:6] == b"\xfd7zXZ\x00":
        return tarfile.open(fileobj=io.BytesIO(lzma.decompress(body)), mode="r:")
    if body[:2] == b"\x1f\x8b":
        return tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
    if body[:1] == b"\x5d":
        return tarfile.open(fileobj=io.BytesIO(lzma.decompress(body, format=lzma.FORMAT_ALONE)), mode="r:")
    if body[:4] == b"\x28\xb5\x2f\xfd":
        return tarfile.open(fileobj=io.BytesIO(_zstd_decompress(body)), mode="r:")
    return tarfile.open(fileobj=io.BytesIO(body), mode="r:")


def macho_slices(data):
    magic = struct.unpack(">I", data[:4])[0]
    out = []
    if magic in (0xCAFEBABE, 0xCAFEBABF):
        n = struct.unpack(">I", data[4:8])[0]
        off = 8
        for _ in range(n):
            if magic == 0xCAFEBABE:
                cpu, sub, _o, _sz, _al = struct.unpack(">iiIII", data[off:off + 20])
                off += 20
            else:
                cpu, sub, _o, _sz, _al, _r = struct.unpack(">iiQQII", data[off:off + 32])
                off += 32
            out.append((cpu, sub))
    elif struct.unpack("<I", data[:4])[0] == 0xFEEDFACF:
        cpu, sub = struct.unpack("<ii", data[4:12])
        out.append((cpu, sub))
    else:
        raise SystemExit("不是 Mach-O: magic=%08x" % magic)
    return out


def norm(n):
    while n.startswith("./"):
        n = n[2:]
    return n.lstrip("/")


def parse_deb(path):
    raw = open(path, "rb").read()
    members = ar_members(raw)
    names = [n for n, _ in members]

    ctl_name = [n for n in names if n.startswith("control.tar")]
    dat_name = [n for n in names if n.startswith("data.tar")]
    ctx = {
        "path": path,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "members": names,
        "first_member": names[0] if names else "",
        "fields": {},
        "scripts": [],
        "files": [],          # [(path, mode)]
        "dylib": b"",
        "dylib_path": "",
        "plist_path": "",
        "plist": None,
        "plist_error": "",
        "slices": [],
    }
    if not ctl_name or not dat_name:
        return ctx

    ctl = open_tar([b for n, b in members if n.startswith("control.tar")][0])
    cnames = ctl.getnames()
    cf = ctl.extractfile("./control" if "./control" in cnames else "control").read().decode()
    for line in cf.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            ctx["fields"][k.strip()] = v.strip()
    ctx["scripts"] = sorted(n.lstrip("./") for n in cnames
                            if n.lstrip("./") not in ("", "control", "md5sums",
                                                      "conffiles", "triggers", "shlibs"))

    dat = open_tar([b for n, b in members if n.startswith("data.tar")][0])
    ctx["files"] = [(norm(m.name), m.mode) for m in dat.getmembers() if m.isfile()]
    dylibs = [n for n, _ in ctx["files"] if n.endswith(".dylib")]
    plists = [n for n, _ in ctx["files"] if n.endswith(".plist")]
    if len(dylibs) == 1:
        ctx["dylib_path"] = dylibs[0]
        real = [m.name for m in dat.getmembers() if normalize_member(m.name) == dylibs[0]][0]
        ctx["dylib"] = dat.extractfile(real).read()
        try:
            ctx["slices"] = macho_slices(ctx["dylib"])
        except SystemExit:
            ctx["slices"] = []
    if len(plists) == 1:
        ctx["plist_path"] = plists[0]
        real = [m.name for m in dat.getmembers() if normalize_member(m.name) == plists[0]][0]
        try:
            ctx["plist"] = plistlib.loads(dat.extractfile(real).read())
        except Exception as e:
            ctx["plist_error"] = repr(e)
    return ctx


def normalize_member(n):
    return norm(n)


# ---------------------------------------------------------------- 断言表

def _c_first_member(ctx):
    return (ctx["first_member"].startswith("debian-binary"),
            "首成员是 debian-binary", "首成员不是 debian-binary: %r" % ctx["first_member"])


def _c_version(ctx):
    want = ctx.get("_want_version", DEFAULT_VERSION)
    got = ctx["fields"].get("Version", "")
    return (got == want, "Version = %s" % want,
            "Version 应为 %s，实际 %r" % (want, got))


def _c_no_scripts(ctx):
    return (not ctx["scripts"], "无任何 maintainer script",
            "包内有 maintainer script %s（会打断 dpkg configure）" % ctx["scripts"])


def _c_file_count(ctx):
    n_dylib = len([n for n, _ in ctx["files"] if n.endswith(".dylib")])
    n_plist = len([n for n, _ in ctx["files"] if n.endswith(".plist")])
    return (n_dylib == 1 and n_plist == 1, "dylib 与 plist 各 1 个",
            "dylib/plist 数量应为各 1，实际 %d/%d" % (n_dylib, n_plist))


def _c_path_prefix(ctx):
    arch = ctx["fields"].get("Architecture", "")
    want_jb = (arch == "iphoneos-arm64")
    bad = []
    for n in (ctx["dylib_path"], ctx["plist_path"]):
        if not n:
            bad.append("缺 dylib 或 plist")
            continue
        has_jb = n.startswith("var/jb/")
        if want_jb and not has_jb:
            bad.append("%s 应位于 var/jb 下（rootless）" % n)
        if (not want_jb) and has_jb:
            bad.append("%s 不应带 /var/jb 前缀（roothide 是纯 Library）" % n)
    return (not bad,
            "路径前缀与 Architecture=%s 匹配" % arch,
            "; ".join(bad))


def _c_dylib_exec(ctx):
    m = [mm for n, mm in ctx["files"] if n == ctx["dylib_path"]]
    if not m:
        return (False, "", "找不到 dylib 条目")
    # 0755 是需要但不唯一的条件：dpkg 不看 exec 位，但 MobileSubstrate 要读得到
    return (m[0] & 0o111 != 0, "dylib 可执行位 0o%o" % m[0],
            "dylib 权限 0o%o 缺可执行位" % m[0])


def _c_arm64(ctx):
    got = {(c, s & 0x00FFFFFF) for c, s in ctx["slices"]}
    return ((CPU_ARM64, 0) in got, "存在 arm64 切片", "缺 arm64 切片（现有 %r）" % (ctx["slices"],))


def _c_arm64e(ctx):
    got = {(c, s & 0x00FFFFFF) for c, s in ctx["slices"]}
    return ((CPU_ARM64, 2) in got, "存在 arm64e 切片", "缺 arm64e 切片（现有 %r）" % (ctx["slices"],))


def _c_must_have(ctx):
    miss = [s.decode() for s in MUST_HAVE if s not in ctx["dylib"]]
    return (not miss, "hook 目标字符串齐全（%d 项）" % len(MUST_HAVE),
            "产物里缺 %s —— hook 目标没编进去" % miss)


def _c_must_not_have(ctx):
    hit = [s.decode() for s in MUST_NOT_HAVE if s in ctx["dylib"]]
    return (not hit, "不含旧版错误目标 %s" % [s.decode() for s in MUST_NOT_HAVE],
            "仍含 %s（1.0.1 的错误 hook 目标）" % hit)


def _c_filter_plist(ctx):
    if ctx["plist_error"]:
        return (False, "", "filter plist 解析失败: %s" % ctx["plist_error"])
    p = ctx.get("plist")
    if not p:
        return (False, "", "找不到 filter plist")
    bundles = (p.get("Filter") or {}).get("Bundles") or []
    if "com.apple.springboard" not in bundles:
        return (False, "", "filter plist 没挂 com.apple.springboard，插件不会被注入: %r" % (bundles,))
    return (True, "filter plist 挂到 com.apple.springboard", "")


CHECKS = [
    ("first-member", _c_first_member),
    ("version", _c_version),
    ("no-scripts", _c_no_scripts),
    ("file-count", _c_file_count),
    ("path-prefix", _c_path_prefix),
    ("dylib-exec", _c_dylib_exec),
    ("filter-plist", _c_filter_plist),
    ("arm64", _c_arm64),
    ("arm64e", _c_arm64e),
    ("must-have", _c_must_have),
    ("must-not-have", _c_must_not_have),
]


def run_checks(ctx, verbose=True):
    """跑全部断言，返回失败列表 [(check_id, msg)]。"""
    fails = []
    for cid, fn in CHECKS:
        try:
            good, okmsg, badmsg = fn(ctx)
        except Exception as e:                       # 断言自己炸了也算失败，不能算过
            fails.append((cid, "断言异常: %r" % (e,)))
            if verbose:
                print("  FAIL", cid, "断言异常:", repr(e))
            continue
        if good:
            if verbose:
                print("  OK  ", okmsg)
        else:
            fails.append((cid, badmsg))
            if verbose:
                print("  FAIL", badmsg)
    return fails


# ---------------------------------------------------------------- 反向自检

# (说明, 期望被抓到的 check_id, 投毒函数)
def _poison_version(c):
    c["fields"]["Version"] = "1.0.1"


def _poison_postinst(c):
    c["scripts"] = ["postinst"]


def _poison_drop_arm64e(c):
    c["slices"] = [(s[0], s[1]) for s in c["slices"] if (s[1] & 0x00FFFFFF) != 2]


def _poison_rename_stui(c):
    c["dylib"] = c["dylib"].replace(b"STUIStatusBar", b"XTUIStatusBar")


def _poison_old_target(c):
    c["dylib"] = c["dylib"] + b"\x00_UIStatusBarItemView\x00"


def _poison_jb_prefix(c):
    # 把 roothide 的文件挪到 var/jb 下（或反之），路径前缀断言必须报
    arch = c["fields"].get("Architecture", "")
    if arch == "iphoneos-arm64":
        c["dylib_path"] = c["dylib_path"].replace("var/jb/", "")
    else:
        c["dylib_path"] = "var/jb/" + c["dylib_path"]


def _poison_noexec(c):
    c["files"] = [(n, (m & ~0o111) if n == c["dylib_path"] else m) for n, m in c["files"]]


def _poison_first_member(c):
    c["first_member"] = "control.tar.xz"


def _poison_filter_plist(c):
    if c.get("plist"):
        c["plist"] = {"Filter": {"Bundles": ["com.apple.SafariViewService"]}}


POISONS = [
    ("版本号被改回 1.0.1", "version", _poison_version),
    ("包里混进 postinst", "no-scripts", _poison_postinst),
    ("砍掉 arm64e 切片", "arm64e", _poison_drop_arm64e),
    ("STUIStatusBar 改名（hook 目标丢失）", "must-have", _poison_rename_stui),
    ("塞回 _UIStatusBarItemView", "must-not-have", _poison_old_target),
    ("文件位置与 Architecture 不符", "path-prefix", _poison_jb_prefix),
    ("dylib 掉可执行位", "dylib-exec", _poison_noexec),
    ("ar 首成员不是 debian-binary", "first-member", _poison_first_member),
    ("filter plist 挂错 bundle", "filter-plist", _poison_filter_plist),
]


def selftest(base_ctx):
    import copy
    print("=== 反向自检：每条断言都必须能失败 ===")
    # 先确认基线是干净的，否则「投毒被抓到」可能只是本来就有别的失败
    base_fails = run_checks(base_ctx, verbose=False)
    if base_fails:
        print("FAIL: 基线本身就有失败项，反向自检无意义：")
        for cid, m in base_fails:
            print("   ·", cid, m)
        return 1

    dead = []
    for desc, want_id, fn in POISONS:
        c = copy.deepcopy({k: v for k, v in base_ctx.items()})
        fn(c)
        fails = run_checks(c, verbose=False)
        ids = [cid for cid, _ in fails]
        if want_id in ids:
            print("  OK   %-34s -> %s 报错" % (desc, want_id))
        else:
            print("  FAIL %-34s -> 没有被抓到（实际失败项 %s）" % (desc, ids))
            dead.append(desc)

    # 另一类死断言：无论怎么投毒都不失败。这里抽查「改一个字就必挂」的反向：
    # 把 must-have 全删，确保断言不是只看其中一项
    c = copy.deepcopy(base_ctx)
    c["dylib"] = b""
    fails = run_checks(c, verbose=False)
    if "must-have" in [cid for cid, _ in fails]:
        print("  OK   %-34s -> must-have 报错" % "清空 dylib")
    else:
        print("  FAIL %-34s -> 没有被抓到" % "清空 dylib")
        dead.append("清空 dylib")

    if dead:
        print("FAIL: %d 条断言语义失效，自检不通过" % len(dead))
        return 1
    print("OK：反向自检全部有效（%d 条投毒全部被拦）" % (len(POISONS) + 1))
    return 0


# ---------------------------------------------------------------- 主流程

def check(path, want_ver=None, verbose=True):
    ctx = parse_deb(path)
    ctx["_want_version"] = want_ver or version_from_control() or DEFAULT_VERSION
    if verbose:
        print("=" * 70)
        print("文件:", os.path.basename(path), ctx["size"], "bytes")
        print("sha256:", ctx["sha256"])
        print("ar 成员:", ctx["members"])
        print("  Architecture =", ctx["fields"].get("Architecture", "?"),
              "| Version =", ctx["fields"].get("Version", "?"))
        for n, m in ctx["files"]:
            print("   0o%o %s" % (m, n))
        for cpu, sub in ctx["slices"]:
            print("   cputype=0x%08x cpusubtype=0x%08x（有效 0x%08x）"
                  % (cpu & 0xFFFFFFFF, sub & 0xFFFFFFFF, sub & 0x00FFFFFF))
    return ctx, run_checks(ctx, verbose=verbose)


def main(argv):
    want_ver = version_from_control() or DEFAULT_VERSION
    ver_explicit = False
    do_selftest = "--selftest" in argv
    paths = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--selftest":
            i += 1
        elif a == "--version":
            if i + 1 >= len(argv):
                print("--version 后面要跟版本号")
                return 2
            want_ver = argv[i + 1]
            ver_explicit = True
            i += 2
        elif a.startswith("--version="):
            want_ver = a.split("=", 1)[1]
            ver_explicit = True
            i += 1
        elif a.startswith("--"):
            print("未知参数:", a)
            return 2
        else:
            paths.append(a)
            i += 1

    if not paths:
        print(__doc__)
        return 2

    print("期望版本: %s（%s）" % (want_ver, "命令行指定" if ver_explicit else "取自 control"))

    allf = []
    baseline = None
    for p in paths:
        ctx, fails = check(p, want_ver=want_ver)
        allf += [(p, cid, m) for cid, m in fails]
        if baseline is None:
            baseline = ctx

    if do_selftest:
        print()
        rc = selftest(baseline)
        if allf:
            print("\n注：正式校验另有 %d 项失败，见上。" % len(allf))
            return 1
        return rc

    print()
    if allf:
        print("FAIL: %d 项" % len(allf))
        for p, cid, m in allf:
            print("   ·", os.path.basename(p), "[%s]" % cid, m)
        return 1
    print("OK: 全部通过（%d 个包 × %d 项断言）" % (len(paths), len(CHECKS)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))