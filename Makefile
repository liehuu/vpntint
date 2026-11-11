TARGET := iphone:clang:latest:15.0
ARCHS = arm64 arm64e
INSTALL_TARGET_PROCESSES = SpringBoard

include $(THEOS)/makefiles/common.mk

TWEAK_NAME = VpnTint
VpnTint_FILES = Tweak.m
# -fobjc-arc：账本与关联对象依赖 ARC 的所有权语义
# -Wno-deprecated-declarations：currentTraitCollection 等在新 SDK 上有弃用告警，
#   而 -Werror 下告警即失败。这里用的是唯一稳定可用的 API，不能删。
#
# 关于 arm64e 的 cpusubtype（1.0.4 起由 CI 后处理保证，不在编译期赌 flag）：
#   1.0.3 的 arm64e 切片实测 cpusubtype=0x00000002，caps=0x00 —— 即
#   CPU_SUBTYPE_PTRAUTH_ABI(0x80) 未置位，属 iOS 12/13 的老式无版本 ABI。
#   现代 arm64e 会拒绝这种二进制。这里**不**加 -fptrauth-abi-version 之类的
#   flag：本机无法验证该 flag 在 CI 工具链上是否存在，加了若不受支持会
#   直接让构建失败，代价比收益大。改为 CI 里确定性地补标志 + 重签 + 双向复核
#   （tools/fix_arm64e_abi.py → tools/check_codesign.py → tools/check_arch.py）。
#   若某天工具链本身就产出 caps=0x80，fix 脚本会自动变成 no-op。
VpnTint_CFLAGS = -fobjc-arc -O2 -Wno-unused-function -Wno-deprecated-declarations
VpnTint_FRAMEWORKS = UIKit Foundation CoreFoundation CFNetwork

include $(THEOS_MAKE_PATH)/tweak.mk