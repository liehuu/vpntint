// ============================================================================
// VpnTint —— VPN 连接时把状态栏 Wi-Fi 图标 / 蜂窝信号 / 4G-5G 字样染成橙色
//
// 1.0.2 重写。前两版「完全没效果」的根因有两条，都必须记住：
//
//   ① hook 目标类选错了（致命）。
//      iOS 17 起状态栏迁到私有框架 StatusStatusUI.framework，类名从
//      _UIStatusBar* 换成 STUIStatusBar*。1.0.0 / 1.0.1 hook 的
//      _UIStatusBarItemView 在 iOS 17 上根本不会被实例化 → 没有触发点
//      → 插件等价于没装（日志里只有一行「已加载」，之后再无任何记录）。
//      正确入口：
//        STUIStatusBar                      _updateWithAggregatedData:
//                                           _updateWithData:completionHandler:
//                                           → 本轮状态数据，vpnEntry.enabled
//        STUIStatusBarStringView            applyStyleAttributes: / setText:
//                                           → 「4G / 5G / LTE」那些字
//        STUIStatusBarSignalView 及其子类    setActiveColor: / setInactiveColor:
//                                           → Wi-Fi 图标与蜂窝信号格
//
//   ② 染色方式选错了（致命）。
//      iOS 17 的信号视图是自绘的，颜色来自 activeColor / inactiveColor 两个
//      属性。写 tintColor 对它们完全无效 —— 旧版对信号视图设 tintColor 和
//      子视图 backgroundColor，等于什么都没做。
//
//   另有三条来自实机验证的机制（照 SingleVPN 2.2 的结论，缺一不可）：
//
//   ③ 启动闸门（500ms）。
//      respring 后主屏状态栏实例在启动瞬间建成，此刻介入会把它建成残缺状态，
//      且 iOS 17 之后不会重建 → 图标整块空白。闸门内只记录视图、不写颜色。
//
//   ④ 断开后必须主动写回原色。
//      染色是「一次性下发」：断开 VPN 不改变信号格数 / 强度 / 网络类型文字，
//      系统不会重发样式 → 上一轮染的橙色会一直留在屏幕上。
//
//   ⑤ 未激活色必须保留原 alpha。
//      colorWithAlphaComponent: 是把 alpha「替换」成给定值，不是叠加。系统给
//      未激活信号格的 alpha 接近 1，被压到 0.2 后：深色壁纸 → 近乎全黑，
//      浅色壁纸 → 近乎全白，两种背景下都「看不见」，表现为图标消失。
//      正确做法是只换 RGB，alpha 沿用原色。
//
//   ⑥ 1.0.2 装完直接进安全模式（SpringBoard 崩），根因在 hook 框架本身：
//      · 同一个 repl 装到「基类 + 全部子类」，而原 IMP 表按
//        object_getClass(self) 查 —— 子类 override 调 [super setActiveColor:] 时
//        self 仍是子类实例，会查回子类自己的 orig，无限递归直到栈溢出。
//        @try/@catch 拦不住 SIGSEGV。
//      · objc_copyClassList 全量遍历、对大量私有类调 class_getInstanceMethod，
//        在 constructor 阶段可能触发 +initialize，宿主尚未就绪就会崩。
//      1.0.3：每个 (类,SEL) 用独立 trampoline 精确绑定自己的 orig（绝不按 self
//      查表）、只 hook 具体类不 hook 基类、精确 objc_getClass 不做全量遍历、
//      初始化延迟到主队列、再加一层递归深度保险。
//
// 工程约束：
//   · 不用 Logos，入口是 __attribute__((constructor))，全部 hook 走 runtime。
//     这样每个 hook 点都有 NULL 检查与返回值，不会因为某个类在某个系统版本
//     上不存在而整组静默失败。
//   · 热路径（applyStyleAttributes: 等）零 NSString 分配、零 IO、零日志。
//     原 IMP 按槽位直取，绝不做按 self 的继承链查找（那是 1.0.2 的递归根因）。
//   · 包内不含任何 maintainer script（历史事故：postinst 里重启 SpringBoard
//     打断 dpkg 配置流程 → Sileo 报「dpkg 被中断」）。
// ============================================================================

#import <UIKit/UIKit.h>
#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <dlfcn.h>
#import <string.h>
#import <fcntl.h>
#import <unistd.h>

extern CFDictionaryRef CFNetworkCopySystemProxySettings(void);

#define VT_VERSION   "1.0.3"
#define VT_PREF_PATH "/var/mobile/Library/Preferences/com.liehuu.vpntint.plist"
#define VT_READY_MS  500

// ============================================================================
// 私有接口声明
//
// 只用于「让编译器知道有哪些属性/方法」，不做 KVC 动态调用。
// iOS 17（StatusStatusUI.framework）与 iOS 16 及更早（UIKitCore）两套并存，
// 声明本身不引用任何符号，运行时按类是否存在决定装哪一组。
// ============================================================================

#pragma mark - iOS 17：StatusStatusUI

@interface STStatusBarDataEntry : NSObject
@property (getter=isEnabled, nonatomic, readonly) bool enabled;
@end

@interface STStatusBarDataCellularEntry : STStatusBarDataEntry
@end

@interface STStatusBarDataWifiEntry : STStatusBarDataEntry
@end

@interface STStatusBarData : NSObject
@property (nonatomic, readonly) STStatusBarDataCellularEntry *cellularEntry;
@property (nonatomic, readonly) STStatusBarDataCellularEntry *secondaryCellularEntry;
@property (nonatomic, readonly) STStatusBarDataEntry *vpnEntry;
@property (nonatomic, readonly) STStatusBarDataWifiEntry *wifiEntry;
@end

@interface STUIStatusBar : NSObject
- (STStatusBarData *)currentAggregatedData;
- (STStatusBarData *)currentData;
@end

@interface STUIStatusBarStyleAttributes : NSObject <NSCopying>
@property (nonatomic, copy) UIColor *textColor;
@property (nonatomic, copy) UIColor *imageTintColor;
@property (nonatomic, copy) UIColor *imageDimmedTintColor;
@end

@interface STUIStatusBarSignalView : UIView
@property (nonatomic, copy) UIColor *activeColor;
@property (nonatomic, copy) UIColor *inactiveColor;
@end

#pragma mark - iOS 16 及更早：UIKitCore

@interface _UIStatusBarStyleAttributes : NSObject <NSCopying>
@property (nonatomic, copy) UIColor *textColor;
@property (nonatomic, copy) UIColor *imageTintColor;
@end

@interface _UIStatusBarSignalView : UIView
@property (nonatomic, copy) UIColor *activeColor;
@property (nonatomic, copy) UIColor *inactiveColor;
@end

