TARGET := iphone:clang:latest:15.0
ARCHS = arm64 arm64e
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