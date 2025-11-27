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
'''
#获取物理网卡获取IP
ip_adapters = Wmi.Win32_NetworkAdapterConfiguration()
for ip in ip_adapters:
    if ip.MACAddress in (MACAddress):
        if ip.IPAddress != None:
            IP_Address = re.sub(r'^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$', '', str(ip.IPAddress))
            IPAddress.append(IP_Address)
'''

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

'''
# 输出计算机名
print(f"计算机名：\t{Hostname}")

# 输出IP地址
if IPAddress:
    print(f"IP地址：\t{IPAddress[0]}")
    for ip in IPAddress[1:]:
        print(f"\t\t{ip}")
else:
    print("IP地址：\t")  # 处理无IP情况

# 输出MAC地址
if MACAddress:
    print(f"MAC地址：\t{MACAddress[0]}")
    for mac in MACAddress[1:]:
        print(f"\t\t{mac}")
else:
    print("MAC地址：\t")  # 处理无MAC情况
'''

# 构建水印文本 
info_lines = [
    f"计算机名: {Hostname}",
    f"IP地址:   {IPAddress[0]}"] + [f"    {ip}" for ip in IPAddress[1:]] + [
    f"MAC地址:  {MAClist[0]}"] + [f"    {mac}" for mac in MAClist[1:]]

watermark_text = "\n".join(info_lines)

# 定义固定宽度，确保标签后的内容对齐

# 添加水印到图片 
try:
    # 打开壁纸图片
    img = Image.open(Wallpaperpath)
    width, height = img.size
    # 调整图片大小
    img = img.resize((int(width*1080/height),1080))
    # 创建绘图对象
    draw = ImageDraw.Draw(img)
    
    # 设置字体（尝试加载系统字体）
    try:
        font = ImageFont.truetype("simhei.ttf", 22)
    except:
        font = ImageFont.load_default()   

    # 计算文本尺寸和位置
    text_bbox = draw.textbbox((0, 0), watermark_text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    
    margin = 90  # 边距
    x = img.width - text_width - margin  
    y = margin
    '''
    # 添加半透明背景
    background_bbox = (
        x - 5, y - 5,
        x + text_width + 5,
        y + text_height + 5
    )
    draw.rectangle(background_bbox, fill=(0, 0, 0, 128))  # 半透明黑色
    '''
    # 绘制白色水印
    draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255))
    #再次绘制水印
    draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255))

    #预览水印图片
    #img.show()

    # 保存到指定位置
    img_path = os.path.join(Userpath, "IpWallpaper")
    if not os.path.exists(img_path):
        os.makedirs(img_path)        
    output_path = os.path.join(img_path, f"Wallpaper_Watermark.jpg")
    
    img.save(output_path, quality=95)
    
    #print(f"水印图片已保存到：{output_path}")

except Exception as e:
    print(f"处理图片时出现错误：{str(e)}")

#修改壁纸
ctypes.windll.user32.SystemParametersInfoW(20, 0, output_path, 3)