// ============================================================================
// 配置
// ============================================================================

static BOOL      gEnabled      = YES;   // 总开关
static BOOL      gTintBars     = YES;   // Wi-Fi / 蜂窝信号格是否也染
static BOOL      gTintTypeText = YES;   // 4G / 5G 文字是否也染
static BOOL      gCFFallback   = YES;   // 状态栏数据拿不到时用系统代理设置兜底
static CGFloat   gPollInterval = 1.0;   // 兜底轮询间隔（秒）
static UIColor  *gTintColor    = nil;   // 目标颜色
static NSArray  *gIgnoreIfaces = nil;   // 不计入 VPN 的接口名前缀（例如 utun0）
// 渐进开关（诊断用）：0=完全不装 hook 只写日志 / 1=仅数据层 / 2=+文字层 / 3=全部（默认）
// 万一又进安全模式，把 HookLevel 设 0，插件就不碰任何系统方法，手机必定能正常开机；
// 再逐级往上调，配合日志定位是哪一层出的问题，不用反复卸载重装。
static int       gHookLevel    = 3;

static UIColor *VTColorFromHex(NSString *hex) {
    if (![hex isKindOfClass:[NSString class]]) return nil;
    NSString *s = [[hex stringByReplacingOccurrencesOfString:@"#" withString:@""] uppercaseString];
    if (s.length != 6 && s.length != 8) return nil;
    unsigned int v = 0;
    if (![[NSScanner scannerWithString:s] scanHexInt:&v]) return nil;
    if (s.length == 6) {
        return [UIColor colorWithRed:((v >> 16) & 0xFF) / 255.0
                               green:((v >> 8) & 0xFF) / 255.0
                                blue:(v & 0xFF) / 255.0
                               alpha:1.0];
    }
    return [UIColor colorWithRed:((v >> 24) & 0xFF) / 255.0
                           green:((v >> 16) & 0xFF) / 255.0
                            blue:((v >> 8) & 0xFF) / 255.0
                           alpha:(v & 0xFF) / 255.0];
}

static void VTLoadPrefs(void) {
    gEnabled      = YES;
    gHookLevel    = 3;
    gTintBars     = YES;
    gTintTypeText = YES;
    gCFFallback   = YES;
    gPollInterval = 1.0;
    gTintColor    = [UIColor systemOrangeColor];
    gIgnoreIfaces = @[];

    NSDictionary *d = [NSDictionary dictionaryWithContentsOfFile:@VT_PREF_PATH];
    if (![d isKindOfClass:[NSDictionary class]]) return;

    if (d[@"Enabled"])        gEnabled      = [d[@"Enabled"] boolValue];
    if (d[@"HookLevel"]) {
        int lv = [d[@"HookLevel"] intValue];
        if (lv >= 0 && lv <= 3) gHookLevel = lv;
    }
    if (d[@"TintSignalBars"]) gTintBars     = [d[@"TintSignalBars"] boolValue];
    if (d[@"TintTypeText"])   gTintTypeText = [d[@"TintTypeText"] boolValue];
    if (d[@"CFFallback"])     gCFFallback   = [d[@"CFFallback"] boolValue];
    if (d[@"PollInterval"]) {
        CGFloat v = [d[@"PollInterval"] doubleValue];
        if (v >= 0.3) gPollInterval = v;
    }
    // ★ 颜色必须兜底：为 nil 会让染色路径走进「写 nil」的分支，等于把图标清空。
    UIColor *c = VTColorFromHex(d[@"ColorHex"]);
    if (c) gTintColor = c;
    if ([d[@"IgnoreInterfaces"] isKindOfClass:[NSArray class]]) gIgnoreIfaces = d[@"IgnoreInterfaces"];
}

// ============================================================================
// 日志
//
// 只在「启动 / 状态切换 / 首次上色 / 异常」写，热路径不写。
// 用户判断「没生效」与「崩了」全靠这个文件，所以启动行必须把每个 hook 点的
// 安装结果都打出来。
// ============================================================================

static NSString *gLogPath = nil;

static void VTLogInit(void) {
    if (gLogPath) return;
    NSArray *cands = @[@"/var/mobile/Library/Logs/VpnTint.log", @"/tmp/VpnTint.log"];
    NSFileManager *fm = [NSFileManager defaultManager];
    for (NSString *p in cands) {
        @try {
            [fm createDirectoryAtPath:[p stringByDeletingLastPathComponent]
          withIntermediateDirectories:YES attributes:nil error:NULL];
            if (![fm fileExistsAtPath:p]) {
                [fm createFileAtPath:p contents:[NSData data] attributes:nil];
            }
            NSDictionary *attr = [fm attributesOfItemAtPath:p error:NULL];
            if ([attr[NSFileSize] unsignedLongLongValue] > 128 * 1024) {
                [fm removeItemAtPath:p error:NULL];
                [fm createFileAtPath:p contents:[NSData data] attributes:nil];
            }
            if ([fm fileExistsAtPath:p]) { gLogPath = p; break; }
        } @catch (NSException *e) { continue; }
    }
}

static void VTLog(NSString *fmt, ...) {
    @try {
        VTLogInit();
        if (!gLogPath) return;
        va_list ap;
        va_start(ap, fmt);
        NSString *msg = [[NSString alloc] initWithFormat:fmt arguments:ap];
        va_end(ap);
        NSDateFormatter *df = [[NSDateFormatter alloc] init];
        df.dateFormat = @"MM-dd HH:mm:ss";
        NSString *line = [NSString stringWithFormat:@"[%@] %@\n", [df stringFromDate:[NSDate date]], msg];
        NSFileHandle *fh = [NSFileHandle fileHandleForWritingAtPath:gLogPath];
        [fh seekToEndOfFile];
        [fh writeData:[line dataUsingEncoding:NSUTF8StringEncoding]];
        [fh closeFile];
    } @catch (NSException *e) { }
}

// ============================================================================
// 颜色工具
// ============================================================================

/// 换 RGB、**alpha 沿用 original 的**。见文件头 ⑤。
/// 三个边界都按「宁可保持原样，不要写一个看不见的色」处理。
static UIColor *VTKeepAlpha(UIColor *original, UIColor *hue) {
    if (!hue) return original;
    if (!original) return hue;
    CGFloat sr = 0, sg = 0, sb = 0, sa = 0;
    CGFloat hr = 0, hg = 0, hb = 0, ha = 0;
    if (![original getRed:&sr green:&sg blue:&sb alpha:&sa]) return hue;  // 动态色等：用纯色相
    if (![hue getRed:&hr green:&hg blue:&hb alpha:&ha]) return hue;
    if (sa <= 0.001) return hue;
    return [UIColor colorWithRed:hr green:hg blue:hb alpha:sa];
}

