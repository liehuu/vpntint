// VpnTint —— VPN 连接时把状态栏 Wi-Fi 图标 / 蜂窝信号、4G/5G 字样染成橙色，断开恢复默认色。
// 目标：iOS 15 ~ 17（iOS 13 起应用内的状态栏也由 SpringBoard 渲染，所以只注入 SpringBoard 就够）
//
// 设计要点：
// 1) 主 hook 打在 _UIStatusBarItemView 的 applyStyleAttributes:（框架每次刷新状态栏都会调用它，
//    是「框架把颜色改回去」的唯一入口，在这里覆写不会被下一个 tick 冲掉）。
// 2) 子类若自己 override 了 applyStyleAttributes:，基类 hook 拦不到 → 用 runtime 遍历
//    _UIStatusBarItemView 的所有子类逐个补 hook，并把「替换前的有效 IMP」记表，链式调用原实现。
// 3) 热路径零 IO、零日志、零 NSString 分配（类名判定走 class_getName + strcasestr）。
// 4) 颜色写入幂等（值相同不 set），原始值存关联对象，断开时逐项还原，不猜默认色。
// 5) 所有 hook / 构造器入口包 @try-@catch，异常不外泄到 SpringBoard。

#import <UIKit/UIKit.h>
#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <string.h>
#import <fcntl.h>
#import <unistd.h>
#import <sys/stat.h>

extern CFDictionaryRef CFNetworkCopySystemProxySettings(void);

#define VT_VERSION   "1.0.0"
#define VT_PREF_PATH "/var/mobile/Library/Preferences/com.liehuu.vpntint.plist"

#pragma mark - 配置

static BOOL       gEnabled      = YES;   // 总开关
static BOOL       gTintBars     = YES;   // 蜂窝信号格子是否也染橙
static BOOL       gTintTypeText = YES;   // 4G/5G 文字是否也染橙
static CGFloat    gPollInterval = 1.0;   // VPN 状态轮询间隔（秒）
static UIColor   *gTintColor    = nil;   // 目标颜色
static NSArray   *gIgnoreIfaces = nil;   // 不计入 VPN 的接口名前缀（例如 utun0）

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
    gPollInterval = 1.0;
    gTintColor    = [UIColor systemOrangeColor];
    gIgnoreIfaces = @[];

    NSDictionary *d = [NSDictionary dictionaryWithContentsOfFile:@VT_PREF_PATH];
    if (![d isKindOfClass:[NSDictionary class]]) return;

    if (d[@"Enabled"])        gEnabled      = [d[@"Enabled"] boolValue];
    if (d[@"TintSignalBars"]) gTintBars     = [d[@"TintSignalBars"] boolValue];
    if (d[@"TintTypeText"])   gTintTypeText = [d[@"TintTypeText"] boolValue];
    if (d[@"PollInterval"]) {
        CGFloat v = [d[@"PollInterval"] doubleValue];
        if (v >= 0.3) gPollInterval = v;
    }
    UIColor *c = VTColorFromHex(d[@"ColorHex"]);
    if (c) gTintColor = c;
    if ([d[@"IgnoreInterfaces"] isKindOfClass:[NSArray class]]) gIgnoreIfaces = d[@"IgnoreInterfaces"];
}

#pragma mark - 日志（只在启动与状态切换时写）

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

#pragma mark - VPN 状态判定

