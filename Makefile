TARGET := iphone:clang:latest:15.0
ARCHS = arm64 arm64e
INSTALL_TARGET_PROCESSES = SpringBoard

include $(THEOS)/makefiles/common.mk

TWEAK_NAME = VpnTint
VpnTint_FILES = Tweak.x
VpnTint_CFLAGS = -fobjc-arc -O2 -Wno-unused-function
VpnTint_FRAMEWORKS = UIKit Foundation CoreFoundation CFNetwork

include $(THEOS_MAKE_PATH)/tweak.mk
