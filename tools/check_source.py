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
    """按花括号配平取函数体；跳过同名的前向声明。

    踩过的坑：Tweak.m 里 `static void VTInvoke(...);` 有一条前向声明，
    旧实现直接 code.find("{", idx) 会跨过这条声明，取到后面**别的函数**的
    函数体 → VTInvoke 的三条判据变成恒 FAIL，且从报错里看不出是判据写错了。
    所以每个候选匹配都要先看签名后面第一个结束符是 '{'（定义）还是 ';'（声明）。
    """
    start = 0
    while True:
        idx = code.find(signature, start)
        if idx < 0:
            return None
        j = idx + len(signature)
        while j < len(code) and code[j] not in "{;":
            j += 1
        if j >= len(code):
            return None
        if code[j] == ";":
            start = j + 1          # 前向声明，继续找下一个同签名位置
            continue
        brace = j
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
    chk('精确类名安装含 STUIStatusBar（iOS 17 私有体系）',
        'VTHookNamedClass("STUIStatusBar",' in nc,
        "未找到精确类安装目标 STUIStatusBar：iOS 17 上 _UIStatusBar* 不会被实例化")

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

    # ---------- 7. hook 安装器必须安全 ----------
    hm = body_of(code, "static BOOL VTHookMethod")
    chk("VTHookMethod 存在", hm is not None, "缺 VTHookMethod")
    if hm:
        chk("VTHookMethod 有入参空检查", "if (!c || !sel) return NO;" in hm,
            "VTHookMethod 缺入参空检查")
        chk("VTHookMethod 防同一(类,SEL)重复安装",
            "gHooks[i].cls == c" in hm and "return NO" in hm,
            "缺重复安装检查：同一类同一个 SEL 装两次会把 orig 指向自己的 repl")
        chk("VTHookMethod 有槽位上限", "gHookCount >= VT_MAX_HOOKS" in hm,
            "缺槽位上限：会写穿 gHooks 数组")
        chk("原 IMP 精确绑定到槽位", "gHooks[gHookCount].orig    = eff" in hm,
            "orig 没有写进该 hook 自己的槽位")

    # 记录与替换必须分离。若边记录边替换，而两个被 hook 的类存在继承关系，
    # 后装的类用 class_getInstanceMethod 会取到前一个已被换掉的 trampoline，
    # orig 就指向我们自己的 hook —— 双重染色，极端情况下又是递归。
    cm = body_of(code, "static void VTCommitHooks")
    ini = body_of(code, "static void VTInitialize")
    chk("VTHookMethod 只预约不替换 IMP",
        hm is not None and "class_replaceMethod" not in hm,
        "安装阶段就替换了 IMP：继承关系下会把 orig 记成自己的 trampoline")
    chk("VTCommitHooks 统一替换 IMP",
        cm is not None and "class_replaceMethod(r->cls, r->sel, repl, r->types);" in cm,
        "缺统一提交：所有槽位记录完之前不能动任何 IMP")
    chk("VTInitialize 调用 VTCommitHooks",
        ini is not None and "VTCommitHooks();" in ini,
        "记录了但没提交：一条 hook 都不会生效")
    chk("HookLevel=0 时完全不装 hook",
        ini is not None and "if (gHookLevel <= 0) {" in ini,
        "缺无卸载降级：再出安全模式时用户无法自证是不是本插件造成的")

    # ---------- 8. 调回原实现（漏了 = 系统原行为被吞掉） ----------
    # 1.0.3 起所有 hook 共用一个 VTInvoke，必须保证每个分支最终都调一次 VTCallOrig。
    iv = body_of(code, "static void VTInvoke")
    chk("VTInvoke 存在", iv is not None, "缺 VTInvoke")
    if iv:
        chk("VTInvoke 每个分支都调回原实现", "VTCallOrig(r, self, _cmd, pass, a2);" in iv,
            "VTInvoke 漏了调回原实现，状态栏会停止刷新")
        chk("VTInvoke 有递归深度保险", "gChainDepth >= VT_MAX_CHAIN_DEPTH" in iv,
            "缺递归深度保险：意外重入会直接栈溢出（@try 拦不住 SIGSEGV）")
        chk("VTInvoke 深度计数成对增减",
            "gChainDepth++" in iv and "gChainDepth--" in iv,
            "gChainDepth 不是成对增减，会永久卡在某个深度")
        pc = iv.find("@catch")
        po = iv.find("VTCallOrig(r, self, _cmd, pass, a2);")
        chk("prepare 异常不吞原实现",
            "pass = a1;" in iv and 0 <= pc < po,
            "原实现调用仍在 prepare 的 @try 里：染色逻辑一抛异常就把这次状态栏更新吞掉")

    co = body_of(code, "static void VTCallOrig")
    chk("VTCallOrig 存在", co is not None, "缺 VTCallOrig")
    if co:
        chk("VTCallOrig 只用槽位绑定的 orig", "r->orig" in co and "object_getClass" not in co,
            "VTCallOrig 又在按 self 的类查表 —— 这正是 1.0.2 递归崩溃的根因")
        chk("VTCallOrig 对空 IMP 有保护", "if (!imp) return;" in co,
            "VTCallOrig 缺空 IMP 检查")

    # ---------- 8b. 崩溃防护（1.0.2 进安全模式的四条根因，逐条钉死） ----------
    # 负向断言一律在剥过注释的文本上做，否则文件头那段事故说明会被判成违规。
    chk("不做全量类表遍历 objc_copyClassList",
        "objc_copyClassList" not in code,
        "又出现全量类表遍历：启动早期对私有类触发 +initialize 会崩宿主")
    chk("不按实例的类查原 IMP（VTFindOrig/VTChain 已废除）",
        "VTFindOrig" not in code and "VTChain1" not in code and "VTChain2" not in code,
        "又出现按 object_getClass(self) 沿继承链查 orig 的写法")
    chk("不用 __thread/TLS（越狱 dylib 加载期风险）",
        "__thread" not in code and "_Thread_local" not in code,
        "TLS 变量依赖 dyld 的 TLV 支持，加载期就可能失败")
    chk("初始化延迟到主队列，不在 constructor 里改 IMP",
        "VTConstructor" in code and "dispatch_async(dispatch_get_main_queue()" in code,
        "缺 VTConstructor 延迟初始化：constructor 阶段 Foundation/私有框架尚未就绪")

    tramps = re.findall(r"VT_TRAMP\((\d+)\)", nc)
    cases = re.findall(r"VT_CASE\((\d+)\)", nc)
    chk("每个槽位都有独立 trampoline（24 个且编号一致）",
        len(tramps) == 24 and len(cases) == 24 and tramps == cases,
        "VT_TRAMP=%d VT_CASE=%d，槽位数不匹配会让部分 hook 拿到别人的 orig"
        % (len(tramps), len(cases)))

    chk("有 HookLevel 渐进开关（出问题时可不卸载就降级）",
        "gHookLevel" in code and 'd[@"HookLevel"]' in nc,
        "缺 HookLevel：用户遇到安全模式只能卸载重装，无法分级定位")

    # 补染/还原不能再直接取 methodForSelector 的 IMP：那拿到的是我们替换后的
    # repl，直调会二次进入染色逻辑并可能重入。必须走正常消息派发。
    rp = body_of(nc, "static void VTRepaintSeen")
    rs = body_of(nc, "static void VTRestoreSlot")
    chk("补染走 objc_msgSend 而非直取 IMP",
        rp is not None and "objc_msgSend" in rp and "methodForSelector:@selector(set" not in rp,
        "VTRepaintSeen 里仍在直调 methodForSelector 拿到的 IMP")
    chk("还原走 objc_msgSend 而非直取 IMP",
        rs is not None and "objc_msgSend" in rs,
        "VTRestoreSlot 里仍在直调 methodForSelector 拿到的 IMP")

    # ---------- 8c. 观测手段本身必须可验证（1.0.3 实机事故）----------
    # 用户装 1.0.3 仍进安全模式且「完全找不到 VpnTint.log」。两条原因：
    # ① VTLog 只写 /var/mobile/Library/Logs 与 /tmp，SpringBoard 沙箱可能都写不进去，
    #    gLogPath=nil 后所有日志静默丢弃；② 首条日志排在所有 hook 安装之后，
    #    崩在初始化段时用户看到的就是「崩了 + 零日志」，无法定位。
    # 「没日志」不等于「没发生」——先把观测建起来，再谈根因。
    ctor = body_of(nc, "static void VTConstructor")
    chk("dylib 加载后立即打点（首条日志在任何 hook 之前）",
        ctor is not None and "[0/7]" in ctor,
        "constructor 没有先打点：崩在初始化段时用户拿不到任何证据")
    steps = re.findall(r'VTLog\(@"\[(\d)/7\]', nc)
    chk("初始化分步打点齐全（0..7）",
        sorted(set(steps)) == list("01234567"),
        "缺分步打点：崩在哪一步只能靠猜（实测 %s）" % sorted(set(steps)))
    chk("日志同时进系统 os_log（NSLog 兜底）",
        'NSLog(@"VpnTint: %@", msg);' in nc,
        "只写文件：沙箱写不进去时所有日志静默丢失")
    chk("日志路径有试写探针与沙箱 home 候选",
        "NSHomeDirectory()" in nc and
        "probe = [NSFileHandle fileHandleForWritingAtPath:p]" in nc,
        "缺可写性验证：fileExists / 目录建成功都不等于能写文件")

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
        # ---- 1.0.0/1.0.1「完全没效果」那一批教训 ----
        ("砍掉 STUIStatusBar（模拟 1.0.1 的错误 hook 目标）",
         raw.replace('VTHookNamedClass("STUIStatusBar",',
                     'VTHookNamedClass("_UIStatusBarItemView",'),
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

        # ---- 1.0.2「进安全模式」那一批教训：每条防护都要能失败 ----
        ("VTCallOrig 改回按 self 的类查表（1.0.2 递归根因）",
         raw.replace("IMP imp = r ? r->orig : NULL;",
                     "IMP imp = VTFindOrig(object_getClass(self), _cmd);"),
         "只用槽位绑定的 orig"),
        ("删掉递归深度保险",
         raw.replace("if (gChainDepth >= VT_MAX_CHAIN_DEPTH) {", "if (0) {"),
         "递归深度保险"),
        ("深度计数不再成对增减",
         raw.replace("gChainDepth--;", "/* no decrement */"),
         "成对增减"),
        ("漏掉调回原实现",
         raw.replace("VTCallOrig(r, self, _cmd, pass, a2);", "/* no orig call */"),
         "每个分支都调回原实现"),
        ("加回全量类表遍历",
         raw.replace("static int VTInstall17(void) {",
                     "static int VTInstall17(void) {\n"
                     "    unsigned int dummy = 0; objc_copyClassList(&dummy);"),
         "全量类表遍历"),
        ("初始化改回 constructor 里同步执行",
         raw.replace("dispatch_async(dispatch_get_main_queue(), ^{",
                     "dispatch_async(dispatch_get_global_queue(0, 0), ^{"),
         "延迟到主队列"),
        ("砍掉一个 trampoline 槽位",
         raw.replace("VT_TRAMP(23)\n", ""),
         "独立 trampoline"),
        ("用回 TLS 变量",
         raw.replace("static volatile int gChainDepth = 0;",
                     "static __thread int gChainDepth = 0;"),
         "TLS"),
        ("去掉 HookLevel 渐进开关",
         raw.replace("gHookLevel", "gLevelFixed"),
         "HookLevel"),
        ("补染又去直取 methodForSelector 的 IMP",
         raw.replace("((void (*)(id, SEL, id))objc_msgSend)(v, @selector(setActiveColor:), gTintColor);",
                     "IMP imp = [v methodForSelector:@selector(setActiveColor:)];\n"
                     "                    ((void (*)(id, SEL, id))imp)(v, @selector(setActiveColor:), gTintColor);"),
         "补染走 objc_msgSend"),
        ("还原又去直取 methodForSelector 的 IMP",
         raw.replace("((void (*)(id, SEL, id))objc_msgSend)(view, sel, color);",
                     "IMP imp = [view methodForSelector:sel];\n"
                     "        ((void (*)(id, SEL, id))imp)(view, sel, color);"),
         "还原走 objc_msgSend"),
        ("去掉 hook 槽位上限",
         raw.replace("if (gHookCount >= VT_MAX_HOOKS) return NO;", ""),
         "槽位上限"),
        ("去掉重复安装检查",
         raw.replace("if (gHooks[i].cls == c && sel_isEqual(gHooks[i].sel, sel)) return NO;", ""),
         "重复安装"),
        ("orig 写错槽位",
         raw.replace("gHooks[gHookCount].orig    = eff;", "gHooks[0].orig = eff;"),
         "精确绑定到槽位"),
        ("VTCallOrig 去掉空 IMP 保护",
         raw.replace("if (!imp) return;", ""),
         "空 IMP"),
        ("VTHookMethod 去掉入参空检查",
         raw.replace("if (!c || !sel) return NO;", ""),
         "入参空检查"),
        ("安装阶段又直接替换 IMP（回到边记录边替换）",
         raw.replace("    gHookCount++;\n    return YES;\n}\n\n/// 统一提交",
                     "    gHookCount++;\n"
                     "    class_replaceMethod(c, sel, repl, method_getTypeEncoding(m));\n"
                     "    return YES;\n}\n\n/// 统一提交"),
         "只预约不替换"),
        ("删掉统一提交（记录完不装 hook）",
         raw.replace("            VTCommitHooks();   // 所有 orig 记录完毕后，才统一替换 IMP\n", ""),
         "VTInitialize 调用"),
        ("原实现调用挪回 prepare 的 try 里（异常吞掉状态栏更新）",
         raw.replace("@try { VTCallOrig(r, self, _cmd, pass, a2); }",
                     "/* 原实现调用被挪走了 */"),
         "prepare 异常不吞"),
        ("去掉 HookLevel=0 无害降级",
         raw.replace("if (gHookLevel <= 0) {", "if (0) {"),
         "HookLevel=0"),
        ("砍掉 constructor 的加载打点（回到崩了也零日志）",
         raw.replace('VTLog(@"[0/7] dylib 已加载，constructor 进入主队列");',
                     "/* no load log */"),
         "加载后立即打点"),
        ("砍掉一个分步打点",
         raw.replace('VTLog(@"[5/7] hook 记录完成 %d 条，准备统一替换 IMP", hooks);\n', ""),
         "分步打点齐全"),
        ("去掉 NSLog 兜底（回到只写文件）",
         raw.replace('NSLog(@"VpnTint: %@", msg);   // 系统日志兜底：不依赖任何文件是否可写\n', ""),
         "os_log"),
        ("去掉日志试写探针",
         raw.replace("probe = [NSFileHandle fileHandleForWritingAtPath:p]",
                     "probe = nil"),
         "试写探针"),
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