/// 网络类型文字白名单。只有这些文本会被染色，运营商名 / 通知等不动。
static BOOL VTIsNetworkTypeText(NSString *t) {
    if (![t isKindOfClass:[NSString class]] || t.length == 0 || t.length > 8) return NO;
    if ([t isEqualToString:@"G"] || [t isEqualToString:@"3G"] ||
        [t isEqualToString:@"4G"] || [t isEqualToString:@"4G+"] ||
        [t isEqualToString:@"LTE"] || [t isEqualToString:@"NR"] ||
        [t isEqualToString:@"E"] || [t isEqualToString:@"1X"]) return YES;
    if ([t hasPrefix:@"5G"]) return YES;   // 5G / 5G+ / 5G UW / 5G E
    return NO;
}

// ============================================================================
// VPN 状态判定（双源）
//
// 主源：状态栏数据里的 vpnEntry.enabled —— 系统自己算好的，最准。
// 兜底：CFNetworkCopySystemProxySettings 的 __SCOPED__ 里出现隧道接口。
//       只在主源从未拿到过数据时才启用（例如 hook 目标又改名了），
//       由 1s 定时器刷新到 gVPNFromCF，热路径只读布尔。
// ============================================================================

static BOOL gVPNFromData = NO;   // 主源：本轮状态栏数据
static BOOL gVPNFromCF   = NO;   // 兜底：系统代理设置
static int  gDataHits    = 0;    // 状态栏数据 hook 命中次数

static BOOL VTDetectVPNByCFNetwork(void) {
    BOOL active = NO;
    CFDictionaryRef proxy = CFNetworkCopySystemProxySettings();
    if (!proxy) return NO;
    NSDictionary *d = (__bridge_transfer NSDictionary *)proxy;
    if (![d isKindOfClass:[NSDictionary class]]) return NO;
    id scoped = d[@"__SCOPED__"];
    if (![scoped isKindOfClass:[NSDictionary class]]) return NO;

    static NSArray *prefixes = nil;
    if (!prefixes) prefixes = @[@"utun", @"ipsec", @"ppp", @"tun", @"tap", @"wg"];

    for (id key in [(NSDictionary *)scoped allKeys]) {
        if (![key isKindOfClass:[NSString class]]) continue;
        NSString *k = [(NSString *)key lowercaseString];
        BOOL skip = NO;
        for (NSString *ig in gIgnoreIfaces) {
            if ([ig isKindOfClass:[NSString class]] && [k hasPrefix:ig.lowercaseString]) { skip = YES; break; }
        }
        if (skip) continue;
        for (NSString *p in prefixes) {
            if ([k hasPrefix:p]) { active = YES; break; }
        }
        if (active) break;
    }
    return active;
}

static BOOL VTIsVPNOn(void) {
    if (gVPNFromData) return YES;
    if (gCFFallback && gVPNFromCF) return YES;
    return NO;
}

// ============================================================================
// 状态机（全局状态先声明，账本段的还原逻辑要用到闸门）
// ============================================================================

static volatile int gReady = 0;   // 启动闸门，见文件头 ③
static int  gLastDecision = -1;   // -1 未知 / 0 不染 / 1 染
static int  gTintHits = 0;        // 首次上色日志只打一次

static NSHashTable *gSeen = nil;  // 闸门期间见过的视图，开闸后补染

static void VTRemember(UIView *v) {
    if (!v) return;
    if (!gSeen) gSeen = [NSHashTable weakObjectsHashTable];
    [gSeen addObject:v];
}

// ============================================================================
// 还原账本
//
// 一视图一字典，键是槽位名，同一字典放三样：
//   @"tc"/@"ac"/@"ic"     → 系统原本的颜色
//   @"tc#"/@"ac#"/@"ic#"  → 我们自己染上去的（残留识别用）
//   @"@style"             → 记账时的界面外观
//
// 四个必须遵守的点（每条对应一次踩坑）：
//   ① 残留识别：setText: 路径读到的 self.textColor 可能已被我们染过。
//      若读到的色等于「我上次染的这个值」，它就不是系统色，拒绝记账。
//      不判这条，第二次调用会把橙色当系统色存下来，还原时还原成橙色。
//   ② 逐视图比对外观：系统色随深浅色变化。记账时的外观与还原时不同 → 这批
//      色已过期，写回去就是错色。只跳过该视图，不清表。
//   ③ 写回前实色 + 幂等：只写能取到 RGBA 的实色；透明色不写；当前值已相同
//      就不写（我们是在系统的更新流程之外改私有属性）。
//   ④ 处理完销账：账本只在一个「染色 → 断开」周期内有效。
// ============================================================================

#define VT_RESTORE_MAX_VIEWS 48
#define VT_STYLE_KEY @"@style"

static NSMapTable *gLedger = nil;

/// 当前界面外观：1 深色 / 0 浅色 / -1 拿不到。
static int VTUserInterfaceStyle(BOOL forceRefresh) {
    static int cached = -2;
    static CFAbsoluteTime cachedAt = 0;
    CFAbsoluteTime now = CFAbsoluteTimeGetCurrent();
    if (!forceRefresh && cached != -2 && (now - cachedAt) < 5.0) return cached;

    int v = -1;
    @try {
        UITraitCollection *tc = nil;
        @try { tc = [UITraitCollection currentTraitCollection]; } @catch (NSException *e) {}
        if (tc) v = (tc.userInterfaceStyle == UIUserInterfaceStyleDark) ? 1 : 0;
    } @catch (NSException *e) {
        v = -1;
    }
    if (v >= 0) { cached = v; cachedAt = now; }
    return v;
}

/// ★ key 必须 WeakMemory + ObjectPointerPersonality：
///   只写 weakToStrongObjectsMapTable 会退回默认 personality，视图类一旦重写
///   isEqual 就可能把两个视图并成一条 → 还原串味。
static void VTEnsureLedger(void) {
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        NSPointerFunctionsOptions kOpt =
            NSPointerFunctionsWeakMemory | NSPointerFunctionsObjectPointerPersonality;
        NSPointerFunctionsOptions vOpt =
            NSPointerFunctionsStrongMemory | NSPointerFunctionsObjectPointerPersonality;
        gLedger = [NSMapTable mapTableWithKeyOptions:kOpt valueOptions:vOpt];
    });
}

static NSMutableDictionary *VTLedgerDict(id view, BOOL create) {
    if (!view) return nil;
    VTEnsureLedger();
    NSMutableDictionary *d = nil;
    @try {
        d = [gLedger objectForKey:view];
        if (!d && create) {
            d = [NSMutableDictionary dictionaryWithCapacity:4];
            [gLedger setObject:d forKey:view];
        }
    } @catch (NSException *e) { return nil; }
    return d;
}

