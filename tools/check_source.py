#!/usr/bin/env python3
"""VpnTint 源码闸门。

教训背景（1.0.0 / 1.0.1 完全没效果）：
  插件装在 SpringBoard 里、日志也有「已加载」，但状态栏毫无变化。根因是
  **hook 目标类选错了**：iOS 17 起状态栏迁到 StatusStatusUI.framework，
  类名从 _UIStatusBar* 换成 STUIStatusBar*。旧实现 hook 的
  _UIStatusBarItemView 在 iOS 17 上根本不会被实例化 → 一个触发点都没有。

  这类错误编译能过、包能装、日志也不报错，只有实机看效果才发现 —— 所以
  必须有一条能在 CI 里跑、且**有可能失败**的判据把它钉住。

设计原则（全部来自实际踩坑）：
  1. 每条判据都必须能失败。写完先反问「它有没有可能失败」，答不出来 = 装饰。
  2. 判「有没有某个 token」用 strip_code（连字符串一起抹）；
     判「某个字面量在不在」用 strip_comments（保留字符串）。
     方向用错会让判据永真或永假 —— 本脚本第一版就踩了这个坑：
     用 strip_code 去查 objc_getClass("STUIStatusBar")，字符串被抹成空格，必失败。
  3. 判据要锚住被检查的那一段：用 body_of() 按花括号配平取函数体，
     不能用「两个锚点之间」—— 中间夹的注释/别的函数会代答。
  4. 负向断言必须在剥过注释的文本上做，否则自己的说明文字会被判成违规。
  5. 报错信息带实际值，失败时能直接看出差在哪。
  6. 失败信息不能和成功信息一起打（第一版 OK 行后面还跟着「缺xxx」，
     读起来像自相矛盾）。

用法：python3 tools/check_source.py [源码路径]
      退出 0 = 全通过；1 = 有失败项
"""
import os
import re
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SRC = os.path.join(_REPO, "Tweak.m")

fails, oks = [], []
QUIET = False   # 反向自检期间置 True，避免与变体输出混在一起看不清


def chk(name, ok, detail="", quiet=None):
    q = QUIET if quiet is None else quiet
    if ok:
        oks.append(name)
        if not q:
            print("  OK   " + name)
    else:
        fails.append((name, detail))
        if not q:
            print("  FAIL " + name + (("  -> " + detail) if detail else ""))


