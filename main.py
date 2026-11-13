# WallpaperTool - 桌面水印工具
# 纯 Python 标准库实现（Windows 原生 API：IP Helper / GDI+ / User32），无需第三方库
# 兼容 Windows 7 及以上（Python 3.8 及以下解释器同样兼容 Win7）

import sys, threading, os, ctypes, shutil
import re
import socket
import json
import urllib.request
import tkinter as tk
from tkinter import messagebox
import webbrowser
from ctypes import (POINTER, Structure, byref, c_char_p, c_float, c_int, c_ubyte,
                    c_ulong, c_ushort, c_uint, c_void_p, c_wchar_p, cast)

_user32 = ctypes.windll.user32
_iphlpapi = ctypes.windll.iphlpapi
_gdiplus = ctypes.WinDLL("gdiplus")

# ========================
# 配置：NAT 接口地址等设置项，持久化到 %USERPROFILE%\WallpaperTool\config.json
# ========================

_DEFAULT_CONFIG = {"nat_enabled": False, "nat_api_url": "", "nat_json_field": ""}


def _config_path():
    return os.path.join(os.path.expanduser("~"), "WallpaperTool", "config.json")


def _load_config():
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    # 兼容默认值，防止配置文件缺失键
    result = dict(_DEFAULT_CONFIG)
    result.update({k: v for k, v in cfg.items() if k in _DEFAULT_CONFIG})
    # 旧配置迁移：曾有 nat_api_url 但无开关时，视为已启用
    if "nat_enabled" not in cfg and result.get("nat_api_url"):
        result["nat_enabled"] = True
    return result


def _save_config(cfg):
    try:
        path = _config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存配置失败：%s" % e)

# ========================
# 网络信息：通过 IP Helper API (GetAdaptersAddresses) 获取，替代 wmi 库
# ========================

_AF_UNSPEC = 0
_GAA_FLAG_SKIP_ANYCAST = 0x02
_GAA_FLAG_SKIP_MULTICAST = 0x04
_GAA_FLAG_SKIP_DNS_SERVER = 0x08
_ERROR_BUFFER_OVERFLOW = 111
_IF_OPER_STATUS_UP = 1
_PHYSICAL_IF_TYPES = (6, 71)  # 以太网 / 无线网卡
# 排除虚拟网卡（VMware/Hyper-V/VPN/环回等），近似 wmi 版按 PCI/USB PNPDeviceID 的过滤
# 注意需同时检查 FriendlyName 与 Description：
# 如 VMware 网卡的 FriendlyName 是 "VMware Network Adapter VMnet1"，"Virtual" 只出现在 Description 中
_VIRTUAL_NAME_HINTS = ("vethernet", "virtual", "vmware", "vmnet", "virtualbox",
                       "hyper-v", "tap", "tun", "vpn", "loopback", "bluetooth",
                       "wan miniport", "teredo", "isatap", "pseudo",
                       "wi-fi direct", "hosted network", "ssl")
# Windows 会把无线网卡克隆出多个实例（Wi-Fi Direct/移动热点），克隆实例的
# Description 带 " #N" 后缀（如 "BE200 320MHz #4"），物理网卡不带，以此排除
_RE_CLONED_DESC = re.compile(r"\s#\d+$")


class _SOCKADDR(Structure):
    _fields_ = [("sa_family", c_ushort), ("sa_data", c_ubyte * 14)]


class _SOCKET_ADDRESS(Structure):
    _fields_ = [("lpSockaddr", POINTER(_SOCKADDR)), ("iSockaddrLength", c_int)]


class _sockaddr_in(Structure):
    _fields_ = [("sin_family", c_ushort), ("sin_port", c_ushort),
                ("sin_addr", c_ubyte * 4), ("sin_zero", c_ubyte * 8)]


class _sockaddr_in6(Structure):
    _fields_ = [("sin6_family", c_ushort), ("sin6_port", c_ushort),
                ("sin6_flowinfo", c_ulong), ("sin6_addr", c_ubyte * 16),
                ("sin6_scope_id", c_ulong)]


class _IP_ADAPTER_UNICAST_ADDRESS(Structure):
    pass


# 布局参考 MSDN：union{Length, Flags} → Next → Address → ...（Next 在 Address 之前）
_IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", c_ulong), ("Flags", c_ulong),
    ("Next", POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", _SOCKET_ADDRESS),
]