static void VTRecord(id view, NSString *slot, UIColor *systemColor, UIColor *paintedColor) {
    if (!view || !slot || !systemColor) return;
    @try {
        NSMutableDictionary *d = VTLedgerDict(view, YES);
        if (!d) return;
        // ① 残留识别
        NSString *paintedKey = [slot stringByAppendingString:@"#"];
        UIColor *lastPainted = [d objectForKey:paintedKey];
        if (lastPainted && [lastPainted isEqual:systemColor]) return;
        [d setObject:systemColor forKey:slot];
        if (paintedColor) [d setObject:paintedColor forKey:paintedKey];
        int style = VTUserInterfaceStyle(NO);
        if (style >= 0) [d setObject:@(style) forKey:VT_STYLE_KEY];
    } @catch (NSException *e) {
        // 记账失败最多是这次还原不全，绝不影响宿主
    }
}

/// 写一个色回视图的某个槽位。任意一条检查不过就不写。
static void VTRestoreSlot(id view, NSString *setter, NSString *getter, UIColor *color) {
    if (!view || !setter || !color) return;
    @try {
        SEL sel = NSSelectorFromString(setter);
        if (!sel || ![view respondsToSelector:sel]) return;
        CGFloat r = 0, g = 0, b = 0, a = 0;
        if (![color getRed:&r green:&g blue:&b alpha:&a]) return;   // 非实色
        if (a <= 0.001) return;                                     // 透明色写上去就是看不见
        if (getter) {
            SEL gsel = NSSelectorFromString(getter);
            if (gsel && [view respondsToSelector:gsel]) {
                @try {
                    IMP gimp = [view methodForSelector:gsel];
                    if (gimp) {
                        // __unsafe_unretained：ARC 不知道函数指针返回对象的所有权，
                        // 默认按 +1 处理会误 release 别人的对象 → 崩宿主。
                        __unsafe_unretained id cur = ((id (*)(id, SEL))gimp)(view, gsel);
                        if ([cur isKindOfClass:[UIColor class]] && [cur isEqual:color]) return;
                    }
                } @catch (NSException *e) {}
            }
        }
        ((void (*)(id, SEL, id))objc_msgSend)(view, sel, color);
    } @catch (NSException *e) {}
}

static void VTRestoreNow(void) {
    if (!gReady) return;             // 闸门未开不动作
    if (VTIsVPNOn()) return;         // 延后执行期间可能已重连，此时擦色就是新 bug
    if (!gLedger) return;

    int styleNow = VTUserInterfaceStyle(YES);

    // 先收集再处理：遍历 NSMapTable 的同时改它属于未定义行为
    NSMutableArray *views = [NSMutableArray array];
    @try {
        for (id v in gLedger) {
            if (v) [views addObject:v];
            if ([views count] >= VT_RESTORE_MAX_VIEWS) break;
        }
    } @catch (NSException *e) { return; }

    // 槽位 → setter / getter。只碰这三个：来源明确、有对应视图 setter。
    // tint 那一路不要碰：setTintColor: 是 UIView 通用属性，会传播到子视图。
    NSArray *plan = @[
        @[@"tc", @"setTextColor:", @"textColor"],
        @[@"ac", @"setActiveColor:", @"activeColor"],
        @[@"ic", @"setInactiveColor:", @"inactiveColor"],
    ];

    for (id view in views) {
        NSMutableDictionary *d = VTLedgerDict(view, NO);
        if (!d) continue;
        // ② 逐视图外观比对：过期就跳过（不清表）
        BOOL usable = YES;
        @try {
            NSNumber *rec = [d objectForKey:VT_STYLE_KEY];
            if (styleNow >= 0 && rec && [rec intValue] != styleNow) usable = NO;
        } @catch (NSException *e) { usable = NO; }
        if (usable) {
            for (NSArray *pair in plan) {
                UIColor *c = nil;
                @try { c = [d objectForKey:pair[0]]; } @catch (NSException *e) { c = nil; }
                if (c) VTRestoreSlot(view, pair[1], pair[2], c);
            }
        }
        // ④ 销账
        @try { [gLedger removeObjectForKey:view]; } @catch (NSException *e) {}
    }
}

// ============================================================================
// 状态判定入口
// ============================================================================

static BOOL VTShouldTint(void) {
    if (!gReady) return NO;
    if (!gEnabled) return NO;
    return VTIsVPNOn();
}

static void VTRestoreAsync(void) {
    dispatch_async(dispatch_get_main_queue(), ^{
        @try { VTRestoreNow(); } @catch (NSException *e) {}
    });
}

/// 每个数据层 hook 末尾调一次；定时器也调。天然去重（只在 1→0 跳变时动作）。
static void VTNoteTransition(void) {
    if (!gReady) return;
    BOOL now = VTIsVPNOn();
    int prev = gLastDecision;
    gLastDecision = now ? 1 : 0;
    if (prev == 1 && !now) {
        VTLog(@"VPN 断开 → 还原原色");
        VTRestoreAsync();
    }
}

// ============================================================================
// hook 框架（1.0.3 重写，修「装上就进安全模式」）
//
// 1.0.2 的致命缺陷：同一个 repl 被装到「基类 + 全部子类」，而链式调用按
//   实例的实际类沿继承链查原 IMP。子类 override 调 [super setActiveColor:]
//   时 self 仍是子类实例，查回的是子类自己的 orig -> 无限递归 -> 栈溢出
//   -> SpringBoard 崩 -> 安全模式。@try/@catch 拦不住 SIGSEGV。
//
// 修法（对齐 Logos %orig 的语义）：每个 (类, SEL) 分配一个独立 trampoline，
// 槽位号编译期写死，原 IMP 从该槽位直接取，绝不按 self 的类查表。super 调用
// 命中的是父类自己的 trampoline（另一个槽位），取的是父类的 orig，不会绕回子类。
//
// 另外三道保险：
//   - __thread 递归深度：任何没预料到的重入都直接透传原实现，不进染色逻辑。
//   - 只 hook 精确命名的具体类，不做全量类表遍历（启动早期遍历私有类会触发
//     大量 +initialize，宿主尚未就绪时极易崩）。
//   - 整个初始化延迟到主队列，避开 dyld 装载镜像的 constructor 阶段。
// ============================================================================

#define VT_MAX_HOOKS       24
#define VT_MAX_CHAIN_DEPTH 4

