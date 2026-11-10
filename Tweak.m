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
// 工程约束：
//   · 不用 Logos，入口是 __attribute__((constructor))，全部 hook 走 runtime。
//     这样每个 hook 点都有 NULL 检查与返回值，不会因为某个类在某个系统版本
//     上不存在而整组静默失败。
//   · 热路径（applyStyleAttributes: 等）零 NSString 分配、零 IO、零日志。
//     原 IMP 查找用静态 C 数组线性扫描（hook 点 < 64，指针比较）。
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

#define VT_VERSION   "1.0.2"
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
    gTintBars     = YES;
    gTintTypeText = YES;
    gCFFallback   = YES;
    gPollInterval = 1.0;
    gTintColor    = [UIColor systemOrangeColor];
    gIgnoreIfaces = @[];

    NSDictionary *d = [NSDictionary dictionaryWithContentsOfFile:@VT_PREF_PATH];
    if (![d isKindOfClass:[NSDictionary class]]) return;

    if (d[@"Enabled"])        gEnabled      = [d[@"Enabled"] boolValue];
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
        IMP imp = [view methodForSelector:sel];
        if (imp) ((void (*)(id, SEL, id))imp)(view, sel, color);
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
// hook 安装器（全部 runtime，零 Logos）
//
// 原 IMP 存静态 C 数组，链式调用时线性扫描 + 指针比较 —— 零 NSString 分配。
// ============================================================================

typedef struct {
    Class cls;
    SEL   sel;
    IMP   orig;
} VTHookRec;

static VTHookRec gHooks[64];
static int       gHookCount = 0;

static IMP VTFindOrig(Class c, SEL sel) {
    while (c) {
        for (int i = 0; i < gHookCount; i++) {
            if (gHooks[i].cls == c && sel_isEqual(gHooks[i].sel, sel)) return gHooks[i].orig;
        }
        c = class_getSuperclass(c);
    }
    return NULL;
}

static BOOL VTHookMethod(Class c, SEL sel, IMP repl) {
    if (!c || !sel || !repl) return NO;
    Method m = class_getInstanceMethod(c, sel);
    if (!m) return NO;
    IMP eff = method_getImplementation(m);
    if (eff == repl) return NO;                       // 已装过，别自我递归
    if (gHookCount >= 64) return NO;
    gHooks[gHookCount].cls  = c;
    gHooks[gHookCount].sel  = sel;
    gHooks[gHookCount].orig = eff;
    gHookCount++;
    class_replaceMethod(c, sel, repl, method_getTypeEncoding(m));
    return YES;
}

/// 把 IMP 调回原实现。参数统一按 id 传，够用（本插件 hook 的点都是对象参数）。
static void VTChain1(id self, SEL cmd, id a1) {
    IMP imp = VTFindOrig(object_getClass(self), cmd);
    if (imp) ((void (*)(id, SEL, id))imp)(self, cmd, a1);
}

static void VTChain2(id self, SEL cmd, id a1, id a2) {
    IMP imp = VTFindOrig(object_getClass(self), cmd);
    if (imp) ((void (*)(id, SEL, id, id))imp)(self, cmd, a1, a2);
}

/// applyStyleAttributes: / setText: / setActiveColor: / setInactiveColor: 都不在
/// 任何公开头里完整声明，直接写 @selector(...) 会触发 undeclared selector 告警，
/// -Werror 下编译直接失败。一律走字符串转 SEL，只在安装阶段调用一次。
static SEL VTSelApplyStyle(void)  { return NSSelectorFromString(@"applyStyleAttributes:"); }
static SEL VTSelSetText(void)     { return NSSelectorFromString(@"setText:"); }
static SEL VTSelSetActive(void)   { return NSSelectorFromString(@"setActiveColor:"); }
static SEL VTSelSetInactive(void) { return NSSelectorFromString(@"setInactiveColor:"); }

