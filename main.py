from PIL import Image, ImageDraw, ImageFont
import sys,threading,os,wmi,ctypes
import shutil
import pythoncom
import tkinter as tk
from tkinter import messagebox


def get_network_info():
    """获取主机名、IP、MAC地址等信息"""
    Userpath = os.path.expanduser("~")
    Hostname = os.getenv('computername')
    wmi_obj = wmi.WMI()

    # 获取物理网卡 MAC 地址
    MAC_adapters = wmi_obj.Win32_NetworkAdapter()
    MACAddress = [
        adapter.MACAddress for adapter in MAC_adapters
        if adapter.PNPDeviceID and (
            adapter.PNPDeviceID.startswith("PCI") or adapter.PNPDeviceID.startswith("USB"))
    ]

    # 获取已启用的物理网卡 IP 配置
    ip_adapters = wmi_obj.Win32_NetworkAdapterConfiguration()
    MAClist = [
        mac.MACAddress for mac in ip_adapters
        if mac.MACAddress in MACAddress and mac.IPEnabled
    ]

    IPAddress = []
    for ip in ip_adapters:
        if ip.MACAddress in MAClist:
            for ip_addr in ip.IPAddress or []:
                if not ip_addr.startswith("fe80"):  # 排除 IPv6 链路本地地址
                    IPAddress.append(ip_addr.strip())

    return {
        'Hostname': Hostname,
        'IPAddress': IPAddress,
        'MACAddress': MAClist,
        'Userpath': Userpath
    }


def create_watermark(info, options, remark=""):
    """将指定信息作为水印写入当前壁纸并设为桌面背景"""
    try:
        # 构建水印文本
        info_lines = []

        if options.get('hostname', True):
            info_lines.append(f"计算机名: {info['Hostname']}")

        if options.get('ip', True) and info['IPAddress']:
            info_lines.append(f"IP地址:   {info['IPAddress'][0]}")
            info_lines.extend([f"    {ip}" for ip in info['IPAddress'][1:]])

        if options.get('mac', True) and info['MACAddress']:
            info_lines.append(f"MAC地址:  {info['MACAddress'][0]}")
            info_lines.extend([f"    {mac}" for mac in info['MACAddress'][1:]])

        if remark:
            remark_lines = remark.split('\n')
            info_lines.append(f"备注:     {remark_lines[0]}")
            info_lines.extend([f"    {line}" for line in remark_lines[1:]])

        watermark_text = "\n".join(info_lines)
        if not watermark_text.strip():
            print("无有效信息可写入水印。")
            return

        # === 关键：兼容 Win7 和 Win10/11 的壁纸路径 ===
        Userpath = info['Userpath']
        Wallpaperpath = os.path.join(Userpath, r"AppData\Roaming\Microsoft\Windows\Themes\TranscodedWallpaper")

        if not os.path.exists(Wallpaperpath):
            Wallpaperpath = Wallpaperpath + ".jpg"  # 尝试 Win7 路径

        if not os.path.exists(Wallpaperpath):
            raise FileNotFoundError(
                "未找到当前壁纸文件（检查是否为受支持的 Windows 版本）"
            )
        
        # 备份当前壁纸（使用文件拷贝，保留带时间戳的历史备份并更新 latest 备份）
        try:
            backup_path = os.path.join(Userpath, "WallpaperTool")
            os.makedirs(backup_path, exist_ok=True)
            backup_file_latest = os.path.join(backup_path, "Wallpaper_Backup.jpg")
            # 仅保留最新备份，直接覆盖同名文件
            shutil.copy2(Wallpaperpath, backup_file_latest)
        except Exception as e:
            # 备份失败时打印错误，但继续执行（不阻止主操作）
            print(f"备份当前壁纸失败：{e}")

        # 打开并处理壁+按 Windows "填充(Fill/cover)" 模式生成与屏幕分辨率相同的画布
        img = Image.open(Wallpaperpath)
        width, height = img.size
        try:
            screen_w = ctypes.windll.user32.GetSystemMetrics(0)
            screen_h = ctypes.windll.user32.GetSystemMetrics(1)
        except Exception:
            screen_w, screen_h = width, height

        # 计算 cover 缩放比例（确保图片覆盖整个屏幕，可能会裁切）
        scale = max(screen_w / width, screen_h / height)
        new_w = int(width * scale)
        new_h = int(height * scale)
        img_resized = img.resize((new_w, new_h), Image.LANCZOS)

        # 在与屏幕分辨率相同的画布上居中粘贴已缩放图片（等同于 Windows 的填充模式）
        canvas = Image.new('RGB', (screen_w, screen_h), (0, 0, 0))
        left = (screen_w - new_w) // 2
        top = (screen_h - new_h) // 2
        canvas.paste(img_resized, (left, top))
        img = canvas
        # 在图片上添加水印
        draw = ImageDraw.Draw(img)

        # 字体大小按屏幕高度缩放，确保在高分辨率下仍然可见
        font_size = max(12, int(screen_h / 50))
        try:
            font = ImageFont.truetype("simhei.ttf", font_size)
        except OSError:
            font = ImageFont.load_default()

        # 计算文本尺寸并确保其不会超出图片右侧边界，必要时减小字体
        text_bbox = draw.textbbox((0, 0), watermark_text, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        margin = int(screen_w * 0.05) if screen_w else 90
        # 默认放在右上角，但基于画布（屏幕）坐标计算，保证在 Fill 模式下不会被裁切
        x = img.width - text_width - margin
        y = margin

        # 若文本宽度过大，则逐步减小字体直到适配或到达最小字体
        min_font_size = 10
        while x < 10 and font_size > min_font_size:
            font_size = max(min_font_size, int(font_size * 0.9))
            try:
                font = ImageFont.truetype("simhei.ttf", font_size)
            except OSError:
                font = ImageFont.load_default()
            text_bbox = draw.textbbox((0, 0), watermark_text, font=font)
            text_width = text_bbox[2] - text_bbox[0]
            x = img.width - text_width - margin

        # 确保 x 不为负
        if x < 10:
            x = 10

        # 绘制带阴影并双次绘制白色水印以增加可读性和加深颜色
        shadow_offset = max(1, int(font_size / 20))
        draw.text((x + shadow_offset, y + shadow_offset), watermark_text, font=font, fill=(0, 0, 0))
        # 第一次白色绘制
        draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255))
        # 第二次白色绘制，叠加以加深颜色
        draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255))

        # 保存
        img_path = os.path.join(Userpath, "WallpaperTool")
        os.makedirs(img_path, exist_ok=True)
        output_path = os.path.join(img_path, "Wallpaper_Watermark.jpg")
        img.save(output_path, quality=95)

        # 设置为桌面壁纸
        ctypes.windll.user32.SystemParametersInfoW(20, 0, output_path, 3)

    except Exception as e:
        print(f"处理图片时出现错误：{str(e)}")