typedef enum {
    VTKindDataAggregated = 1,
    VTKindDataCompletion = 2,
    VTKindStringStyle    = 3,
    VTKindStringSetText  = 4,
    VTKindSignalActive   = 5,
    VTKindSignalInactive = 6,
    VTKindSignalStyle    = 7,
} VTHookKind;

typedef struct {
    Class       cls;
    SEL         sel;
    IMP         orig;     // 精确绑定：记录阶段该方法的系统原始实现
    const char *types;    // 记录阶段取的类型编码，提交阶段不再查 runtime
    VTHookKind  kind;
    BOOL        twoArgs;
} VTHookRec;

static VTHookRec gHooks[VT_MAX_HOOKS];
static int       gHookCount = 0;
// 不用 __thread / TLS：越狱 dylib 里 TLS 变量依赖 dyld 的 TLV 支持，加载期就有风险。
// 状态栏更新全在主线程，普通 volatile 计数足够；跨线程最坏只是少染一次色，不会崩。
static volatile int gChainDepth = 0;

static void VTInvoke(int idx, id self, SEL _cmd, id a1, id a2);
static void VTCaptureVPNFromData(id data);

/// 每个槽位一个独立 C 函数：槽位号写死在函数名里，原 IMP 不按 self 查找。
#define VT_TRAMP(i) \
static void VTTramp1_##i(id self, SEL _cmd, id a1) { VTInvoke(i, self, _cmd, a1, nil); } \
static void VTTramp2_##i(id self, SEL _cmd, id a1, id a2) { VTInvoke(i, self, _cmd, a1, a2); }

VT_TRAMP(0)  VT_TRAMP(1)  VT_TRAMP(2)  VT_TRAMP(3)  VT_TRAMP(4)  VT_TRAMP(5)
VT_TRAMP(6)  VT_TRAMP(7)  VT_TRAMP(8)  VT_TRAMP(9)  VT_TRAMP(10) VT_TRAMP(11)
VT_TRAMP(12) VT_TRAMP(13) VT_TRAMP(14) VT_TRAMP(15) VT_TRAMP(16) VT_TRAMP(17)
VT_TRAMP(18) VT_TRAMP(19) VT_TRAMP(20) VT_TRAMP(21) VT_TRAMP(22) VT_TRAMP(23)

#undef VT_TRAMP

static IMP VTTrampolineFor(int idx, BOOL twoArgs) {
#define VT_CASE(i) case i: return twoArgs ? (IMP)VTTramp2_##i : (IMP)VTTramp1_##i;
    switch (idx) {
        VT_CASE(0)  VT_CASE(1)  VT_CASE(2)  VT_CASE(3)  VT_CASE(4)  VT_CASE(5)
        VT_CASE(6)  VT_CASE(7)  VT_CASE(8)  VT_CASE(9)  VT_CASE(10) VT_CASE(11)
        VT_CASE(12) VT_CASE(13) VT_CASE(14) VT_CASE(15) VT_CASE(16) VT_CASE(17)
        VT_CASE(18) VT_CASE(19) VT_CASE(20) VT_CASE(21) VT_CASE(22) VT_CASE(23)
        default: return NULL;
    }
#undef VT_CASE
}

/// 用槽位绑定的原 IMP 调回系统实现。绝不按 object_getClass(self) 查表。
static void VTCallOrig(VTHookRec *r, id self, SEL _cmd, id a1, id a2) {
    IMP imp = r ? r->orig : NULL;
    if (!imp) return;
    if (r->twoArgs) ((void (*)(id, SEL, id, id))imp)(self, _cmd, a1, a2);
    else            ((void (*)(id, SEL, id))imp)(self, _cmd, a1);
}

/// 只「预约」一个 hook：记录系统原始 IMP 与类型编码，**不改任何方法**。
///
/// 关键：所有槽位的 orig 必须在任何 IMP 被替换之前全部记录完。若边记录边
/// 替换，而两个被 hook 的类恰好有继承关系，后装的那个类用
/// class_getInstanceMethod 会取到前一个已被换掉的 trampoline，orig 就指向
/// 我们自己的 hook —— 轻则双重染色，重则又变成递归。分两阶段可结构性排除。
static BOOL VTHookMethod(Class c, SEL sel, VTHookKind kind, BOOL twoArgs) {
    if (!c || !sel) return NO;
    Method m = class_getInstanceMethod(c, sel);
    if (!m) return NO;
    IMP eff = method_getImplementation(m);
    if (!eff) return NO;
    if (gHookCount >= VT_MAX_HOOKS) return NO;
    for (int i = 0; i < gHookCount; i++) {
        if (gHooks[i].cls == c && sel_isEqual(gHooks[i].sel, sel)) return NO;
    }
    IMP repl = VTTrampolineFor(gHookCount, twoArgs);
    if (!repl) return NO;
    gHooks[gHookCount].cls     = c;
    gHooks[gHookCount].sel     = sel;
    gHooks[gHookCount].orig    = eff;
    gHooks[gHookCount].types   = method_getTypeEncoding(m);
    gHooks[gHookCount].kind    = kind;
    gHooks[gHookCount].twoArgs = twoArgs;
    gHookCount++;
    return YES;
}

/// 统一提交：此时 gHooks[] 里的 orig 全部是未受污染的系统实现。
static void VTCommitHooks(void) {
    for (int i = 0; i < gHookCount; i++) {
        VTHookRec *r = &gHooks[i];
        IMP repl = VTTrampolineFor(i, r->twoArgs);
        if (!repl || !r->cls || !r->sel || !r->types || !r->orig) continue;
        class_replaceMethod(r->cls, r->sel, repl, r->types);
    }
}

/// 动态 SEL（避免 undeclared selector 在 -Werror 下失败）
static SEL VTSelApplyStyle(void)  { return NSSelectorFromString(@"applyStyleAttributes:"); }
static SEL VTSelSetText(void)     { return NSSelectorFromString(@"setText:"); }
static SEL VTSelSetActive(void)   { return NSSelectorFromString(@"setActiveColor:"); }
static SEL VTSelSetInactive(void) { return NSSelectorFromString(@"setInactiveColor:"); }

// 染色逻辑：只算「要传给原实现的入参」，不负责调回原实现。
static id   VTPrepareStringStyle(id self, id attrs);
static id   VTPrepareSignalColor(id self, id color, BOOL keepAlpha);
static id   VTPrepareSignalStyle(id attrs);
static void VTPostSetText(id self, id text);

