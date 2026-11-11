TARGET := iphone:clang:latest:15.0
# ★ 只发 arm64 单切片（1.0.4 起）。这不是省事，是唯一确定性安全的选项。
#
# 证据链：
#   1.0.3 的双切片里，arm64e 那一片 cpusubtype=0x00000002（caps=0x00），
#   即缺 CPU_SUBTYPE_PTRAUTH_ABI(0x80)，属 iOS 12/13 的老式无版本 ABI。
#   而 CI 构建日志里链接器自己就在警告：
#     ld: warning: object file Tweak.m.17ed827e.o was built with an
#                 incompatible arm64e ABI compiler
#   —— 说明**编译器产出的机器码本身就是老 ABI**，不是头字段漏标。
#
#   所以「补 caps=0x80」是错误修法：它只把标签改成新 ABI，而代码里该有的
#   ptrauth 签名指令一条都没有。二进制会声称自己是新 ABI，实际按老 ABI 跑，
#   一旦与按新 ABI 编译的系统库交换函数指针/block，验签失败点比现在更隐蔽。
#   这属于「用改元数据掩盖 ABI 不匹配」，不能做。
#
#   只发 arm64：arm64e 设备上 dyld 走 arm64 兼容加载路径，整条 ptrauth
#   路径都不经过，ABI 问题从根上消失。状态栏染色是纯 UI 逻辑，无性能代价。
#   社区同类症状（WatchPair11）也是靠这一条修好的。
#   CPIOS 的 roothide 包仍标 Architecture: iphoneos-arm64e —— 那是 dpkg 的
#   安装匹配元数据，与包内二进制切片无关，arm64 切片在 arm64e 设备上照常加载。
ARCHS = arm64
INSTALL_TARGET_PROCESSES = SpringBoard

include $(THEOS)/makefiles/common.mk

TWEAK_NAME = VpnTint
VpnTint_FILES = Tweak.m
# -fobjc-arc：账本与关联对象依赖 ARC 的所有权语义
# -Wno-deprecated-declarations：currentTraitCollection 等在新 SDK 上有弃用告警，
#   而 -Werror 下告警即失败。这里用的是唯一稳定可用的 API，不能删。
VpnTint_CFLAGS = -fobjc-arc -O2 -Wno-unused-function -Wno-deprecated-declarations
VpnTint_FRAMEWORKS = UIKit Foundation CoreFoundation CFNetwork

include $(THEOS_MAKE_PATH)/tweak.mk