// 依据：系统代理设置的 __SCOPED__ 里出现隧道接口（utun / ipsec / ppp / tun / tap / wg）。
// 纯用户态 API，无需 entitlement，SpringBoard 内可直接调用。
static BOOL VTDetectVPN(void) {
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

#pragma mark - 视图识别

typedef NS_ENUM(NSInteger, VTKind) {
    VTKindNone = 0,
    VTKindWifi,
    VTKindCellular,
};

static inline BOOL VTHasC(const char *hay, const char *needle) {
    return hay && needle && strcasestr(hay, needle) != NULL;
}

// 从视图自身类名判断（零分配）
static VTKind VTKindFromClassName(Class c) {
    const char *n = class_getName(c);
    if (VTHasC(n, "Wifi") || VTHasC(n, "WiFi")) return VTKindWifi;
    if (VTHasC(n, "Cellular") || VTHasC(n, "Signal")) return VTKindCellular;
    return VTKindNone;
}

static VTKind VTKindForView(UIView *v) {
    Class c = object_getClass(v);
    VTKind k = VTKindFromClassName(c);
    if (k != VTKindNone) return k;

    const char *cn = class_getName(c);

    // 次级判据：item 的 identifier（wifi / cellular / cellular.type ...）
    // 只对字符串/条目视图尝试，避免热路径上给每个视图都做 msgSend + description（会分配）
    if (VTHasC(cn, "String") || VTHasC(cn, "ItemView")) {
        @try {
            if ([v respondsToSelector:@selector(item)]) {
                id item = ((id (*)(id, SEL))objc_msgSend)(v, @selector(item));
                NSString *ident = nil;
                if (item && [item respondsToSelector:@selector(identifier)]) {
                    id i = ((id (*)(id, SEL))objc_msgSend)(item, @selector(identifier));
                    if (i) {
                        if ([i respondsToSelector:@selector(stringRepresentation)]) {
                            id s = ((id (*)(id, SEL))objc_msgSend)(i, @selector(stringRepresentation));
                            if ([s isKindOfClass:[NSString class]]) ident = s;
                        }
                        if (!ident) ident = [i description];
                    }
                }
                if ([ident isKindOfClass:[NSString class]]) {
                    if (VTHasC(ident.UTF8String, "wifi")) return VTKindWifi;
                    if (VTHasC(ident.UTF8String, "cellular")) return VTKindCellular;
                }
            }
        } @catch (NSException *e) { }
    }

    // 三级判据：祖先（4G/5G 那个 label 是通用 _UIStatusBarStringView，只能靠祖先认）
    UIView *p = v.superview;
    int depth = 0;
    while (p && depth++ < 10) {
        VTKind pk = VTKindFromClassName(object_getClass(p));
        if (pk != VTKindNone) return pk;
        p = p.superview;
    }
    return VTKindNone;
}

#pragma mark - 上色 / 还原（原始值存关联对象，幂等写入）

static const void *kOrigTextColor = &kOrigTextColor;
static const void *kOrigTintColor = &kOrigTintColor;
static const void *kOrigImage     = &kOrigImage;

static void VTSaveOnce(id obj, const void *key, id value) {
    if (objc_getAssociatedObject(obj, key)) return;
    objc_setAssociatedObject(obj, key, value ?: [NSNull null], OBJC_ASSOCIATION_RETAIN_NONATOMIC);
}

static void VTSetLabel(UILabel *l, UIColor *c) {
    VTSaveOnce(l, kOrigTextColor, l.textColor);
    if (![l.textColor isEqual:c]) l.textColor = c;
}

static void VTRestoreLabel(UILabel *l) {
    id o = objc_getAssociatedObject(l, kOrigTextColor);
    if (!o) return;
    l.textColor = (o == [NSNull null]) ? nil : (UIColor *)o;
    objc_setAssociatedObject(l, kOrigTextColor, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
}

static void VTSetImageView(UIImageView *iv, UIColor *c) {
    UIImage *img = iv.image;
    if (img && img.renderingMode != UIImageRenderingModeAlwaysTemplate) {
        VTSaveOnce(iv, kOrigImage, img);
        iv.image = [img imageWithRenderingMode:UIImageRenderingModeAlwaysTemplate];
    }
    VTSaveOnce(iv, kOrigTintColor, iv.tintColor);
    if (![iv.tintColor isEqual:c]) iv.tintColor = c;
}

static void VTRestoreImageView(UIImageView *iv) {
    id oi = objc_getAssociatedObject(iv, kOrigImage);
    if (oi && oi != [NSNull null]) iv.image = (UIImage *)oi;
    objc_setAssociatedObject(iv, kOrigImage, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    id oc = objc_getAssociatedObject(iv, kOrigTintColor);
    if (oc) {
        iv.tintColor = (oc == [NSNull null]) ? nil : (UIColor *)oc;
        objc_setAssociatedObject(iv, kOrigTintColor, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    }
}

// 信号格子：部分版本用 tintColor 自绘，部分版本是若干 backgroundColor 小 view，两种都覆盖
static void VTSetSignalView(UIView *v, UIColor *c) {
    VTSaveOnce(v, kOrigTintColor, v.tintColor);
    if (![v.tintColor isEqual:c]) v.tintColor = c;
    for (UIView *s in [v.subviews copy]) {
        if (s.backgroundColor && CGColorGetAlpha(s.backgroundColor.CGColor) > 0.01) {
            VTSaveOnce(s, kOrigTintColor, s.backgroundColor);
            if (![s.backgroundColor isEqual:c]) s.backgroundColor = c;
        }
    }
}

static void VTRestoreSignalView(UIView *v) {
    id oc = objc_getAssociatedObject(v, kOrigTintColor);
    if (oc) {
        v.tintColor = (oc == [NSNull null]) ? nil : (UIColor *)oc;
        objc_setAssociatedObject(v, kOrigTintColor, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    }
    for (UIView *s in [v.subviews copy]) {
        id ob = objc_getAssociatedObject(s, kOrigTintColor);
        if (ob) {
            s.backgroundColor = (ob == [NSNull null]) ? nil : (UIColor *)ob;
            objc_setAssociatedObject(s, kOrigTintColor, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        }
    }
}

static void VTTintNode(UIView *v, UIColor *c, VTKind kind, int depth) {
    if (!v || depth > 6) return;
    const char *cn = class_getName(object_getClass(v));

    if ([v isKindOfClass:[UILabel class]]) {
        if (kind == VTKindCellular && !gTintTypeText) return;   // 4G/5G 文字：按配置跳过
        VTSetLabel((UILabel *)v, c);
        return;
    }
    if ([v isKindOfClass:[UIImageView class]]) {
        VTSetImageView((UIImageView *)v, c);
        for (UIView *s in [v.subviews copy]) VTTintNode(s, c, kind, depth + 1);
        return;
    }
    if (VTHasC(cn, "Signal")) {
        if (kind == VTKindCellular && !gTintBars) return;       // 信号格子：按配置跳过
        VTSetSignalView(v, c);
    } else {
        if (![v.tintColor isEqual:c]) v.tintColor = c;
    }
    for (UIView *s in [v.subviews copy]) VTTintNode(s, c, kind, depth + 1);
}

static void VTUntintNode(UIView *v, int depth) {
    if (!v || depth > 6) return;
    const char *cn = class_getName(object_getClass(v));
    if ([v isKindOfClass:[UILabel class]]) { VTRestoreLabel((UILabel *)v); return; }
    if ([v isKindOfClass:[UIImageView class]]) {
        VTRestoreImageView((UIImageView *)v);
        for (UIView *s in [v.subviews copy]) VTUntintNode(s, depth + 1);
        return;
    }
    if (VTHasC(cn, "Signal")) {
        VTRestoreSignalView(v);
    } else {
        id oc = objc_getAssociatedObject(v, kOrigTintColor);
        if (oc) {
            v.tintColor = (oc == [NSNull null]) ? nil : (UIColor *)oc;
            objc_setAssociatedObject(v, kOrigTintColor, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        }
    }
    for (UIView *s in [v.subviews copy]) VTUntintNode(s, depth + 1);
}

#pragma mark - 视图登记与统一刷新

static NSHashTable<UIView *> *gItemViews = nil;   // weak，不持有视图
static BOOL gVPNOn = NO;
static BOOL gBaseClassExists = NO;
static BOOL gDidLogFirstTint = NO;

static void VTHandleItemView(UIView *v) {
    if (!v || !gEnabled) return;
    VTKind k = VTKindForView(v);
    if (k == VTKindNone) return;
    if (!gItemViews) gItemViews = [NSHashTable weakObjectsHashTable];
    [gItemViews addObject:v];
    if (gVPNOn) {
        VTTintNode(v, gTintColor, k, 0);
        if (!gDidLogFirstTint) {
            gDidLogFirstTint = YES;
            VTLog(@"首次上色命中：%s (kind=%ld)", class_getName(object_getClass(v)), (long)k);
        }
    }
}

static void VTApplyAll(BOOL on) {
    if (!gItemViews) return;
    for (UIView *v in gItemViews.allObjects) {
        VTKind k = VTKindForView(v);
        if (k == VTKindNone) continue;
        if (on) VTTintNode(v, gTintColor, k, 0);
        else    VTUntintNode(v, 0);
    }
}

// 兜底发现：万一两条 hook 都没命中（私有类改名），从窗口树里按类名捞一遍
static void VTScanView(UIView *v, int depth, NSMutableArray *out) {
    if (!v || depth > 14) return;
    if (VTKindForView(v) != VTKindNone) [out addObject:v];
    for (UIView *s in [v.subviews copy]) VTScanView(s, depth + 1, out);
}

static void VTDiscoverFallback(void) {
    @try {
        if (!gItemViews) gItemViews = [NSHashTable weakObjectsHashTable];
        NSMutableArray *out = [NSMutableArray array];
        for (UIWindow *w in [[UIApplication sharedApplication] windows]) VTScanView(w, 0, out);
        for (UIView *v in out) [gItemViews addObject:v];
        if (out.count) {
            gDidLogFirstTint = NO;
            if (gVPNOn) VTApplyAll(YES);
        }
    } @catch (NSException *e) { }
}

#pragma mark - runtime 补 hook（子类自己 override 了 applyStyleAttributes: 时基类 hook 拦不到）

static NSMutableDictionary<NSString *, id> *gOrigIMPs = nil;

static void VTHookApply(id self, SEL _cmd, id attrs) {
    Class c = object_getClass(self);
    while (c) {
        id boxed = gOrigIMPs[NSStringFromClass(c)];
        if (boxed) {
            IMP imp = [boxed pointerValue];
            if (imp && imp != (IMP)VTHookApply) ((void (*)(id, SEL, id))imp)(self, _cmd, attrs);
            break;
        }
        c = class_getSuperclass(c);
    }
    @try { VTHandleItemView((UIView *)self); } @catch (NSException *e) { }
}

static int VTInstallRuntimeHooks(const char *baseName) {
    Class base = objc_getClass(baseName);
    if (!base) return 0;
    SEL sel = @selector(applyStyleAttributes:);
    if (!gOrigIMPs) gOrigIMPs = [NSMutableDictionary dictionary];

    int installed = 0;
    unsigned int num = 0;
    Class *all = objc_copyClassList(&num);
    if (!all) return 0;

    for (unsigned int i = 0; i < num; i++) {
        Class c = all[i];
        BOOL isSub = (c == base);
        if (!isSub) {
            Class s = c;
            while ((s = class_getSuperclass(s))) {
                if (s == base) { isSub = YES; break; }
            }
        }
        if (!isSub) continue;

        Method m = class_getInstanceMethod(c, sel);
        if (!m) continue;

        IMP eff = method_getImplementation(m);
        if (eff == (IMP)VTHookApply) continue;              // 已补过，别自我递归

        // 记「替换前的有效实现」（可能是自己实现的，也可能是继承来的）
        gOrigIMPs[NSStringFromClass(c)] = [NSValue valueWithPointer:eff];
        class_replaceMethod(c, sel, (IMP)VTHookApply, method_getTypeEncoding(m));
        installed++;
    }
    free(all);
    return installed;
}

#pragma mark - Logos hook

%hook _UIStatusBarItemView

- (void)applyStyleAttributes:(id)attributes {
    %orig;
    @try { VTHandleItemView(self); } @catch (NSException *e) { }
}

- (void)layoutSubviews {
    %orig;
    @try { VTHandleItemView(self); } @catch (NSException *e) { }
}

%end

#pragma mark - 定时器

static dispatch_source_t gTimer = NULL;
static dispatch_source_t gPrefWatch = NULL;

static void VTStartTimer(void) {
    if (gTimer) return;
    gTimer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_main_queue());
    uint64_t interval = (uint64_t)(gPollInterval * (double)NSEC_PER_SEC);
    if (interval < 200ull * NSEC_PER_MSEC) interval = 200ull * NSEC_PER_MSEC;
    dispatch_source_set_timer(gTimer, dispatch_time(DISPATCH_TIME_NOW, interval), interval, 200ull * NSEC_PER_MSEC);
    dispatch_source_set_event_handler(gTimer, ^{
        @try {
            if (!gEnabled) return;
            BOOL on = VTDetectVPN();
            if (on != gVPNOn) {
                gVPNOn = on;
                VTLog(@"VPN 状态 -> %@", on ? @"已连接" : @"已断开");
                VTApplyAll(on);
                if (on && gItemViews.count == 0) VTDiscoverFallback();
            } else if (on) {
                VTApplyAll(YES);                    // 幂等重刷，压住框架自己的重置
                if (gItemViews.count == 0) VTDiscoverFallback();
            }
        } @catch (NSException *e) { }
    });
    dispatch_resume(gTimer);
}

static void VTStartPrefWatch(void) {
    if (gPrefWatch) return;
    int fd = open(VT_PREF_PATH, O_EVTONLY);
    if (fd < 0) return;                             // 没写配置就跳过，不报错
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
            if (!gEnabled) { VTApplyAll(NO); return; }
            VTApplyAll(gVPNOn);
        } @catch (NSException *e) { }
    });
    dispatch_resume(gPrefWatch);
}

#pragma mark - 构造

%ctor {
    @autoreleasepool {
        @try {
            VTLoadPrefs();

            gBaseClassExists = objc_getClass("_UIStatusBarItemView") != NULL;
            int n = VTInstallRuntimeHooks("_UIStatusBarItemView");
            gVPNOn = VTDetectVPN();

            VTLog(@"VpnTint %s 已加载 | host=%@ | 基类=%d | 补hook子类=%d | VPN=%d",
                  VT_VERSION, [[NSProcessInfo processInfo] processName],
                  gBaseClassExists ? 1 : 0, n, gVPNOn ? 1 : 0);
            if (!gBaseClassExists) VTLog(@"警告：未找到 _UIStatusBarItemView，将走窗口兜底扫描");

            VTStartTimer();
            VTStartPrefWatch();
        } @catch (NSException *e) {
            VTLog(@"构造异常：%@", e);
        }
    }
}
