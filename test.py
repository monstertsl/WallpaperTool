from PIL import Image, ImageDraw, ImageFont
import os,wmi,ctypes

Userpath= os.path.expanduser("~")
Wallpaperpath = os.path.join(Userpath, r"AppData\Roaming\Microsoft\Windows\Themes\TranscodedWallpaper")

#判断是否是win7系统
if os.path.exists(Wallpaperpath):
    pass
else:
    Wallpaperpath = Wallpaperpath + ".jpg"

Hostname = os.getenv('computername')

Wmi = wmi.WMI()
# 筛选物理网卡获取MAC
MAC_adapters = Wmi.Win32_NetworkAdapter()
MACAddress = [
    adapter.MACAddress for adapter in MAC_adapters if adapter.PNPDeviceID and (
        adapter.PNPDeviceID.startswith("PCI") or adapter.PNPDeviceID.startswith("USB"))
]

# 筛选获取在线物理网卡信息
ip_adapters = Wmi.Win32_NetworkAdapterConfiguration()
MAClist = [
    mac.MACAddress for mac in ip_adapters if mac.MACAddress in MACAddress and mac.IPEnabled
]

IPAddress = []
for ip in ip_adapters:
    if ip.MACAddress in MAClist:
        for ip in ip.IPAddress :
            if not ip.startswith("fe80"):
                IPAddress.append(ip.strip())

print(IPAddress)
print(MAClist)