# ========================
# GUI 模式
# ========================
class WatermarkApp:
    def __init__(self, root):
        self.root = root
        self.root.title("水印设置")
        self.root.geometry("400x200")

        self.ip_var = tk.BooleanVar(value=True)
        self.mac_var = tk.BooleanVar(value=True)
        self.hostname_var = tk.BooleanVar(value=True)

        self.create_widgets()

    def create_widgets(self):
        # 复选框
        frame_checkboxes = tk.Frame(self.root)
        frame_checkboxes.pack(pady=20)

        tk.Checkbutton(frame_checkboxes, text="IP", variable=self.ip_var).pack(side="left", padx=20)
        tk.Checkbutton(frame_checkboxes, text="MAC", variable=self.mac_var).pack(side="left", padx=20)
        tk.Checkbutton(frame_checkboxes, text="主机名", variable=self.hostname_var).pack(side="left", padx=20)

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
        status_frame.pack(fill="x", padx=10)
        tk.Label(status_frame, textvariable=self.status_var, anchor="w").pack(fill="x")

    def clear_all(self):
        self.ip_var.set(False)
        self.mac_var.set(False)
        self.hostname_var.set(False)
        self.remark_entry.delete(0, tk.END)

    def restore_backup(self):
        """将壁纸还原为备份的壁纸文件（覆盖系统当前壁纸）。"""
        backup_file = os.path.join(os.path.expanduser("~"), "WallpaperTool", "Wallpaper_Backup.jpg")
        if not os.path.exists(backup_file):
            messagebox.showwarning("还原失败", f"未找到备份文件：{backup_file}")
            self.status_var.set("未找到备份")
            return

        try:
            # 直接调用 Windows API 设置壁纸
            ctypes.windll.user32.SystemParametersInfoW(20, 0, backup_file, 3)
        except Exception as e:
            messagebox.showerror("错误", f"还原壁纸失败：{e}")
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
            self.status_var.set(f"完成，{remaining} 秒后退出")
            self._countdown_id = self.root.after(1000, lambda: tick(remaining - 1))

        # 如果已有倒计时在运行，则取消它
        if hasattr(self, '_countdown_id') and self._countdown_id:
            try:
                self.root.after_cancel(self._countdown_id)
            except Exception:
                pass

        tick(seconds)

    def apply_watermark(self):
        # 更新状态并启动后台线程
        self.status_var.set("开始生成水印...")
        thread = threading.Thread(target=self.generate_watermark)
        thread.daemon = True
        thread.start()

    def generate_watermark(self):
        try:
            # 在新的线程中使用 WMI/COM 对象前必须初始化 COM
            pythoncom.CoInitialize()
            try:
                info = get_network_info()
                options = {
                    'ip': self.ip_var.get(),
                    'mac': self.mac_var.get(),
                    'hostname': self.hostname_var.get()
                }
                remark = self.remark_entry.get().strip()
                create_watermark(info, options, remark)
            finally:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
        except Exception as e:
            # 在主线程中显示错误并更新状态
            self.root.after(0, lambda: messagebox.showerror("错误", f"生成水印失败：{e}"))
            self.root.after(0, lambda: self.status_var.set("出错"))
            return

        # 成功完成后在主线程启动 5 秒倒计时并退出程序
        self.root.after(0, lambda: self.start_exit_countdown(5))


# ========================
# 主程序入口
# ========================
if __name__ == "__main__":
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