/// 收集一个类及其全部子类（子类 override 后基类 hook 拦不到，必须逐个补）。
static int VTCollectSubclasses(Class base, Class *out, int cap) {
    if (!base || !out || cap <= 0) return 0;
    out[0] = base;
    int n = 1;
    unsigned int total = 0;
    Class *all = objc_copyClassList(&total);
    if (!all) return n;
    for (unsigned int i = 0; i < total && n < cap; i++) {
        Class c = all[i];
        if (c == base) continue;
        Class s = c;
        BOOL isSub = NO;
        while ((s = class_getSuperclass(s))) {
            if (s == base) { isSub = YES; break; }
        }
        if (isSub) out[n++] = c;
    }
    free(all);
    return n;
}

/// 泛化收集：类名含 needle 且实现了 sel 的类（应对私有类改名）。
static int VTCollectByName(const char *needle, SEL sel, Class *out, int cap) {
    if (!needle || !sel || !out || cap <= 0) return 0;
    int n = 0;
    unsigned int total = 0;
    Class *all = objc_copyClassList(&total);
    if (!all) return 0;
    for (unsigned int i = 0; i < total && n < cap; i++) {
        Class c = all[i];
        const char *cn = class_getName(c);
        if (!cn || !strcasestr(cn, needle)) continue;
        if (!class_getInstanceMethod(c, sel)) continue;
        out[n++] = c;
    }
    free(all);
    return n;
}

// ============================================================================
// 染色实现
// ============================================================================

/// 闸门期间记录到 gSeen 的视图，闸门打开后补染一次
/// （覆盖「先连 VPN 再 respring」：那时 setActiveColor: 早已调用完）。

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

static void VTHookUpdateAggregated(id self, SEL _cmd, id data) {
    VTCaptureVPNFromData(data);
    VTNoteTransition();
    VTChain1(self, _cmd, data);
}

static void VTHookUpdateWithCompletion(id self, SEL _cmd, id data, id handler) {
    VTCaptureVPNFromData(data);
    VTNoteTransition();
    VTChain2(self, _cmd, data, handler);
}

#pragma mark - 文字层：4G / 5G

static void VTHookStringApplyStyle(id self, SEL _cmd, id attrs) {
    id pass = attrs;
    VTRemember((UIView *)self);
    if (VTShouldTint() && gTintTypeText && attrs) {
        @try {
            NSString *t = [(UILabel *)self text];
            if (VTIsNetworkTypeText(t)) {
                STUIStatusBarStyleAttributes *sa = (STUIStatusBarStyleAttributes *)attrs;
                // ★ 必须在 copy 之前读：改的是副本，原始入参才是系统本来要用的色
                UIColor *sys = nil;
                if ([sa respondsToSelector:@selector(textColor)]) sys = [sa textColor];
                UIColor *paint = gTintColor;
                if (sys && paint) {
                    id copy = [sa copy];
                    if (copy) {
                        [(STUIStatusBarStyleAttributes *)copy setTextColor:paint];
                        pass = copy;
                        VTRecord(self, @"tc", sys, paint);
                        if (gTintHits++ == 0) {
                            VTLog(@"首次上色（文字）：%s text=%@", class_getName(object_getClass(self)), t);
                        }
                    }
                }
            }
        } @catch (NSException *e) {}
    }
    VTChain1(self, _cmd, pass);
}

