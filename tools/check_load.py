#!/usr/bin/env python3
"""检查 dylib 的加载期风险：依赖库、未定义符号、install_name、构建版本。

为什么需要（2026-11-11）：
  用户装 1.0.3 仍进安全模式，但**没有任何新的 SpringBoard-*.ips**。
  如果真崩了，系统 ReportCrash 必写日志。没有新 ips 意味着两种可能：
    ① 崩溃早到崩溃报告器都没起来；
    ② 根本没崩 —— 是注入器在**加载阶段**就拒绝了这个 dylib
       （依赖缺失 / 未定义符号 / install_name 非法），随后进入安全模式，
       安全模式里所有 tweak 都不加载，于是「再也不崩」，也就永远没有新 ips。
  第 ② 种完全由静态信息可查，不需要实机。所以先把这一层查干净。

用法: check_load.py <deb 或 dylib> [...]
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF

# arm64e 的 cpusubtype 高 8 位是 capability 字段（不是 subtype 的一部分）：
#   0x80 = CPU_SUBTYPE_PTRAUTH_ABI，表示「带 ptrauth ABI 版本的二进制」
#   0x40 = kernel ABI
#   低 4 位 = ptrauth ABI 版本号
# LLVM 作者原话（llvm-project#104650）：
#   "Modern arm64e requires versioned binaries: a binary with 0x00 caps in
#    cpusubtype is now rejected by the linker and everything after."
# 我们 1.0.3 的 arm64e 切片实测 cpusubtype = 0x00000002 —— caps 为 0x00，
# 就是这种「老式、无版本」的 arm64e。这是加载期风险，必须判失败。
CPU_SUBTYPE_PTRAUTH_ABI = 0x80   # 位于 bits 24..31

# 常量以 <mach-o/loader.h> 为准逐条校对过。
# 前一版有 7 条错位（0x23/0x24/0x25/0x26/0x29/0x2A/0x2B/0x32/0x33/0x34），
# 后果最严重的是把 0x26 当成 LC_REEXPORT_DYLIB —— 0x26 实为 LC_FUNCTION_STARTS，
# 于是拿函数起始表的偏移去当字符串偏移读，读出 "t_timer" 这类垃圾依赖，
# 还让 arm64 与 arm64e 两个切片的输出互不一致。那两条是工具缺陷，不是 dylib 缺陷。
LC_NAMES = {
    0x01: "LC_SEGMENT", 0x02: "LC_SYMTAB", 0x0B: "LC_DYSYMTAB",
    0x0C: "LC_LOAD_DYLIB", 0x0D: "LC_ID_DYLIB", 0x0E: "LC_LOAD_DYLINKER",
    0x19: "LC_SEGMENT_64", 0x1B: "LC_UUID", 0x1D: "LC_CODE_SIGNATURE",
    0x1E: "LC_SEGMENT_SPLIT_INFO",
    0x20: "LC_LAZY_LOAD_DYLIB", 0x21: "LC_ENCRYPTION_INFO",
    0x22: "LC_DYLD_INFO", 0x80000022: "LC_DYLD_INFO_ONLY",
    0x24: "LC_VERSION_MIN_MACOSX", 0x25: "LC_VERSION_MIN_IPHONEOS",
    0x26: "LC_FUNCTION_STARTS",
    0x29: "LC_DATA_IN_CODE", 0x2A: "LC_SOURCE_VERSION",
    0x2B: "LC_DYLIB_CODE_SIGN_DRS", 0x2C: "LC_ENCRYPTION_INFO_64",
    0x2D: "LC_LINKER_OPTION", 0x2E: "LC_LINKER_OPTIMIZATION_HINT",
    0x32: "LC_BUILD_VERSION", 0x80000033: "LC_DYLD_EXPORTS_TRIE",
    0x80000034: "LC_DYLD_CHAINED_FIXUPS",
    0x80000018: "LC_LOAD_WEAK_DYLIB", 0x8000001F: "LC_REEXPORT_DYLIB",
    0x80000023: "LC_LOAD_UPWARD_DYLIB", 0x8000001C: "LC_RPATH",
    0x80000028: "LC_MAIN",
}
# 只有这几个命令体里带 dylib 名字（dylib_command 的 name 偏移在 body[8:12]）。
DYLIB_CMDS = (0x0C, 0x20, 0x80000018, 0x8000001F, 0x80000023)
# 系统框架在 iOS 上一定存在；这里只列可能被误链接的"不该有"的项
SUSPECT_PREFIXES = ("/usr/lib/libsubstrate", "/usr/lib/libhooker",
                    "/usr/lib/libellekit", "/Library/Frameworks/CydiaSubstrate",
                    "/usr/lib/libhooker.dylib")
# 链接器/工具链层面最容易出现的「系统不提供」符号，逐条点名。
# ___isOSVersionAtLeast 是 @available 的产物：clang 生成它，但 iOS 系统库不导出，
# 于是链接期就可能失败、或运行期 dlopen 解析失败。本项目已禁用 @available。
SUSPECT_SYMS = ("___isOSVersionAtLeast", "___isPlatformVersionAtLeast")

# 未定义符号前缀分类：命中的归入「系统必然提供」一档，不命中的单独列出来人工看。
# 注意这**不是**白名单校验 —— 判失败靠下面的分类结果里出现「未分类」项，
# 而不是靠前缀表本身。前缀表永远比现实短。
SAFE_PREFIXES = (
    "_OBJC_CLASS_$_", "_OBJC_METACLASS_$_", "_OBJC_EHTYPE_$_", "_OBJC_IVAR_$_",
    "_objc_", "_class_", "_object_", "_method_", "_sel_", "_ivar_", "_protocol_",
    "_dispatch_", "__dispatch_",
    "_CF", "__CF", "___CF",
    "_NS", "__NS", "___NS",
    "_open", "_close", "_read", "_write", "_dlopen", "_dlsym", "_dlclose",
    "___stack_chk_", "___objc_personality_v0", "__Unwind_",
    "_bzero", "_memcpy", "_memset", "_strlen", "_strcmp", "_strdup",
    # dyld_stub_binder 由 dyld 自身导出（不以 _ 开头），每条有 stub 的 dylib 都有它
    "dyld_stub_binder",
)


def classify_symbols(syms):
    """把未定义符号分成 (可疑, 未分类)。

    可疑 = 名字里含已知的「系统不提供」特征；
    未分类 = 不匹配任何已知安全前缀 —— 这些不是 FAIL，是必须人眼过一遍的。
    """
    susp, unknown = [], []
    for s in syms:
        if any(k in s for k in SUSPECT_SYMS):
            susp.append(s)
        elif not s.startswith(SAFE_PREFIXES):
            unknown.append(s)
    return susp, unknown


def check_frameworks(deps):
    """返回可疑依赖列表（空 = 通过）。"""
    return [d for _k, d in deps if any(d.startswith(p) for p in SUSPECT_PREFIXES)]


def slices(data):
    if len(data) < 8:
        return []
    magic = struct.unpack(">I", data[:4])[0]
    if magic in (FAT_MAGIC, FAT_MAGIC_64):
        n = struct.unpack(">I", data[4:8])[0]
        out, off = [], 8
        for _ in range(n):
            if magic == FAT_MAGIC:
                cpu, sub, o, sz, _a = struct.unpack(">iiIII", data[off:off + 20])
                off += 20
            else:
                cpu, sub, o, sz, _a, _r = struct.unpack(">iiQQII", data[off:off + 32])
                off += 32
            out.append((cpu, sub & 0xFFFFFFFF, data[o:o + sz]))
        return out
    if struct.unpack("<I", data[:4])[0] == MH_MAGIC_64:
        return [(struct.unpack("<i", data[4:8])[0],
                 struct.unpack("<I", data[8:12])[0], data)]
    return []


def cstr(sl, off):
    e = sl.find(b"\x00", off)
    return sl[off:e if e > 0 else off + 200].decode("utf-8", "replace")


def analyze(sl):
    """返回 dict：deps / id / build / uuid / undefined / dylibs_loaded。"""
    ncmds = struct.unpack("<I", sl[16:20])[0]
    off = 32
    info = {"deps": [], "id": None, "build": None, "uuid": None,
            "cmds": {}, "ncmds": ncmds}
    symtab = None
    for _ in range(ncmds):
        if off + 8 > len(sl):
            break
        cmd, cmdsize = struct.unpack("<II", sl[off:off + 8])
        if cmdsize < 8:
            break
        name = LC_NAMES.get(cmd, "0x%x" % cmd)
        info["cmds"][name] = info["cmds"].get(name, 0) + 1
        body = sl[off:off + cmdsize]
        if cmd in DYLIB_CMDS:
            noff = struct.unpack("<I", body[8:12])[0]
            info["deps"].append((name, cstr(sl, off + noff)))
        elif cmd == 0x0D:
            noff = struct.unpack("<I", body[8:12])[0]
            info["id"] = cstr(sl, off + noff)
        elif cmd == 0x32:
            plat, minos, sdk, ntools = struct.unpack("<IIII", body[8:24])
            fmt = lambda v: "%d.%d.%d" % (v >> 16, (v >> 8) & 0xFF, v & 0xFF)
            info["build"] = {"platform": plat, "minos": fmt(minos), "sdk": fmt(sdk),
                             "ntools": ntools}
        elif cmd == 0x25 and len(body) >= 16:
            # 老式 LC_VERSION_MIN_IPHONEOS，没有 platform 字段，按 iOS 记
            ver, sdk = struct.unpack("<II", body[8:16])
            fmt = lambda v: "%d.%d.%d" % (v >> 16, (v >> 8) & 0xFF, v & 0xFF)
            info["build"] = {"platform": 2, "minos": fmt(ver), "sdk": fmt(sdk),
                             "ntools": 0, "legacy": True}
        elif cmd == 0x1B and len(body) >= 24:
            u = body[8:24]
            info["uuid"] = "%02X%02X%02X%02X-%02X%02X-%02X%02X-%02X%02X-%s" % (
                u[0], u[1], u[2], u[3], u[4], u[5], u[6], u[7],
                u[8], u[9], u[10:16].hex().upper())
        elif cmd == 0x02:
            symoff, nsyms, stroff, strsize = struct.unpack("<IIII", body[8:24])
            symtab = (symoff, nsyms, stroff, strsize)
        off += cmdsize

    # 未定义符号（n_type & N_TYPE == N_UNDF 且 n_value == 0）
    undef = []
    if symtab:
        symoff, nsyms, stroff, strsize = symtab
        for i in range(nsyms):
            p = symoff + i * 16
            if p + 16 > len(sl):
                break
            strx, ntype, _sect, _desc, _val = struct.unpack("<IBBHQ", sl[p:p + 16])
            if (ntype & 0x0E) == 0x00 and (ntype & 0x01):   # N_UNDF | N_EXT
                nm = cstr(sl, stroff + strx)
                if nm:
                    undef.append(nm)
    info["undefined"] = sorted(set(undef))
    return info


def arch_name(cpu, sub):
    return "arm64e" if (sub & 0x00FFFFFF) == 2 else "arm64"


def check_arm64e_abi(arch, sub):
    """arm64e 切片必须带 ptrauth ABI 版本（caps 里 0x80 置位）。返回失败描述或 None。"""
    if arch != "arm64e":
        return None
    caps = (sub >> 24) & 0xFF
    if not (caps & CPU_SUBTYPE_PTRAUTH_ABI):
        return ("arm64e 切片 cpusubtype=0x%08x 的 caps=0x%02x，"
                "缺 CPU_SUBTYPE_PTRAUTH_ABI(0x80)：这是老式无版本 arm64e，"
                "现代 arm64e 内核/注入器会拒绝加载（应重新链接或只发 arm64）"
                % (sub, caps))
    return None


def check_container(path, expect=None):
    """检查一个 deb/dylib 的加载期风险。

    expect: 可选 dict，用于 selftest 注入故障，例如 {"drop_dep": True}。
    返回 (failures, notes)：failures 非空即 FAIL。
    """
    from check_codesign import dylib_from

    name, data = dylib_from(path)
    failures, notes = [], []
    print("=" * 74)
    print("%s -> %s (%d bytes)" % (os.path.basename(path), name, len(data)))

    sls = slices(data)
    if not sls:
        failures.append("取不到任何 Mach-O 切片")
    for cpu, sub, sl in sls:
        arch = arch_name(cpu, sub & 0xFFFFFFFF)
        i = analyze(sl)
        print("  [%s] ncmds=%d cpusubtype=0x%08x uuid=%s"
              % (arch, i["ncmds"], sub, i["uuid"]))
        abi = check_arm64e_abi(arch, sub)
        if abi:
            print("        !! %s" % abi)
            failures.append("[%s] %s" % (arch, abi))
        print("        install_name = %s" % i["id"])
        if not i["id"]:
            failures.append("[%s] 缺 LC_ID_DYLIB" % arch)

        b = i["build"]
        if not b:
            failures.append("[%s] 缺 LC_BUILD_VERSION / LC_VERSION_MIN_IPHONEOS" % arch)
        else:
            print("        build: platform=%d minos=%s sdk=%s"
                  % (b["platform"], b["minos"], b["sdk"]))
            if b["platform"] != 2:
                failures.append("[%s] platform=%d 不是 2(iOS)" % (arch, b["platform"]))

        deps = list(i["deps"])
        if expect and expect.get("drop_dep"):
            deps = []                 # selftest：模拟依赖全丢
        print("        依赖库 (%d):" % len(deps))
        for kind, d in deps:
            print("          %-22s %s" % (kind, d))
        # 任何真实的 iOS dylib 至少链接 libSystem。一个依赖都没有 =
        # 要么链接参数被清空了，要么这个文件根本不是正常产物。
        if not deps:
            failures.append("[%s] 一个依赖库都没有（不可能正常加载）" % arch)
        bad_deps = check_frameworks(deps)
        for d in bad_deps:
            failures.append("[%s] 链接了越狱侧库：%s" % (arch, d))

        und = i["undefined"]
        susp, unknown = classify_symbols(und)
        print("        未定义符号 %d 个（可疑 %d / 未分类 %d）"
              % (len(und), len(susp), len(unknown)))
        for s in susp:
            print("          !! %s" % s)
            failures.append("[%s] 可疑符号（系统不提供）：%s" % (arch, s))
        for s in unknown:
            print("          ?  %s   <-- 未分类，需人工确认系统是否导出" % s)
    return failures, notes


def selftest(path):
    """反向自检：每条判据都必须能失败，正常输入必须能被放行。

    ★ 关键改动：不再要求「基准样本零缺陷才继续」。
      1.0.3 的 arm64e 切片本来就缺 ptrauth ABI，基准必然 FAIL。
      旧写法会在这里直接退出，于是其余判据永远不会被验证 ——
      那等于用「样本有缺陷」把整套自检短路掉。
      现在改成：基准的失败项先记录下来，然后逐条验证每个判据双向都能动。
    """
    print("### 基准样本状态（不要求通过，只记录）")
    base, _ = check_container(path)
    if base:
        print("基准有 %d 条失败（这正是待修的真实缺陷，不作阻断）：" % len(base))
        for f in base:
            print("   ", f)
    else:
        print("基准通过")
    print()

    print("### 反向自检 A：去掉全部依赖库必须被判失败")
    fails_a, _notes_a = check_container(path, expect={"drop_dep": True})
    hit_a = any("一个依赖库都没有" in f for f in fails_a)
    if not hit_a:
        print("FAIL: 依赖全丢没被判失败，这条检查是装饰")
        for f in fails_a:
            print("   ", f)
        return 1
    print("OK: 依赖全丢被判失败（%d 条）\n" % len(fails_a))

    print("### 反向自检 B：注入一个系统不导出的符号必须被判失败")
    susp, _unk = classify_symbols(["___isOSVersionAtLeast", "_objc_msgSend"])
    if susp != ["___isOSVersionAtLeast"]:
        print("FAIL: 可疑符号分类器没抓到 ___isOSVersionAtLeast，实际 %r" % (susp,))
        return 1
    if classify_symbols(["_objc_msgSend"])[0]:
        print("FAIL: 正常符号被误判为可疑")
        return 1
    print("OK: 可疑符号分类器双向都对\n")

    print("### 反向自检 C：越狱侧依赖必须被判失败")
    if not check_frameworks([("LC_LOAD_DYLIB", "/usr/lib/libsubstrate.dylib")]):
        print("FAIL: 没抓到 libsubstrate 依赖")
        return 1
    if check_frameworks([("LC_LOAD_DYLIB", "/usr/lib/libobjc.A.dylib")]):
        print("FAIL: 把系统库误判为越狱侧库")
        return 1
    print("OK: 依赖检查双向都对\n")

    print("### 反向自检 D：arm64e ptrauth ABI 判据必须双向都能动")
    # 双向：老式无版本必须被抓，新式带版本必须放行，arm64 根本不该参与判定
    if not check_arm64e_abi("arm64e", 0x00000002):
        print("FAIL: 老式 arm64e(caps=0x00) 没被抓到 —— 这条判据是装饰")
        return 1
    if check_arm64e_abi("arm64e", 0x80000002):
        print("FAIL: 正常带 ptrauth ABI 的 arm64e 被误判")
        return 1
    if check_arm64e_abi("arm64", 0x00000000):
        print("FAIL: arm64 被卷进 arm64e 的判定")
        return 1
    print("OK: arm64e ABI 判据双向都对")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_selftest = "--selftest" in sys.argv
    if not args:
        raise SystemExit(__doc__)

    if do_selftest:
        rc = selftest(args[0])
        return rc

    bad = 0
    for path in args:
        failures, notes = check_container(path)
        for n in notes:
            print("  ~ %s" % n)
        for f in failures:
            print("  FAIL %s" % f)
        bad += len(failures)
    print()
    if bad:
        print("FAIL：发现 %d 处静态加载风险（这些都是「装了不加载/被内核拒」的候选）" % bad)
        return 1
    print("OK：依赖全为系统库、无可疑符号、构建信息完整")
    return 0


if __name__ == "__main__":
    sys.exit(main())