def strip_comments(s):
    """注释抹成空格，**保留字符串字面量内容**。用于「字面量在不在」判定。"""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        nx = s[i + 1] if i + 1 < n else ""
        if c == "/" and nx == "/":
            while i < n and s[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == "/" and nx == "*":
            out.append("  ")
            i += 2
            while i < n - 1 and not (s[i] == "*" and s[i + 1] == "/"):
                out.append("\n" if s[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        if c in ('"', "'"):
            q = c
            out.append(q)
            i += 1
            while i < n:
                if s[i] == "\\":
                    out.append(s[i:i + 2])
                    i += 2
                    continue
                out.append(s[i])
                if s[i] == q:
                    i += 1
                    break
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def strip_code(s):
    """注释 + 字符串内容都抹掉。用于「token 在不在」判定。"""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        nx = s[i + 1] if i + 1 < n else ""
        if c == "/" and nx == "/":
            while i < n and s[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == "/" and nx == "*":
            out.append("  ")
            i += 2
            while i < n - 1 and not (s[i] == "*" and s[i + 1] == "/"):
                out.append("\n" if s[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        if c in ('"', "'"):
            q = c
            out.append(q)
            i += 1
            while i < n:
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == q:
                    out.append(q)
                    i += 1
                    break
                out.append(" ")
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def body_of(code, signature):
    """按花括号配平取函数体。"""
    idx = code.find(signature)
    if idx < 0:
        return None
    brace = code.find("{", idx)
    if brace < 0:
        return None
    depth = 0
    for i in range(brace, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[brace:i + 1]
    return None


def run_checks(raw, quiet=False):
    """返回 (oks, fails)。抽成函数是为了让 --selftest 能喂变体源码反向验证。"""
    global fails, oks
    fails, oks = [], []

    code = strip_code(raw)      # token 判定
    nc = strip_comments(raw)    # 字面量判定

    # ---------- 1. hook 目标必须是 iOS 17 的 STUIStatusBar* 体系 ----------
    # 这是 1.0.0/1.0.1 完全失效的根因，必须钉死。判字面量 → 用 nc。
    chk('objc_getClass("STUIStatusBar") 在场（iOS 17 私有体系）',
        'objc_getClass("STUIStatusBar")' in nc,
        "未找到 objc_getClass(\"STUIStatusBar\"): iOS 17 上 _UIStatusBar* 不会被实例化")

    chk("装有 _updateWithAggregatedData: hook",
        "_updateWithAggregatedData:" in nc,
        "缺该选择器：拿不到 vpnEntry.enabled")

    chk("装有 _updateWithData:completionHandler: hook",
        "_updateWithData:completionHandler:" in nc,
        "缺该选择器")

    # 必须 dlopen StatusStatusUI：SpringBoard 启动早期该框架可能还没加载
    chk("dlopen 了 StatusStatusUI.framework",
        "StatusStatusUI.framework" in nc,
        "未 dlopen：启动早期 objc_getClass 可能拿不到 STUIStatusBar")

    # ---------- 2. 信号视图必须走 activeColor / inactiveColor ----------
    # 1.0.0/1.0.1 对信号视图写 tintColor，iOS 17 上无效（信号格是自绘的）。
    chk("hook 了 setActiveColor:", "@selector(setActiveColor:)" in nc,
        "缺 setActiveColor: hook")
    chk("hook 了 setInactiveColor:", "@selector(setInactiveColor:)" in nc,
        "缺 setInactiveColor: hook")

    # ---------- 3. 未激活色必须保留 alpha ----------
    chk("不使用 colorWithAlphaComponent（会替换 alpha 导致图标消失）",
        "colorWithAlphaComponent" not in code,
        "源码里出现 colorWithAlphaComponent，会把系统 alpha 替换成给定值")

    ka = body_of(code, "static UIColor *VTKeepAlpha")
    chk("VTKeepAlpha 存在", ka is not None, "缺 VTKeepAlpha")
    if ka:
        chk("VTKeepAlpha 用原色的 alpha 组装新色",
            "alpha:sa" in ka.replace(" ", ""),
            "未用 original 的 alpha（应为 alpha:sa）")

    # ---------- 4. 启动闸门 ----------
    chk("存在启动闸门 gReady",
        re.search(r"gReady\s*=\s*1", code) is not None,
        "没有把 gReady 置 1 的地方，闸门永远不会开")
    m = re.search(r"#define\s+VT_READY_MS\s+(\d+)", code)
    chk("闸门时长 >= 300ms", m is not None and int(m.group(1)) >= 300,
        ("VT_READY_MS 过小，会撞 respring 首帧" if m else "缺 VT_READY_MS 定义"))

    # ---------- 5. 断开后必须主动还原 ----------
    nt = body_of(code, "static void VTNoteTransition")
    chk("VTNoteTransition 存在", nt is not None, "缺 VTNoteTransition")
    if nt:
        chk("断开（1→0）时调用还原", "VTRestoreAsync" in nt,
            "断开分支没有调用还原：橙色会一直留在屏幕上")

    rn = body_of(nc, "static void VTRestoreNow")
    chk("VTRestoreNow 存在", rn is not None, "缺 VTRestoreNow")
    if rn:
        for slot in ("setTextColor:", "setActiveColor:", "setInactiveColor:"):
            chk("还原覆盖 %s" % slot, ('@"%s"' % slot) in rn,
                "还原计划里缺 %s" % slot)

    # ---------- 6. 不得出现会打断 dpkg 的动作 ----------
    # 历史事故：postinst 里 killall -9 SpringBoard → Sileo 报「dpkg 被中断」。
    banned = re.findall(r"killall|sbreload|respring", code, re.I)
    chk("源码不含重启桌面动作", not banned, "出现: %s" % sorted(set(banned)))

    # ---------- 7. 每个 hook 都必须有空检查 ----------
    hm = body_of(code, "static BOOL VTHookMethod")
    chk("VTHookMethod 存在", hm is not None, "缺 VTHookMethod")
    if hm:
        chk("VTHookMethod 有对空检查", "if (!c || !sel || !repl)" in hm,
            "VTHookMethod 缺空检查")

    # ---------- 8. 链式调用不能漏（漏了 = 系统原行为被吞掉） ----------
    chain_missing = []
    for fn, chain in (
        ("static void VTHookUpdateAggregated", "VTChain1"),
        ("static void VTHookUpdateWithCompletion", "VTChain2"),
        ("static void VTHookStringApplyStyle", "VTChain1"),
        ("static void VTHookStringSetText", "VTChain1"),
        ("static void VTHookSetActiveColor", "VTChain1"),
        ("static void VTHookSetInactiveColor", "VTChain1"),
    ):
        b = body_of(code, fn)
        if b is None or chain not in b:
            chain_missing.append(fn.split()[-1])
    chk("所有 hook 都链式调回原实现", not chain_missing,
        "缺: %s" % ", ".join(chain_missing))

    # ---------- 9. VPN 判定双源（数据层为主，代理设置为兜底） ----------
    chk("VPN 判定读 vpnEntry",
        "vpnEntry" in nc, "没有读 vpnEntry，状态判定会只剩代理设置兜底")
    chk("保留 CFNetwork 兜底",
        "CFNetworkCopySystemProxySettings" in code,
        "缺代理设置兜底：数据层 hook 万一再失配就彻底没救")

    # ---------- 10. 不许用 @available ----------
    # 实测：本工程工具链（clang-11）里 @available 会编成对 ___isOSVersionAtLeast 的
    # 引用，该符号在其 libSystem 里不存在 → 链接阶段 Undefined symbols，构建挂掉。
    # 编译是过的，只在 Linking 崩，很容易误判成语法错误而白找一轮。
    av = re.findall(r"@available\s*\(", code)
    chk("不使用 @available（工具链缺 ___isOSVersionAtLeast，链接会失败）",
        not av, "出现 %d 处 @available，改用 NSProcessInfo 版本比较" % len(av))
    chk("用 NSProcessInfo 判断系统版本",
        "isOperatingSystemAtLeastVersion" in code,
        "缺版本判断，iOS 17 / iOS 16 两套 hook 无法分流")

    return list(oks), list(fails)


def selftest(raw, path):
    """反向自检：把几条关键机制逐一破坏，确认闸门每次都报 FAIL。

    不做这一步，闸门就是装饰 —— 它可能一直在「通过」，而从没检查到东西。
    """
    global QUIET
    print("== 反向自检：确认每条关键判据都能失败 ==")
    variants = [
        ("砍掉 STUIStatusBar（模拟 1.0.1 的错误 hook 目标）",
         raw.replace('objc_getClass("STUIStatusBar")', 'objc_getClass("_UIStatusBarItemView")'),
         "STUIStatusBar"),
        ("砍掉 vpnEntry 读取",
         raw.replace("vpnEntry", "vpnFoo"),
         "vpnEntry"),
        ("引入 colorWithAlphaComponent（模拟压 alpha 的旧写法）",
         raw.replace("static UIColor *VTKeepAlpha(UIColor *original, UIColor *hue) {",
                     "static UIColor *VTKeepAlpha(UIColor *original, UIColor *hue) {\n"
                     "    if (hue) return [hue colorWithAlphaComponent:0.2];"),
         "colorWithAlphaComponent"),
        ("把 gReady 置 1 删掉（闸门永不开）",
         raw.replace("gReady = 1;", "gReady = 0;"),
         "gReady"),
        ("断开不还原",
         raw.replace("VTRestoreAsync();\n    }", "/* no restore */\n    }", 1),
         "断开"),
        ("换回 @available（工具链缺 ___isOSVersionAtLeast，链接必挂）",
         raw.replace("BOOL is17 = VTIsIOS17OrLater();",
                     "BOOL is17 = NO; if (@available(iOS 17, *)) is17 = YES;"),
         "@available"),
    ]

    QUIET = True
    ok_all = True
    for name, mutated, needle in variants:
        if mutated == raw:
            print("  FAIL 变体未生效（字符串替换没命中）: " + name)
            ok_all = False
            continue
        o, f = run_checks(mutated)
        names = [n for n, _ in f]
        hit = any(needle in n for n in names)
        if hit:
            print("  OK   变体被拦住: " + name)
        else:
            print("  FAIL 变体没被拦住: " + name + "（该判据是装饰）")
            ok_all = False

    # 未被破坏的原始源码必须仍然全通过，否则上面的「拦住」没有意义
    o, f = run_checks(raw)
    if f:
        print("  FAIL 原始源码未通过，自检基线无效: %s" % [n for n, _ in f])
        ok_all = False
    else:
        print("  OK   原始源码基线全通过（%d 项）" % len(o))
    QUIET = False

    print()
    print("OK：反向自检全部有效" if ok_all else "FAIL：反向自检存在装饰判据")
    return 0 if ok_all else 1


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else DEFAULT_SRC
    if not os.path.exists(path):
        raise SystemExit("找不到源码: " + path)
    raw = open(path, encoding="utf-8").read()
    print("== 源码闸门：%s (%d bytes) ==" % (os.path.basename(path), len(raw)))

    if "--selftest" in sys.argv:
        return selftest(raw, path)

    o, f = run_checks(raw)
    print()
    if f:
        print("FAIL: %d 项不通过 / 共 %d 项" % (len(f), len(f) + len(o)))
        for n, d in f:
            print("      · %s%s" % (n, ("  (" + d + ")") if d else ""))
        return 1
    print("OK: %d 项全部通过" % len(o))
    return 0


if __name__ == "__main__":
    sys.exit(main())