static void VTHookStringSetText(id self, SEL _cmd, NSString *text) {
    VTChain1(self, _cmd, text);
    if (VTIsNetworkTypeText(text)) VTRemember((UIView *)self);
    if (!VTShouldTint() || !gTintTypeText) return;
    if (!VTIsNetworkTypeText(text)) return;
    @try {
        // self.textColor 可能已是我们上一轮染的（VTRecord 会识别并拒绝记账）
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

static void VTHookSetActiveColor(id self, SEL _cmd, UIColor *color) {
    UIColor *pass = color;
    VTRemember((UIView *)self);
    if (VTShouldTint() && gTintBars && color) {
        @try {
            UIColor *paint = gTintColor;
            if (paint) {
                VTRecord(self, @"ac", color, paint);
                pass = paint;
                if (gTintHits++ == 0) {
                    VTLog(@"首次上色（active）：%s", class_getName(object_getClass(self)));
                }
            }
        } @catch (NSException *e) {}
    }
    VTChain1(self, _cmd, pass);
}

static void VTHookSetInactiveColor(id self, SEL _cmd, UIColor *color) {
    UIColor *pass = color;
    VTRemember((UIView *)self);
    if (VTShouldTint() && gTintBars && color) {
        @try {
            // ★★ 保 alpha：见文件头 ⑤
            UIColor *paint = VTKeepAlpha(color, gTintColor);
            if (paint) {
                VTRecord(self, @"ic", color, paint);
                pass = paint;
            }
        } @catch (NSException *e) {}
    }
    VTChain1(self, _cmd, pass);
}

/// 信号视图另一条改色通路：样式属性直接下发 tint。改副本，不记账
/// （副本只影响本次绘制，下一次样式下发会自然覆盖）。
static void VTHookSignalApplyStyle(id self, SEL _cmd, id attrs) {
    id pass = attrs;
    if (VTShouldTint() && gTintBars && attrs) {
        @try {
            STUIStatusBarStyleAttributes *sa = (STUIStatusBarStyleAttributes *)attrs;
            UIColor *sys = nil;
            UIColor *sysDim = nil;
            if ([sa respondsToSelector:@selector(imageTintColor)]) sys = [sa imageTintColor];
            if (!sys && [sa respondsToSelector:@selector(textColor)]) sys = [sa textColor];
            if ([sa respondsToSelector:@selector(imageDimmedTintColor)]) sysDim = [sa imageDimmedTintColor];
            if (sys && gTintColor) {
                id copy = [sa copy];
                if (copy) {
                    if ([copy respondsToSelector:@selector(setImageTintColor:)]) {
                        [(STUIStatusBarStyleAttributes *)copy setImageTintColor:gTintColor];
                    }
                    // ★★ dimmed 也要保 alpha：压到 0.2 同样会把次要元素弄没
                    UIColor *dimmed = VTKeepAlpha(sysDim ?: sys, gTintColor);
                    if (dimmed && [copy respondsToSelector:@selector(setImageDimmedTintColor:)]) {
                        [(STUIStatusBarStyleAttributes *)copy setImageDimmedTintColor:dimmed];
                    }
                    pass = copy;
                }
            }
        } @catch (NSException *e) {}
    }
    VTChain1(self, _cmd, pass);
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
                    IMP imp = [v methodForSelector:@selector(setActiveColor:)];
                    if (imp) ((void (*)(id, SEL, id))imp)(v, @selector(setActiveColor:), gTintColor);
                    painted++;
                }
                if ([v respondsToSelector:@selector(setInactiveColor:)]) {
                    IMP imp = [v methodForSelector:@selector(setInactiveColor:)];
                    if (imp) ((void (*)(id, SEL, id))imp)(v, @selector(setInactiveColor:), gTintColor);
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

/// 装一组 hook，返回成功条数。
static int VTInstall17(void) {
    int n = 0;
    SEL selApply  = VTSelApplyStyle();
    SEL selText   = VTSelSetText();
    SEL selActive = VTSelSetActive();
    SEL selInact  = VTSelSetInactive();

    // ① 状态栏数据（VPN 状态主源）
    Class sb = objc_getClass("STUIStatusBar");
    if (sb) {
        if (VTHookMethod(sb, NSSelectorFromString(@"_updateWithAggregatedData:"),
                         (IMP)VTHookUpdateAggregated)) n++;
        if (VTHookMethod(sb, NSSelectorFromString(@"_updateWithData:completionHandler:"),
                         (IMP)VTHookUpdateWithCompletion)) n++;
    }

    // ② 文字视图（4G / 5G）—— 按类名 + 方法存在性收集，抗私有类改名。
    //    两个收集结果取并集：StringView 一层可能既有基类也有子类，各自都 override 过。
    Class strBuf[64];
    int strN = VTCollectByName("StringView", selApply, strBuf, 64);
    for (int i = 0; i < strN; i++) {
        if (VTHookMethod(strBuf[i], selApply, (IMP)VTHookStringApplyStyle)) n++;
    }
    int setTextN = VTCollectByName("StringView", selText, strBuf, 64);
    for (int i = 0; i < setTextN; i++) {
        if (VTHookMethod(strBuf[i], selText, (IMP)VTHookStringSetText)) n++;
    }

    // ③ 信号视图（Wi-Fi / 蜂窝）—— 基类 + 全部子类；基类不存在时退化为按名收集。
    //    两路结果合并：STUIStatusBarSignalView 的子类里，Wi-Fi 与蜂窝各一个，
    //    任一路漏掉都会出现「只有 Wi-Fi 变色 / 只有信号格变色」。
    Class merged[128];
    int mergedN = 0;
    Class sigBuf[96];
    Class sigBase = objc_getClass("STUIStatusBarSignalView");
    if (sigBase) {
        int k = VTCollectSubclasses(sigBase, sigBuf, 96);
        for (int i = 0; i < k && mergedN < 128; i++) merged[mergedN++] = sigBuf[i];
    }
    int byName = VTCollectByName("SignalView", selInact, sigBuf, 96);
    for (int i = 0; i < byName && mergedN < 128; i++) {
        BOOL dup = NO;
        for (int j = 0; j < mergedN; j++) {
            if (merged[j] == sigBuf[i]) { dup = YES; break; }
        }
        if (!dup) merged[mergedN++] = sigBuf[i];
    }
    for (int i = 0; i < mergedN; i++) {
        if (VTHookMethod(merged[i], selActive, (IMP)VTHookSetActiveColor)) n++;
        if (VTHookMethod(merged[i], selInact, (IMP)VTHookSetInactiveColor)) n++;
        // applyStyleAttributes: 不是每个信号类都实现，装不上不算失败
        VTHookMethod(merged[i], selApply, (IMP)VTHookSignalApplyStyle);
    }

    VTLog(@"iOS17 hook：data=%d string=%d signal=%d（共 %d 条）",
          sb ? 1 : 0, strN, mergedN, n);
    return n;
}

static int VTInstall16(void) {
    int n = 0;
    SEL selApply  = VTSelApplyStyle();
    SEL selText   = VTSelSetText();
    SEL selActive = VTSelSetActive();
    SEL selInact  = VTSelSetInactive();

    Class strBuf[64];
    int strN = VTCollectByName("StringView", selApply, strBuf, 64);
    for (int i = 0; i < strN; i++) {
        if (VTHookMethod(strBuf[i], selApply, (IMP)VTHookStringApplyStyle)) n++;
    }
    int setTextN = VTCollectByName("StringView", selText, strBuf, 64);
    for (int i = 0; i < setTextN; i++) {
        if (VTHookMethod(strBuf[i], selText, (IMP)VTHookStringSetText)) n++;
    }

    Class merged[128];
    int mergedN = 0;
    Class sigBuf[96];
    Class sigBase = objc_getClass("_UIStatusBarSignalView");
    if (sigBase) {
        int k = VTCollectSubclasses(sigBase, sigBuf, 96);
        for (int i = 0; i < k && mergedN < 128; i++) merged[mergedN++] = sigBuf[i];
    }
    int byName = VTCollectByName("StatusBarSignalView", selInact, sigBuf, 96);
    for (int i = 0; i < byName && mergedN < 128; i++) {
        BOOL dup = NO;
        for (int j = 0; j < mergedN; j++) {
            if (merged[j] == sigBuf[i]) { dup = YES; break; }
        }
        if (!dup) merged[mergedN++] = sigBuf[i];
    }
    for (int i = 0; i < mergedN; i++) {
        if (VTHookMethod(merged[i], selActive, (IMP)VTHookSetActiveColor)) n++;
        if (VTHookMethod(merged[i], selInact, (IMP)VTHookSetInactiveColor)) n++;
    }

    VTLog(@"iOS16 hook：string=%d signal=%d（共 %d 条）", strN, mergedN, n);
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

__attribute__((constructor))
static void VTInitialize(void) {
    @autoreleasepool {
        @try {
            VTLoadPrefs();
            if (!gEnabled) {
                VTLog(@"VpnTint %s：总开关关闭，直接退出", VT_VERSION);
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

            gVPNFromData = NO;
            gVPNFromCF   = gCFFallback ? VTDetectVPNByCFNetwork() : NO;

            VTLog(@"VpnTint %s 已加载 | host=%@ | iOS%d | StatusStatusUI=%d | hook=%d | VPN=%d(源=%@)",
                  VT_VERSION,
                  [[NSProcessInfo processInfo] processName],
                  is17 ? 17 : 16,
                  dloked ? 1 : 0,
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