class _IP_ADAPTER_ADDRESSES(Structure):
    pass


_IP_ADAPTER_ADDRESSES._fields_ = [
        ("Length", c_ulong),
        ("IfIndex", c_ulong),
        ("Next", POINTER(_IP_ADAPTER_ADDRESSES)),
        ("AdapterName", c_char_p),
        ("FirstUnicastAddress", POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
        ("FirstAnycastAddress", c_void_p),
        ("FirstMulticastAddress", c_void_p),
        ("FirstDnsServerAddress", c_void_p),
        ("DnsSuffix", c_wchar_p),
        ("Description", c_wchar_p),
        ("FriendlyName", c_wchar_p),
        ("PhysicalAddress", c_ubyte * 8),
        ("PhysicalAddressLength", c_ulong),
        ("Flags", c_ulong),
        ("Mtu", c_ulong),
        ("IfType", c_ulong),
        ("OperStatus", c_int),
        ("Ipv6IfIndex", c_ulong),
        ("ZoneIndices", c_ulong * 16),
        ("FirstPrefix", c_void_p),
    ]


_iphlpapi.GetAdaptersAddresses.argtypes = [c_ulong, c_ulong, c_void_p, c_void_p, POINTER(c_ulong)]


def _next_unicast(uni):
    """返回单播地址链表的下一个节点指针（无则 None）。"""
    return uni.contents.Next if uni else None


def _unicast_ip(uni):
    """从单播地址结构中提取 IP 字符串，排除回环地址。"""
    sa = uni.contents.Address.lpSockaddr
    if not sa:
        return None
    try:
        family = sa.contents.sa_family
        if family == 2:  # AF_INET
            raw = cast(sa, POINTER(_sockaddr_in)).contents.sin_addr
            ip = socket.inet_ntop(socket.AF_INET, bytes(raw))
            if ip.startswith("127."):
                return None
            return ip
        if family == 23:  # AF_INET6
            raw = cast(sa, POINTER(_sockaddr_in6)).contents.sin6_addr
            ip = socket.inet_ntop(socket.AF_INET6, bytes(raw))
            if ip.lower().startswith("fe80") or ip == "::1":  # 排除 IPv6 链路本地地址
                return None
            return ip
    except Exception:
        return None
    return None


def _get_adapters_info():
    """返回 (已启用物理网卡 MAC 列表, 对应 IP 列表)"""
    mac_list, ip_list = [], []
    flags = _GAA_FLAG_SKIP_ANYCAST | _GAA_FLAG_SKIP_MULTICAST | _GAA_FLAG_SKIP_DNS_SERVER
    size = c_ulong(0)
    rc = _iphlpapi.GetAdaptersAddresses(_AF_UNSPEC, flags, None, None, byref(size))
    if rc != _ERROR_BUFFER_OVERFLOW or size.value == 0:
        return mac_list, ip_list

    buf = (ctypes.c_ubyte * size.value)()
    rc = _iphlpapi.GetAdaptersAddresses(_AF_UNSPEC, flags, None, buf, byref(size))
    if rc != 0:
        return mac_list, ip_list

    adapter = cast(buf, POINTER(_IP_ADAPTER_ADDRESSES)).contents
    while adapter:
        if (adapter.OperStatus == _IF_OPER_STATUS_UP
                and adapter.IfType in _PHYSICAL_IF_TYPES
                and adapter.PhysicalAddressLength):
            name = ("%s %s" % (adapter.FriendlyName or "", adapter.Description or "")).lower()
            if not any(hint in name for hint in _VIRTUAL_NAME_HINTS) \
                    and not _RE_CLONED_DESC.search(adapter.Description or ""):
                mac = ":".join(
                    "%02X" % b for b in adapter.PhysicalAddress[:adapter.PhysicalAddressLength])
                if mac and mac not in mac_list:
                    mac_list.append(mac)
                uni = adapter.FirstUnicastAddress
                while uni:
                    ip = _unicast_ip(uni)
                    if ip and ip not in ip_list:
                        ip_list.append(ip)
                    uni = _next_unicast(uni)
        nxt = adapter.Next
        adapter = nxt.contents if nxt else None
    return mac_list, ip_list


def get_network_info():
    """获取主机名、IP、MAC地址等信息"""
    Userpath = os.path.expanduser("~")
    Hostname = os.getenv("computername") or socket.gethostname()

    MAClist, IPAddress = _get_adapters_info()

    # --- 获取 NAT地址（接口地址与接口字段为设置项，未启用/未配置则不获取、不显示）---
    NATAddress = None
    cfg = _load_config()
    nat_api_url = (cfg.get("nat_api_url") or "").strip()
    nat_json_field = (cfg.get("nat_json_field") or "").strip()
    if cfg.get("nat_enabled") and nat_api_url:
        try:
            # 接口字段留空 = 纯文本响应（整个响应即 IP，如 ipinfo.in）；
            # 填了字段 = JSON 模式，按字段路径取值（支持 a.b.c 点路径），如 {"address": "1.2.3.4"}
            # 部分 IP 查询服务（如 ip.sb）有 Cloudflare 防护，需带浏览器 User-Agent 否则 403
            req = urllib.request.Request(
                nat_api_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read().decode('utf-8').strip()
            if nat_json_field:
                node = json.loads(body)
                for key in nat_json_field.split('.'):
                    node = node[key]
                NATAddress = str(node) if node is not None else None
            else:
                NATAddress = body or None
        except Exception:
            # 获取失败静默处理，返回 None，由上层逻辑决定是否提示
            NATAddress = None

    return {
        'Hostname': Hostname,
        'IPAddress': IPAddress,
        'NATAddress': NATAddress,
        'MACAddress': MAClist,
        'Userpath': Userpath
    }


# ========================
# 图片处理：通过 GDI+（系统自带 gdiplus.dll）完成，替代 Pillow
# ========================

class _GdiplusStartupInput(Structure):
    _fields_ = [("GdiplusVersion", c_uint),
                ("DebugEventCallback", c_void_p),
                ("SuppressBackgroundThread", c_int),
                ("SuppressExternalCodecs", c_int)]


class _RectF(Structure):
    _fields_ = [("X", c_float), ("Y", c_float), ("Width", c_float), ("Height", c_float)]


class _GUID(Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort),
                ("Data4", c_ubyte * 8)]


# JPEG 编码器 CLSID 与质量参数 GUID（GDI+ 文档常量）
def _guid(data1, data2, data3, data4_hex):
    return _GUID(data1, data2, data3, (c_ubyte * 8)(*bytes.fromhex(data4_hex)))


_CLSID_JPEG = _guid(0x557CF401, 0x1A04, 0x11D3, "9a730000f81ef32e")
_GUID_EncoderQuality = _guid(0x1D5A4AE5, 0x1C74, 0x4C0C, "8a6eed54d583c058")


class _EncoderParameter(Structure):
    _fields_ = [("Guid", _GUID), ("NumberOfValues", c_ulong),
                ("Type", c_ulong), ("Value", c_void_p)]


class _EncoderParameters(Structure):
    _fields_ = [("Count", c_ulong), ("Parameter", _EncoderParameter * 1)]


PixelFormat24bppRGB = 0x0021808
UnitPixel = 2
InterpolationModeHighQualityBicubic = 7
TextRenderingHintAntiAliasGridFit = 4
EncoderParameterValueTypeLong = 4

_gdiplus.GdiplusStartup.argtypes = [POINTER(c_ulong), POINTER(_GdiplusStartupInput), c_void_p]
_gdiplus.GdiplusShutdown.argtypes = [c_ulong]
_gdiplus.GdipLoadImageFromFile.argtypes = [c_wchar_p, POINTER(c_void_p)]
_gdiplus.GdipGetImageWidth.argtypes = [c_void_p, POINTER(c_uint)]
_gdiplus.GdipGetImageHeight.argtypes = [c_void_p, POINTER(c_uint)]
_gdiplus.GdipCreateBitmapFromScan0.argtypes = [c_int, c_int, c_int, c_int, c_void_p, POINTER(c_void_p)]
_gdiplus.GdipGetImageGraphicsContext.argtypes = [c_void_p, POINTER(c_void_p)]
_gdiplus.GdipGraphicsClear.argtypes = [c_void_p, c_ulong]
_gdiplus.GdipSetInterpolationMode.argtypes = [c_void_p, c_int]
_gdiplus.GdipSetTextRenderingHint.argtypes = [c_void_p, c_int]
_gdiplus.GdipDrawImageRectRectI.argtypes = [c_void_p, c_void_p, c_int, c_int, c_int, c_int,
                                            c_int, c_int, c_int, c_int, c_int,
                                            c_void_p, c_void_p, c_void_p]
_gdiplus.GdipCreateFontFamilyFromName.argtypes = [c_wchar_p, c_void_p, POINTER(c_void_p)]
_gdiplus.GdipGetGenericFontFamilySansSerif.argtypes = [POINTER(c_void_p)]
_gdiplus.GdipCreateFont.argtypes = [c_void_p, c_float, c_int, c_int, POINTER(c_void_p)]
_gdiplus.GdipCreateSolidFill.argtypes = [c_ulong, POINTER(c_void_p)]
_gdiplus.GdipCreateStringFormat.argtypes = [c_int, c_ushort, POINTER(c_void_p)]
_gdiplus.GdipMeasureString.argtypes = [c_void_p, c_wchar_p, c_int, c_void_p, POINTER(_RectF),
                                       c_void_p, POINTER(_RectF), POINTER(c_int), POINTER(c_int)]
_gdiplus.GdipDrawString.argtypes = [c_void_p, c_wchar_p, c_int, c_void_p,
                                    POINTER(_RectF), c_void_p, c_void_p]
_gdiplus.GdipSaveImageToFile.argtypes = [c_void_p, c_wchar_p, POINTER(_GUID), c_void_p]
for _fn in ("GdipDisposeImage", "GdipDeleteGraphics", "GdipDeleteFont",
            "GdipDeleteFontFamily", "GdipDeleteBrush", "GdipDeleteStringFormat"):
    getattr(_gdiplus, _fn).argtypes = [c_void_p]


def _create_font_family():
    """按优先级创建字体族，找不到时回退到系统默认无衬线字体"""
    family = c_void_p()
    for name in ("SimHei", "Microsoft YaHei", "黑体"):
        if _gdiplus.GdipCreateFontFamilyFromName(name, None, byref(family)) == 0:
            return family
    if _gdiplus.GdipGetGenericFontFamilySansSerif(byref(family)) != 0:
        raise OSError("无法创建 GDI+ 字体族")
    return family


def _gdiplus_render(src_path, text, out_path, quality=95):
    """读取 src_path 图片，按 Windows 填充(Fill/cover)模式铺满屏幕并叠加水印文字，保存为 JPEG"""
    try:
        screen_w = _user32.GetSystemMetrics(0)
        screen_h = _user32.GetSystemMetrics(1)
    except Exception:
        screen_w, screen_h = 0, 0

    token = c_ulong(0)
    startup = _GdiplusStartupInput(1, None, 0, 0)
    status = _gdiplus.GdiplusStartup(byref(token), byref(startup), None)
    if status != 0:
        raise OSError("GdiplusStartup 失败，状态码 %d" % status)

    img = c_void_p()
    canvas = c_void_p()
    graphics = c_void_p()
    font_family = c_void_p()
    font = c_void_p()
    brush_black = c_void_p()
    brush_white = c_void_p()
    fmt = c_void_p()
    try:
        if _gdiplus.GdipLoadImageFromFile(src_path, byref(img)) != 0:
            raise FileNotFoundError("GDI+ 无法读取图片：%s" % src_path)

        w = c_uint()
        h = c_uint()
        _gdiplus.GdipGetImageWidth(img, byref(w))
        _gdiplus.GdipGetImageHeight(img, byref(h))
        iw, ih = w.value, h.value
        if iw <= 0 or ih <= 0:
            raise OSError("图片尺寸无效")
        if screen_w <= 0 or screen_h <= 0:
            screen_w, screen_h = iw, ih

        # 创建与屏幕分辨率相同的画布并填充黑色（等同 Fill 模式居中裁切铺满）
        if _gdiplus.GdipCreateBitmapFromScan0(screen_w, screen_h, 0, PixelFormat24bppRGB,
                                              None, byref(canvas)) != 0:
            raise OSError("创建画布失败")
        if _gdiplus.GdipGetImageGraphicsContext(canvas, byref(graphics)) != 0:
            raise OSError("创建绘图上下文失败")
        _gdiplus.GdipGraphicsClear(graphics, 0xFF000000)
        _gdiplus.GdipSetInterpolationMode(graphics, InterpolationModeHighQualityBicubic)

        # cover 裁切：按屏幕纵横比从原图居中裁出目标区域铺满画布
        screen_ratio = float(screen_w) / screen_h
        img_ratio = float(iw) / ih
        if img_ratio > screen_ratio:
            src_w, src_h = int(round(ih * screen_ratio)), ih
            src_x, src_y = (iw - src_w) // 2, 0
        else:
            src_w, src_h = iw, int(round(iw / screen_ratio))
            src_x, src_y = 0, (ih - src_h) // 2
        _gdiplus.GdipDrawImageRectRectI(graphics, img, 0, 0, screen_w, screen_h,
                                        src_x, src_y, src_w, src_h,
                                        UnitPixel, None, None, None)

        # 字体与画刷
        font_family = _create_font_family()
        font_size = max(12, int(screen_h / 50))
        min_font_size = 10
        _gdiplus.GdipCreateFont(font_family, c_float(font_size), 0, UnitPixel, byref(font))
        _gdiplus.GdipCreateSolidFill(0xFF000000, byref(brush_black))
        _gdiplus.GdipCreateSolidFill(0xFFFFFFFF, byref(brush_white))
        _gdiplus.GdipCreateStringFormat(0, 0, byref(fmt))
        _gdiplus.GdipSetTextRenderingHint(graphics, TextRenderingHintAntiAliasGridFit)

        # 测量文本宽度，确保不超出图片右侧边界，必要时逐步减小字体
        margin = int(screen_w * 0.05) if screen_w else 90
        layout_wide = 100000.0
        codepoints = c_int()
        lines = c_int()
        bound = _RectF()

        def measure():
            _gdiplus.GdipMeasureString(graphics, text, -1, font,
                                       byref(_RectF(0, 0, layout_wide, layout_wide)),
                                       fmt, byref(bound), byref(codepoints), byref(lines))
            return int(bound.Width)

        text_width = measure()
        x = screen_w - text_width - margin
        y = margin
        while x < 10 and font_size > min_font_size:
            _gdiplus.GdipDeleteFont(font)
            font = c_void_p()
            font_size = max(min_font_size, int(font_size * 0.9))
            _gdiplus.GdipCreateFont(font_family, c_float(font_size), 0, UnitPixel, byref(font))
            text_width = measure()
            x = screen_w - text_width - margin
        if x < 10:
            x = 10

        # 绘制带阴影并双次绘制白色水印以增加可读性和加深颜色
        shadow_offset = max(1, int(font_size / 20))
        layout = _RectF(float(x + shadow_offset), float(y + shadow_offset), layout_wide, layout_wide)
        _gdiplus.GdipDrawString(graphics, text, -1, font, byref(layout), fmt, brush_black)
        layout = _RectF(float(x), float(y), layout_wide, layout_wide)
        _gdiplus.GdipDrawString(graphics, text, -1, font, byref(layout), fmt, brush_white)
        _gdiplus.GdipDrawString(graphics, text, -1, font, byref(layout), fmt, brush_white)

        # 保存为 JPEG（指定质量，失败则按默认质量保存）
        quality_value = c_ulong(quality)
        params = _EncoderParameters()
        params.Count = 1
        params.Parameter[0].Guid = _GUID_EncoderQuality
        params.Parameter[0].NumberOfValues = 1
        params.Parameter[0].Type = EncoderParameterValueTypeLong
        params.Parameter[0].Value = cast(byref(quality_value), c_void_p)
        rc = _gdiplus.GdipSaveImageToFile(canvas, out_path, byref(_CLSID_JPEG), byref(params))
        if rc != 0:
            rc = _gdiplus.GdipSaveImageToFile(canvas, out_path, byref(_CLSID_JPEG), None)
        if rc != 0:
            raise OSError("保存图片失败，状态码 %d" % rc)

        return 0
    finally:
        for cleanup, obj in ((_gdiplus.GdipDeleteStringFormat, fmt),
                             (_gdiplus.GdipDeleteBrush, brush_white),
                             (_gdiplus.GdipDeleteBrush, brush_black),
                             (_gdiplus.GdipDeleteFont, font),
                             (_gdiplus.GdipDeleteFontFamily, font_family),
                             (_gdiplus.GdipDeleteGraphics, graphics),
                             (_gdiplus.GdipDisposeImage, canvas),
                             (_gdiplus.GdipDisposeImage, img)):
            if obj:
                try:
                    cleanup(obj)
                except Exception:
                    pass
        _gdiplus.GdiplusShutdown(token)


def create_watermark(info, options, remark=""):
    """将指定信息作为水印写入当前壁纸并设为桌面背景"""
    try:
        # 构建水印文本
        info_lines = []

        if options.get('hostname', True):
            info_lines.append("计算机名: %s" % info['Hostname'])

        if options.get('ip', True) and info['IPAddress']:
            info_lines.append("IP地址:   %s" % info['IPAddress'][0])
            info_lines.extend(["    %s" % ip for ip in info['IPAddress'][1:]])

            nat_ip = info.get('NATAddress')
            if nat_ip and nat_ip not in info['IPAddress']:
                info_lines.append("NAT地址:  %s" % nat_ip)

        if options.get('mac', True) and info['MACAddress']:
            info_lines.append("MAC地址:  %s" % info['MACAddress'][0])
            info_lines.extend(["    %s" % mac for mac in info['MACAddress'][1:]])

        if remark:
            remark_lines = remark.split('\n')
            info_lines.append("备注:     %s" % remark_lines[0])
            info_lines.extend(["    %s" % line for line in remark_lines[1:]])

        watermark_text = "\n".join(info_lines)
        if not watermark_text.strip():
            print("无有效信息可写入水印。")
            return

        # === 兼容 Win7 和 Win10/11 的壁纸路径 ===
        Userpath = info['Userpath']
        Wallpaperpath = os.path.join(Userpath, r"AppData\Roaming\Microsoft\Windows\Themes\TranscodedWallpaper")

        if not os.path.exists(Wallpaperpath):
            Wallpaperpath = Wallpaperpath + ".jpg"  # 尝试 Win7 路径

        if not os.path.exists(Wallpaperpath):
            raise FileNotFoundError(
                "未找到当前壁纸文件（检查是否为受支持的 Windows 版本）"
            )

        # 备份当前壁纸（保留带时间戳的历史备份并更新 latest 备份）
        try:
            backup_path = os.path.join(Userpath, "WallpaperTool")
            os.makedirs(backup_path, exist_ok=True)
            backup_file_latest = os.path.join(backup_path, "Wallpaper_Backup.jpg")
            shutil.copy2(Wallpaperpath, backup_file_latest)
        except Exception as e:
            # 备份失败时打印错误，但继续执行（不阻止主操作）
            print("备份当前壁纸失败：%s" % e)

        # 通过 GDI+ 生成水印图并保存
        output_path = os.path.join(Userpath, "WallpaperTool", "Wallpaper_Watermark.jpg")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        _gdiplus_render(Wallpaperpath, watermark_text, output_path)

        # 设置为桌面壁纸
        ctypes.windll.user32.SystemParametersInfoW(20, 0, output_path, 3)

    except Exception as e:
        print("处理图片时出现错误：%s" % str(e))


# ========================
# GUI 模式
# ========================
class WatermarkApp:
    def __init__(self, root):
        self.root = root
        self.root.title("水印设置")
        # 不设置固定尺寸，由内容自适应（固定像素尺寸在高分屏 + DPI 感知下会显示不全）

        self.ip_var = tk.BooleanVar(value=True)
        self.mac_var = tk.BooleanVar(value=True)
        self.hostname_var = tk.BooleanVar(value=True)

        cfg = _load_config()
        self.nat_var = tk.BooleanVar(value=bool(cfg.get("nat_enabled")))

        self.create_widgets(cfg)

    def create_widgets(self, cfg):
        # 复选框
        frame_checkboxes = tk.Frame(self.root)
        frame_checkboxes.pack(pady=20)

        tk.Checkbutton(frame_checkboxes, text="IP", variable=self.ip_var).pack(side="left", padx=20)
        tk.Checkbutton(frame_checkboxes, text="MAC", variable=self.mac_var).pack(side="left", padx=20)
        tk.Checkbutton(frame_checkboxes, text="主机名", variable=self.hostname_var).pack(side="left", padx=20)
        tk.Checkbutton(frame_checkboxes, text="NAT", variable=self.nat_var,
                       command=self._on_nat_toggle).pack(side="left", padx=20)

        # 备注输入
        frame_remark = tk.Frame(self.root)
        frame_remark.pack(pady=10, fill="x", padx=20)

        tk.Label(frame_remark, text="备注:").pack(side="left")
        self.remark_entry = tk.Entry(frame_remark)
        self.remark_entry.pack(side="left", fill="x", expand=True)

        # 按钮
        frame_buttons = tk.Frame(self.root)
        frame_buttons.pack(pady=20)

        tk.Button(frame_buttons, text="应用", command=self.apply_watermark).pack(side="left", padx=10)
        tk.Button(frame_buttons, text="清除", command=self.clear_all).pack(side="left", padx=10)
        tk.Button(frame_buttons, text="还原壁纸", command=self.restore_backup).pack(side="left", padx=10)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        status_frame = tk.Frame(self.root)
        status_frame.pack(fill="x", padx=(5, 0))

        tk.Label(status_frame, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)

        tk.Button(
            status_frame,
            text="关于",
            command=lambda: webbrowser.open("https://github.com/monstertsl/WallpaperTool"),
            fg="blue",
            bd=0,
            relief="flat"
        ).pack(side="right")

    def _on_nat_toggle(self):
        """勾选 NAT 时弹出设置窗口填写/修改接口地址与字段"""
        if self.nat_var.get():
            self._open_nat_dialog()

    def _open_nat_dialog(self):
        """NAT 接口设置弹窗：确定保存到配置文件，取消则取消勾选"""
        cfg = _load_config()
        dlg = tk.Toplevel(self.root)
        dlg.title("NAT 接口设置")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, text="接口地址:").grid(row=0, column=0, sticky="w", padx=(12, 4), pady=(12, 4))
        url_entry = tk.Entry(dlg, width=42)
        url_entry.insert(0, cfg.get("nat_api_url", ""))
        url_entry.grid(row=0, column=1, padx=(0, 12), pady=(12, 4))

        tk.Label(dlg, text="接口字段:").grid(row=1, column=0, sticky="w", padx=(12, 4), pady=4)
        field_entry = tk.Entry(dlg, width=42)
        field_entry.insert(0, cfg.get("nat_json_field", "ip"))
        field_entry.grid(row=1, column=1, padx=(0, 12), pady=4)

        tk.Label(dlg, text="返回 JSON 时填字段路径（支持 a.b.c 点路径）；返回纯文本 IP 则留空",
                 fg="gray").grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))

        frame_btn = tk.Frame(dlg)
        frame_btn.grid(row=3, column=0, columnspan=2, pady=(4, 12))

        def on_ok():
            _save_config({
                "nat_enabled": True,
                "nat_api_url": url_entry.get().strip(),
                "nat_json_field": field_entry.get().strip() or "ip",
            })
            dlg.destroy()

        def on_cancel():
            self.nat_var.set(False)
            dlg.destroy()

        tk.Button(frame_btn, text="确定", width=8, command=on_ok).pack(side="left", padx=8)
        tk.Button(frame_btn, text="取消", width=8, command=on_cancel).pack(side="left", padx=8)

        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry("+%d+%d" % (max(x, 0), max(y, 0)))

    def clear_all(self):
        self.ip_var.set(False)
        self.mac_var.set(False)
        self.hostname_var.set(False)
        self.remark_entry.delete(0, tk.END)

    def restore_backup(self):
        """将壁纸还原为备份的壁纸文件（覆盖系统当前壁纸）。"""
        backup_file = os.path.join(os.path.expanduser("~"), "WallpaperTool", "Wallpaper_Backup.jpg")
        if not os.path.exists(backup_file):
            messagebox.showwarning("还原失败", "未找到备份文件：%s" % backup_file)
            self.status_var.set("未找到备份")
            return

        try:
            # 直接调用 Windows API 设置壁纸
            ctypes.windll.user32.SystemParametersInfoW(20, 0, backup_file, 3)
        except Exception as e:
            messagebox.showerror("错误", "还原壁纸失败：%s" % e)
            self.status_var.set("出错")
            return
        # 成功还原后更新状态并启动倒计时退出
        self.status_var.set("已还原")
        messagebox.showinfo("还原壁纸", "已还原为原壁纸。")
        self.start_exit_countdown(5)

    def start_exit_countdown(self, seconds: int):
        """在状态栏显示倒计时并在结束后退出程序。"""
        try:
            seconds = int(seconds)
        except Exception:
            seconds = 5

        def tick(remaining):
            if remaining <= 0:
                try:
                    self.root.destroy()
                except Exception:
                    pass
                return
            # 更新状态栏，保留中文提示
            self.status_var.set("完成，%d 秒后退出" % remaining)
            self._countdown_id = self.root.after(1000, lambda: tick(remaining - 1))

        # 如果已有倒计时在运行，则取消它
        if hasattr(self, '_countdown_id') and self._countdown_id:
            try:
                self.root.after_cancel(self._countdown_id)
            except Exception:
                pass

        tick(seconds)

    def apply_watermark(self):
        # 更新状态并保存 NAT 开关（接口地址/字段由 NAT 设置弹窗维护）
        self.status_var.set("开始生成水印...")
        cfg = _load_config()
        cfg["nat_enabled"] = bool(self.nat_var.get())
        _save_config(cfg)
        thread = threading.Thread(target=self.generate_watermark)
        thread.daemon = True
        thread.start()

    def generate_watermark(self):
        try:
            info = get_network_info()
            options = {
                'ip': self.ip_var.get(),
                'mac': self.mac_var.get(),
                'hostname': self.hostname_var.get()
            }
            remark = self.remark_entry.get().strip()

            # 如果未勾选任何选项且备注为空，则提示并返回
            if not any([options['ip'], options['mac'], options['hostname']]) and not remark:
                # 1. 更新状态栏 (需回到主线程)
                self.root.after(0, lambda: self.status_var.set("错误：请至少勾选一项或填写备注"))
                # 2. 弹窗提示 (需回到主线程)
                self.root.after(0, lambda: messagebox.showwarning("提示", "请至少勾选一项信息（IP/MAC/主机名）或填写备注！"))
                # 3. 直接返回，不执行后续生成逻辑
                return

            # 显示NAT IP功能：勾选 NAT 时才处理；未填接口地址则提示；填了但获取失败则报错
            if options.get('ip', True) and self.nat_var.get():
                cfg = _load_config()
                nat_api_url = (cfg.get("nat_api_url") or "").strip()
                if not nat_api_url:
                    self.root.after(0, lambda: messagebox.showwarning("提示", "已勾选 NAT，请填写接口地址！"))
                elif info.get('NATAddress') is None:
                    error_msg = (
                        "获取 上网地址失败，请检查：\n"
                        "1. 本地网络是否正常；\n"
                        "2. 远端服务 %s 是否可访问。\n"
                        "3. JSON 字段 %r 是否正确。" % (nat_api_url, cfg.get("nat_json_field", "ip"))
                    )
                    self.root.after(0, lambda: messagebox.showinfo("错误", error_msg))
            create_watermark(info, options, remark)
        except Exception as e:
            # 在主线程中显示错误并更新状态
            self.root.after(0, lambda: messagebox.showerror("错误", "生成水印失败：%s" % e))
            self.root.after(0, lambda: self.status_var.set("出错"))
            return

        # 成功完成后在主线程启动 5 秒倒计时并退出程序
        self.root.after(0, lambda: self.start_exit_countdown(5))


# ========================
# 主程序入口
# ========================
def _set_process_dpi_aware():
    """启用 DPI 感知，保证高分屏下 GetSystemMetrics 返回真实像素（Win8.1+ 走 shcore，Win7 回退）"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Win8.1+
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # Vista/Win7
        except Exception:
            pass


if __name__ == "__main__":
    _set_process_dpi_aware()
    if '-q' in sys.argv or '/q' in sys.argv:
        # 静默模式：直接生成水印（全部信息，无备注）
        info = get_network_info()
        options = {'ip': True, 'mac': True, 'hostname': True}
        create_watermark(info, options)
    else:
        # GUI 模式
        root = tk.Tk()
        app = WatermarkApp(root)
        root.mainloop()