static void VTInvoke(int idx, id self, SEL _cmd, id a1, id a2) {
    if (idx < 0 || idx >= gHookCount) return;
    VTHookRec *r = &gHooks[idx];
    if (!r->orig) return;

    // 最后一道保险：任何没预料到的重入都只透传，绝不进染色逻辑。
    if (gChainDepth >= VT_MAX_CHAIN_DEPTH) {
        VTCallOrig(r, self, _cmd, a1, a2);
        return;
    }
    gChainDepth++;
    id pass = a1;
    BOOL postText = NO;
    @try {
        switch (r->kind) {
            case VTKindDataAggregated:
            case VTKindDataCompletion:
                VTCaptureVPNFromData(a1);
                VTNoteTransition();
                break;
            case VTKindStringStyle:    pass = VTPrepareStringStyle(self, a1); break;
            case VTKindStringSetText:  postText = YES; break;   // 必须先调 orig
            case VTKindSignalActive:   pass = VTPrepareSignalColor(self, a1, NO);  break;
            case VTKindSignalInactive: pass = VTPrepareSignalColor(self, a1, YES); break;
            case VTKindSignalStyle:    pass = VTPrepareSignalStyle(a1); break;
            default: break;
        }
    } @catch (NSException *e) {
        // 算入参出错 → 原样透传。绝不能因为染色逻辑异常就把系统这次状态栏
        // 更新整个吞掉：旧写法把 VTCallOrig 放在 try 里，异常一抛状态栏就卡住。
        pass = a1;
        postText = NO;
    }
    @try { VTCallOrig(r, self, _cmd, pass, a2); }
    @catch (NSException *e) {}
    if (postText) {
        @try { VTPostSetText(self, a1); } @catch (NSException *e) {}
    }
    gChainDepth--;
}

// ============================================================================
// 染色逻辑（1.0.3）
//
// 这里的函数只计算「要传给系统原实现的入参」，不负责调回原实现。
// 调回统一由 VTInvoke 里的 VTCallOrig 完成 —— 每个 hook 点必然且只调一次。
// ============================================================================

#pragma mark - 数据层：VPN 状态

static void VTCaptureVPNFromData(id data) {
    if (!data) return;
    @try {
        STStatusBarData *d = (STStatusBarData *)data;
        if (![d respondsToSelector:@selector(vpnEntry)]) return;
        STStatusBarDataEntry *e = [d vpnEntry];
        if (!e) return;
        BOOL on = [e isEnabled];
        gDataHits++;
        if (gDataHits == 1) VTLog(@"状态栏数据 hook 命中（vpnEntry=%d）", on ? 1 : 0);
        gVPNFromData = on;
    } @catch (NSException *e) {}
}

#pragma mark - 文字层：4G / 5G

static id VTPrepareStringStyle(id self, id attrs) {
    id pass = attrs;
    VTRemember((UIView *)self);
    if (!VTShouldTint() || !gTintTypeText || !attrs) return pass;
    @try {
        NSString *t = [(UILabel *)self text];
        if (!VTIsNetworkTypeText(t)) return pass;
        STUIStatusBarStyleAttributes *sa = (STUIStatusBarStyleAttributes *)attrs;
        UIColor *sys = nil;
        if ([sa respondsToSelector:@selector(textColor)]) sys = [sa textColor];
        UIColor *paint = gTintColor;
        if (!sys || !paint) return pass;
        id copy = [sa copy];
        if (!copy) return pass;
        [(STUIStatusBarStyleAttributes *)copy setTextColor:paint];
        VTRecord(self, @"tc", sys, paint);
        if (gTintHits++ == 0) {
            VTLog(@"首次上色（文字）：%s text=%@", class_getName(object_getClass(self)), t);
        }
        return copy;
    } @catch (NSException *e) { return pass; }
}

static void VTPostSetText(id self, id text) {
    if (VTIsNetworkTypeText(text)) VTRemember((UIView *)self);
    if (!VTShouldTint() || !gTintTypeText) return;
    if (!VTIsNetworkTypeText(text)) return;
    @try {
        UIColor *cur = [(UILabel *)self textColor];
        UIColor *paint = gTintColor;
        if (paint && ![cur isEqual:paint]) {
            VTRecord(self, @"tc", cur, paint);
            [(UILabel *)self setTextColor:paint];
            if (gTintHits++ == 0) {
                VTLog(@"首次上色（setText）：%s text=%@", class_getName(object_getClass(self)), text);
            }
        }
    } @catch (NSException *e) {}
}

#pragma mark - 图标层：Wi-Fi / 蜂窝信号

static id VTPrepareSignalColor(id self, id color, BOOL keepAlpha) {
    id pass = color;
    VTRemember((UIView *)self);
    if (!VTShouldTint() || !gTintBars || !color) return pass;
    @try {
        UIColor *orig = (UIColor *)color;
        UIColor *paint = keepAlpha ? VTKeepAlpha(orig, gTintColor) : gTintColor;
        if (!paint) return pass;
        VTRecord(self, keepAlpha ? @"ic" : @"ac", orig, paint);
        if (gTintHits++ == 0) {
            VTLog(@"首次上色（%s）：%s", keepAlpha ? "inactive" : "active",
                  class_getName(object_getClass(self)));
        }
        return paint;
    } @catch (NSException *e) { return pass; }
}

static id VTPrepareSignalStyle(id attrs) {
    id pass = attrs;
    if (!VTShouldTint() || !gTintBars || !attrs) return pass;
    @try {
        STUIStatusBarStyleAttributes *sa = (STUIStatusBarStyleAttributes *)attrs;
        UIColor *sys = nil;
        UIColor *sysDim = nil;
        if ([sa respondsToSelector:@selector(imageTintColor)]) sys = [sa imageTintColor];
        if (!sys && [sa respondsToSelector:@selector(textColor)]) sys = [sa textColor];
        if ([sa respondsToSelector:@selector(imageDimmedTintColor)]) sysDim = [sa imageDimmedTintColor];
        if (!sys || !gTintColor) return pass;
        id copy = [sa copy];
        if (!copy) return pass;
        if ([copy respondsToSelector:@selector(setImageTintColor:)]) {
            [(STUIStatusBarStyleAttributes *)copy setImageTintColor:gTintColor];
        }
        UIColor *dimmed = VTKeepAlpha(sysDim ?: sys, gTintColor);
        if (dimmed && [copy respondsToSelector:@selector(setImageDimmedTintColor:)]) {
            [(STUIStatusBarStyleAttributes *)copy setImageDimmedTintColor:dimmed];
        }
        return copy;
    } @catch (NSException *e) { return pass; }
}

#pragma mark - 闸门打开后补染

/// 把闸门期间记下的视图按当前状态染一次（只走属性 setter，不碰层级）。
static void VTRepaintSeen(void) {
    if (!gSeen || gSeen.count == 0) return;
    if (!VTShouldTint()) return;
    int painted = 0;
    @try {
        for (UIView *v in gSeen.allObjects) {
            if (!v || !v.superview) continue;
            @try {
                if ([v respondsToSelector:@selector(setActiveColor:)]) {
                    ((void (*)(id, SEL, id))objc_msgSend)(v, @selector(setActiveColor:), gTintColor);
                    painted++;
                }
                if ([v respondsToSelector:@selector(setInactiveColor:)]) {
                    ((void (*)(id, SEL, id))objc_msgSend)(v, @selector(setInactiveColor:), gTintColor);
                }
                if ([v isKindOfClass:[UILabel class]]) {
                    NSString *t = [(UILabel *)v text];
                    if (VTIsNetworkTypeText(t)) {
                        [(UILabel *)v setTextColor:gTintColor];
                        painted++;
                    }
                }
            } @catch (NSException *e) {}
        }
    } @catch (NSException *e) {}
    if (painted) VTLog(@"闸门后补染：%d 个视图", painted);
}

// ============================================================================
// 定时器（兜底）
//
// 正常路径不需要它：数据层 hook 一有变化就直接处理。它的作用只有两个：
//   ① 每 1s 刷新 gVPNFromCF —— 万一状态栏数据 hook 又没命中（私有类再改名），
//      仍能靠系统代理设置判定，并把结果记进日志。
//   ② 兜底还原：连接状态来自 CF 时没有 hook 触发点，断开必须靠这里擦色。
// ============================================================================

static dispatch_source_t gTimer = NULL;
static dispatch_source_t gPrefWatch = NULL;

static void VTStartTimer(void) {
    if (gTimer) return;
    gTimer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_main_queue());
    uint64_t interval = (uint64_t)(gPollInterval * (double)NSEC_PER_SEC);
    if (interval < 500ull * NSEC_PER_MSEC) interval = 500ull * NSEC_PER_MSEC;
    dispatch_source_set_timer(gTimer, dispatch_time(DISPATCH_TIME_NOW, interval), interval,
                              300ull * NSEC_PER_MSEC);
    dispatch_source_set_event_handler(gTimer, ^{
        @try {
            if (!gEnabled) return;

            BOOL cf = gCFFallback ? VTDetectVPNByCFNetwork() : NO;
            if (cf != gVPNFromCF) {
                gVPNFromCF = cf;
                if (gDataHits == 0) {
                    // 数据层没命中，只能靠这里 —— 明确记一条，便于用户判断
                    VTLog(@"VPN(系统代理判定) -> %@", cf ? @"已连接" : @"已断开");
                }
                VTNoteTransition();
                if (cf && gTintHits == 0) VTRepaintSeen();
            }
        } @catch (NSException *e) {}
    });
    dispatch_resume(gTimer);
}

static void VTStartPrefWatch(void) {
    if (gPrefWatch) return;
    int fd = open(VT_PREF_PATH, O_EVTONLY);
    if (fd < 0) return;                        // 没写配置就跳过，不报错
    gPrefWatch = dispatch_source_create(DISPATCH_SOURCE_TYPE_VNODE, (uintptr_t)fd,
                                        DISPATCH_VNODE_WRITE | DISPATCH_VNODE_DELETE | DISPATCH_VNODE_RENAME,
                                        dispatch_get_main_queue());
    dispatch_source_set_event_handler(gPrefWatch, ^{
        @try {
            unsigned long flags = dispatch_source_get_data(gPrefWatch);
            if (flags & (DISPATCH_VNODE_DELETE | DISPATCH_VNODE_RENAME)) {
                dispatch_source_cancel(gPrefWatch);
                gPrefWatch = NULL;
                close(fd);
                dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1 * NSEC_PER_SEC)),
                               dispatch_get_main_queue(), ^{ VTStartPrefWatch(); });
                return;
            }
            VTLoadPrefs();
            VTLog(@"配置已重载：enabled=%d bars=%d text=%d", gEnabled, gTintBars, gTintTypeText);
            if (!gEnabled) { VTRestoreNow(); return; }
            VTRepaintSeen();
        } @catch (NSException *e) {}
    });
    dispatch_resume(gPrefWatch);
}

// ============================================================================
// 入口
// ============================================================================

/// 精确类名安装（1.0.3）。
/// 不再遍历全部类：启动早期对大量私有类调 class_getInstanceMethod 可能触发
/// +initialize；且「基类 + 全部子类同时 hook」正是 1.0.2 递归崩溃的来源。
/// 这里只 hook 参考实现验证过的具体类；具体类一个都取不到时才退到基类，
/// 且基类与具体类不会同时装。

static int VTHookNamedClass(const char *name, VTHookKind kind, SEL sel, BOOL twoArgs) {
    if (!name || !sel) return 0;
    Class c = objc_getClass(name);
    if (!c) return 0;
    return VTHookMethod(c, sel, kind, twoArgs) ? 1 : 0;
}

static int VTInstall17(void) {
    int n = 0;
    if (gHookLevel >= 1) {
        n += VTHookNamedClass("STUIStatusBar", VTKindDataAggregated,
                              NSSelectorFromString(@"_updateWithAggregatedData:"), NO);
        n += VTHookNamedClass("STUIStatusBar", VTKindDataCompletion,
                              NSSelectorFromString(@"_updateWithData:completionHandler:"), YES);
    }
    if (gHookLevel >= 2) {
        n += VTHookNamedClass("STUIStatusBarStringView", VTKindStringStyle, VTSelApplyStyle(), NO);
        n += VTHookNamedClass("STUIStatusBarStringView", VTKindStringSetText, VTSelSetText(), NO);
    }
    if (gHookLevel >= 3) {
        int before = gHookCount;
        n += VTHookNamedClass("STUIStatusBarWifiSignalView", VTKindSignalActive,   VTSelSetActive(),   NO);
        n += VTHookNamedClass("STUIStatusBarWifiSignalView", VTKindSignalInactive, VTSelSetInactive(), NO);
        n += VTHookNamedClass("STUIStatusBarWifiSignalView", VTKindSignalStyle,    VTSelApplyStyle(),  NO);
        n += VTHookNamedClass("STUIStatusBarCellularSignalView", VTKindSignalActive,   VTSelSetActive(),   NO);
        n += VTHookNamedClass("STUIStatusBarCellularSignalView", VTKindSignalInactive, VTSelSetInactive(), NO);
        n += VTHookNamedClass("STUIStatusBarCellularSignalView", VTKindSignalStyle,    VTSelApplyStyle(),  NO);
        if (gHookCount == before) {
            // 具体子类都不存在：只装基类兜底，绝不与子类同时装
            n += VTHookNamedClass("STUIStatusBarSignalView", VTKindSignalActive,   VTSelSetActive(),   NO);
            n += VTHookNamedClass("STUIStatusBarSignalView", VTKindSignalInactive, VTSelSetInactive(), NO);
            n += VTHookNamedClass("STUIStatusBarSignalView", VTKindSignalStyle,    VTSelApplyStyle(),  NO);
        }
    }
    VTLog(@"iOS17 hook：level=%d 已装=%d 条", gHookLevel, gHookCount);
    return n;
}

static int VTInstall16(void) {
    int n = 0;
    if (gHookLevel >= 2) {
        n += VTHookNamedClass("_UIStatusBarStringView", VTKindStringStyle, VTSelApplyStyle(), NO);
        n += VTHookNamedClass("_UIStatusBarStringView", VTKindStringSetText, VTSelSetText(), NO);
    }
    if (gHookLevel >= 3) {
        int before = gHookCount;
        n += VTHookNamedClass("_UIStatusBarWifiSignalView", VTKindSignalActive,   VTSelSetActive(),   NO);
        n += VTHookNamedClass("_UIStatusBarWifiSignalView", VTKindSignalInactive, VTSelSetInactive(), NO);
        n += VTHookNamedClass("_UIStatusBarWifiSignalView", VTKindSignalStyle,    VTSelApplyStyle(),  NO);
        n += VTHookNamedClass("_UIStatusBarCellularSignalView", VTKindSignalActive,   VTSelSetActive(),   NO);
        n += VTHookNamedClass("_UIStatusBarCellularSignalView", VTKindSignalInactive, VTSelSetInactive(), NO);
        n += VTHookNamedClass("_UIStatusBarCellularSignalView", VTKindSignalStyle,    VTSelApplyStyle(),  NO);
        if (gHookCount == before) {
            n += VTHookNamedClass("_UIStatusBarSignalView", VTKindSignalActive,   VTSelSetActive(),   NO);
            n += VTHookNamedClass("_UIStatusBarSignalView", VTKindSignalInactive, VTSelSetInactive(), NO);
            n += VTHookNamedClass("_UIStatusBarSignalView", VTKindSignalStyle,    VTSelApplyStyle(),  NO);
        }
    }
    VTLog(@"iOS16 hook：level=%d 已装=%d 条", gHookLevel, gHookCount);
    return n;
}

/// 版本号判断。**不能用 `@available(iOS 17, *)`**。
///
/// 实测：本工程用的 LLVM 工具链（L1ghtmann/llvm-project，clang-11）里，
/// `@available` 会编成对 `___isOSVersionAtLeast` 的引用，而这个符号在它自带的
/// libSystem 里不存在 → 链接阶段 `Undefined symbols for architecture arm64`，
/// 构建直接挂在 Linking 那一步（编译本身是过的，很容易误判成语法错误）。
///
/// 这里换成 NSProcessInfo 的比较，纯 ObjC 运行时，不引入任何新符号。
static BOOL VTIsIOS17OrLater(void) {
    static int cached = -1;
    if (cached < 0) {
        @try {
            NSOperatingSystemVersion want = {17, 0, 0};
            cached = [[NSProcessInfo processInfo] isOperatingSystemAtLeastVersion:want] ? 1 : 0;
        } @catch (NSException *e) {
            cached = 0;
        }
    }
    return cached == 1;
}

static void VTInitialize(void) {
    @autoreleasepool {
        @try {
            VTLoadPrefs();
            if (!gEnabled) {
                VTLog(@"VpnTint %s：总开关关闭，直接退出", VT_VERSION);
                return;
            }
            if (gHookLevel <= 0) {
                // 无卸载降级：level=0 时连私有框架都不 dlopen，更不装任何 hook。
                // 万一又出安全模式，把 HookLevel 设 0 再 respring 就能判断是不是
                // 本插件造成的，不必先进安全模式卸载。
                VTLog(@"VpnTint %s：HookLevel=0，本轮不加载框架、不装任何 hook", VT_VERSION);
                return;
            }

            BOOL is17 = VTIsIOS17OrLater();

            // ★ StatusStatusUI 是 iOS 17 状态栏的新私有框架。SpringBoard 启动早期
            //   它可能还没加载，此时 objc_getClass 拿不到 STUIStatusBar*，必须
            //   dlopen 强制加载后再装 hook。已有则幂等。
            BOOL dloked = NO;
            if (is17) {
                void *h = dlopen("/System/Library/PrivateFrameworks/StatusStatusUI.framework/StatusStatusUI",
                                 RTLD_LAZY);
                dloked = (h != NULL);
            }

            int hooks = is17 ? VTInstall17() : VTInstall16();
            VTCommitHooks();   // 所有 orig 记录完毕后，才统一替换 IMP

            gVPNFromData = NO;
            gVPNFromCF   = gCFFallback ? VTDetectVPNByCFNetwork() : NO;

            VTLog(@"VpnTint %s 已加载 | host=%@ | iOS%d | StatusStatusUI=%d | level=%d | hook=%d | VPN=%d(源=%@)",
                  VT_VERSION,
                  [[NSProcessInfo processInfo] processName],
                  is17 ? 17 : 16,
                  dloked ? 1 : 0,
                  gHookLevel,
                  hooks,
                  VTIsVPNOn() ? 1 : 0,
                  gVPNFromData ? @"data" : (gVPNFromCF ? @"代理设置" : @"无"));

            if (hooks == 0) {
                VTLog(@"警告：一条 hook 都没装上 —— 状态栏私有类名可能又变了，"
                      @"本插件将完全不生效。请把本文件发回以便修正。");
            }

            VTStartTimer();
            VTStartPrefWatch();

            // 启动闸门：窗口内所有 hook 只透传不写色（见文件头 ③）
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(VT_READY_MS * NSEC_PER_MSEC)),
                           dispatch_get_main_queue(), ^{
                gReady = 1;
                gLastDecision = VTIsVPNOn() ? 1 : 0;
                VTLog(@"闸门打开，当前 VPN=%d", gLastDecision);
                VTRepaintSeen();
            });
        } @catch (NSException *e) {
            VTLog(@"构造异常：%@", e);
        }
    }
}

__attribute__((constructor))
static void VTConstructor(void) {
    // ★ 1.0.3：constructor 阶段 dyld 还在装载镜像，Foundation / UIKit /
    //   StatusStatusUI 都可能没初始化完。1.0.2 在这里同步 dlopen + 全量遍历
    //   类表 + 改 IMP，是「装上就进安全模式」的第二个嫌疑。
    //   这里只投递一个 block，真正的初始化放到主队列的第一个 turn。
    dispatch_async(dispatch_get_main_queue(), ^{
        @autoreleasepool {
            @try { VTInitialize(); }
            @catch (NSException *e) { VTLog(@"初始化异常：%@", e); }
        }
    });
}
