#![windows_subsystem = "windows"]

use eframe::egui;
use image::{GenericImageView, Rgba};
use image::imageops::FilterType;
use imageproc::drawing::draw_text_mut;
use rusttype::{Font, Scale};
use std::env;
use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use windows::core::HSTRING;
use windows::Win32::UI::WindowsAndMessaging::{
    GetSystemMetrics, SystemParametersInfoW, SM_CXSCREEN, SM_CYSCREEN, SPIF_SENDWININICHANGE,
    SPIF_UPDATEINIFILE, SPI_SETDESKWALLPAPER,
};
use wmi::{COMLibrary, WMIConnection};
use serde::Deserialize;

// ========================
// 数据结构
// ========================

#[derive(Clone, Default)]
struct NetworkInfo {
    hostname: String, 
    ip_addresses: Vec<String>, 
    mac_addresses: Vec<String>, 
    user_path: String,
}

#[derive(Clone)]
struct WatermarkOptions { 
    show_ip: bool, 
    show_mac: bool, 
    show_hostname: bool, 
    remark: String 
}


// ========================
// 逻辑函数
// ========================

fn get_network_info() -> Result<NetworkInfo, Box<dyn std::error::Error>> {
    let com_con = COMLibrary::new()?;
    let wmi_con = WMIConnection::new(com_con)?;
    
    let user_path = env::var("USERPROFILE").unwrap_or_else(|_| "C:\\".to_string());
    let hostname = env::var("COMPUTERNAME").unwrap_or_else(|_| "Unknown".to_string());

    // --- 第一步：获取物理网卡的 MAC 地址 (对应 Python: MAC_adapters) ---
    // 查询所有网卡，筛选 PNPDeviceID 以 PCI 或 USB 开头的
    #[derive(Deserialize, Debug)]
    struct Win32NetworkAdapter {
        #[serde(rename = "MACAddress")]
        mac_address: Option<String>,
        #[serde(rename = "PNPDeviceID")]
        pnp_device_id: Option<String>,
    }

    let adapters: Vec<Win32NetworkAdapter> = wmi_con.raw_query(
        "SELECT MACAddress, PNPDeviceID FROM Win32_NetworkAdapter"
    )?;

    // 构建物理网卡 MAC 集合 (HashSet 查找更快)
    let mut physical_macs = std::collections::HashSet::new();
    for adapter in adapters {
        if let (Some(mac), Some(pnp)) = (adapter.mac_address, adapter.pnp_device_id) {
            if pnp.starts_with("PCI") || pnp.starts_with("USB") {
                physical_macs.insert(mac);
            }
        }
    }

    // --- 第二步：获取已启用物理网卡的 IP 和 MAC (对应 Python: ip_adapters) ---
    #[derive(Deserialize, Debug)]
    struct Win32NetworkAdapterConfiguration {
        #[serde(rename = "MACAddress")]
        mac_address: Option<String>,
        #[serde(rename = "IPEnabled")]
        ip_enabled: Option<bool>,
        #[serde(rename = "IPAddress")]
        ip_address: Option<Vec<String>>,
    }

    let configs: Vec<Win32NetworkAdapterConfiguration> = wmi_con.raw_query(
        "SELECT MACAddress, IPEnabled, IPAddress FROM Win32_NetworkAdapterConfiguration"
    )?;

    let mut valid_macs = Vec::new();
    let mut valid_ips = Vec::new();

    for config in configs {
        // 核心筛选逻辑：
        // 1. MAC 地址必须存在
        // 2. 必须在物理网卡列表中 (physical_macs)
        // 3. 必须已启用 (IPEnabled == true)
        if let Some(mac) = config.mac_address {
            if physical_macs.contains(&mac) && config.ip_enabled.unwrap_or(false) {
                // 提取 IP
                if let Some(ips) = config.ip_address {
                    for ip in ips {
                        // 排除 fe80 开头的 IPv6 链路本地地址
                        if !ip.starts_with("fe80") {
                            valid_ips.push(ip.clone());
                        }
                    }
                }
                // 记录有效的 MAC (后续去重)
                valid_macs.push(mac);
            }
        }
    }

    // 去重并排序 (保持输出稳定)
    valid_ips.sort();
    valid_ips.dedup();
    valid_macs.sort();
    valid_macs.dedup();

    // 兜底提示：如果没找到，显示提示文字而不是空白
    if valid_ips.is_empty() { 
        valid_ips.push("未发现活跃IP".to_string()); 
    }
    if valid_macs.is_empty() { 
        valid_macs.push("未发现活跃MAC".to_string()); 
    }

    Ok(NetworkInfo {
        hostname,
        ip_addresses: valid_ips,
        mac_addresses: valid_macs,
        user_path,
    })
}

fn set_wallpaper(path: &std::path::Path) -> Result<(), Box<dyn std::error::Error>> {
    let full_path = fs::canonicalize(path)?;
    let path_str = full_path.to_str()
        .ok_or("路径包含非法字符")?
        .trim_start_matches(r"\\?\")
        .to_string();
    
    let path_hstring = HSTRING::from(&path_str);
    
    unsafe {
        SystemParametersInfoW(
            SPI_SETDESKWALLPAPER,
            0,
            Some(path_hstring.as_ptr() as *const _ as *mut _), 
            SPIF_UPDATEINIFILE | SPIF_SENDWININICHANGE,
        )?;
    }
    Ok(())
}

fn create_watermark(info: &NetworkInfo, options: &WatermarkOptions, font: &Font) -> Result<(), Box<dyn std::error::Error>> {
    // 1. 获取屏幕分辨率
    let screen_w = unsafe { GetSystemMetrics(SM_CXSCREEN) } as u32;
    let screen_h = unsafe { GetSystemMetrics(SM_CYSCREEN) } as u32;

    // 2. 路径准备
    let user_path = if info.user_path.is_empty() {
        env::var("USERPROFILE").unwrap_or_else(|_| r"C:\".to_string())
    } else {
        info.user_path.clone()
    };

    let theme_dir = PathBuf::from(&user_path).join(r"AppData\Roaming\Microsoft\Windows\Themes");
    let wallpaper_path = theme_dir.join("TranscodedWallpaper");
    let output_dir = PathBuf::from(&user_path).join("WallpaperTool");
    let output_path = output_dir.join("Wallpaper_Watermark.jpg");
    let backup_path = output_dir.join("Wallpaper_Backup.jpg");

    if !output_dir.exists() {
        fs::create_dir_all(&output_dir)?;
    }

    // 3. 备份逻辑
    if !backup_path.exists() && wallpaper_path.exists() {
        let _ = fs::copy(&wallpaper_path, &backup_path);
    }

    // 优先从备份读取，避免文件锁定问题
    let source_path = if backup_path.exists() { &backup_path } else { &wallpaper_path };
    let img_data = fs::read(source_path).map_err(|e| {
        format!("无法读取壁纸源文件：{}。请确保您当前使用的是图片壁纸。", e)
    })?;

    // 4. 准备文本行
    let mut lines = Vec::new();
    if options.show_hostname {
        lines.push(format!("计算机名：{}", info.hostname));
    }
    if options.show_ip {
        for (i, ip) in info.ip_addresses.iter().enumerate() {
            lines.push(if i == 0 { format!("IP 地址： {}", ip) } else { format!("          {}", ip) });
        }
    }
    if options.show_mac {
        for (i, mac) in info.mac_addresses.iter().enumerate() {
            lines.push(if i == 0 { format!("MAC 地址：{}", mac) } else { format!("          {}", mac) });
        }
    }
    if !options.remark.trim().is_empty() {
        for (i, line) in options.remark.lines().enumerate() {
            lines.push(if i == 0 { format!("备注：    {}", line) } else { format!("          {}", line) });
        }
    }

    // 5. 图片处理逻辑 (Cover/填充 模式)
    let img = image::load_from_memory(&img_data)?;
    let (img_w, img_h) = img.dimensions();

    if img_w == 0 || img_h == 0 {
        return Err("原壁纸尺寸无效".into());
    }

    // 计算填充缩放比例 (确保覆盖整个屏幕，可能会裁切)
    let scale = f32::max(screen_w as f32 / img_w as f32, screen_h as f32 / img_h as f32);
    let new_w = (img_w as f32 * scale) as u32;
    let new_h = (img_h as f32 * scale) as u32;

    // 使用 Triangle 进行快速缩放
    let img_resized = img.resize_exact(new_w, new_h, FilterType::Triangle);

    // 创建与屏幕分辨率相同的画布
    let mut canvas = image::RgbaImage::new(screen_w, screen_h);
    
    // 计算居中偏移 (等同于 Windows 的填充模式)
    let offset_x = (screen_w as i64 - new_w as i64) / 2;
    let offset_y = (screen_h as i64 - new_h as i64) / 2;
    
    // 将缩放后的图粘贴到画布上 (超出部分自动裁切)
    image::imageops::overlay(&mut canvas, &img_resized.to_rgba8(), offset_x, offset_y);

    // 6. 计算字号与位置 (还原 Python 逻辑)
    // 字体大小按屏幕高度缩放，确保在高分辨率下仍然可见
    let font_size = (screen_h as f32 / 40.0).max(14.0).round(); 
    let scale_font = Scale::uniform(font_size);
    
    // 边距计算 (基于屏幕宽度/高度)
    let margin_right = (screen_w as f32 * 0.05) as i32;
    let margin_top = (screen_h as f32 * 0.05) as i32;

    // 动态计算最长行宽度以对齐右侧
    let mut max_w = 0.0;
    for line in &lines {
        let glyphs: Vec<_> = font.layout(line, scale_font, rusttype::point(0.0, 0.0)).collect();
        let w = glyphs.iter().rev().next()
            .map(|g| g.position().x + g.unpositioned().h_metrics().advance_width)
            .unwrap_or(0.0);
        if w > max_w { max_w = w; }
    }
    
    // 确保起始位置不小于 10 (防止超出左边界)
    let start_x = ((screen_w as f32 - max_w - margin_right as f32) as i32).max(10);

    // 7. 绘制文本
    for (i, line) in lines.iter().enumerate() {
        let y = margin_top + (i as f32 * font_size * 1.3) as i32;
        
        // 阴影 (黑色，半透明)
        draw_text_mut(&mut canvas, Rgba([0, 0, 0, 180]), start_x + 2, y + 2, scale_font, font, line);
        // 主文字 (白色)
        draw_text_mut(&mut canvas, Rgba([255, 255, 255, 255]), start_x, y, scale_font, font, line);
    }

    // 8. 安全保存逻辑
    {
        let file = fs::File::create(&output_path)?;
        let writer = std::io::BufWriter::with_capacity(512 * 1024, file);
        let mut jpeg_enc = image::codecs::jpeg::JpegEncoder::new_with_quality(writer, 95);
        jpeg_enc.encode_image(&canvas)?;
    } 

    // 9. 应用壁纸
    set_wallpaper(&output_path)?;
    
    Ok(())
}
// ========================
// GUI 应用
// ========================

struct WatermarkApp {
    options: WatermarkOptions,
    status: Arc<Mutex<String>>,
    drawing_font: Arc<Font<'static>>,
    exit_timer: Option<Instant>,
    network_info: NetworkInfo, // 现在这个字段会被真正使用了
}

impl WatermarkApp {
    fn new(font: Arc<Font<'static>>) -> Self {
        let mut info = get_network_info().unwrap_or_else(|_| {
            NetworkInfo {
                user_path: env::var("USERPROFILE").unwrap_or_default(),
                hostname: env::var("COMPUTERNAME").unwrap_or_default(),
                ..Default::default()
            }
        });

        if info.user_path.is_empty() {
            info.user_path = env::var("USERPROFILE").unwrap_or_else(|_| r"C:\".to_string());
        }

        Self {
            options: WatermarkOptions { show_ip: true, show_mac: true, show_hostname: true, remark: String::new() },
            status: Arc::new(Mutex::new("就绪".to_string())),
            drawing_font: font,
            exit_timer: None,
            network_info: info,
        }
    }
}

impl eframe::App for WatermarkApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        {
            let current_status = self.status.lock().unwrap();
            if (*current_status == "SUCCESS" || *current_status == "RESTORE_SUCCESS") && self.exit_timer.is_none() {
                self.exit_timer = Some(Instant::now());
            }
        }

        let mut display_msg = self.status.lock().unwrap().clone();
        
        if let Some(start_time) = self.exit_timer {
            let elapsed = start_time.elapsed().as_secs();
            if elapsed >= 5 {
                ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            } else {
                ctx.request_repaint_after(Duration::from_millis(100));
                if display_msg == "SUCCESS" || display_msg == "RESTORE_SUCCESS" {
                    let prefix = if display_msg == "SUCCESS" { "应用成功！" } else { "还原成功！" };
                    display_msg = format!("{} {} 秒后自动退出...", prefix, 5 - elapsed);
                }
            }
        }

        egui::CentralPanel::default()
            .show(ctx, |ui| {
            ui.add_space(5.0);
            ui.horizontal(|ui| {
                ui.checkbox(&mut self.options.show_ip, "IP 地址");
                ui.checkbox(&mut self.options.show_mac, "MAC 地址");
                ui.checkbox(&mut self.options.show_hostname, "主机名");
            });

            ui.add_space(5.0);
            ui.horizontal(|ui| {
                ui.label("备注:");
                ui.text_edit_singleline(&mut self.options.remark);
            });

            ui.add_space(10.0);
            ui.horizontal(|ui| {
                if ui.button("应用").clicked() {
                    self.exit_timer = None;
                    let status = Arc::clone(&self.status);
                    let opts = self.options.clone();
                    let font_clone = Arc::clone(&self.drawing_font);
                    let ctx_clone = ctx.clone();
                    
                    // 🚀 核心优化：检查预加载数据是否有效
                    // 如果预加载的 IP 或 MAC 不为空（且不是提示文字），则直接使用，跳过 WMI 查询（极速）
                    let has_valid_data = self.network_info.ip_addresses.iter().any(|ip| !ip.contains("未发现"))
                                      || self.network_info.mac_addresses.iter().any(|mac| !mac.contains("未发现"));
                    
                    // 克隆当前数据供线程使用
                    let cached_info = self.network_info.clone();

                    thread::spawn(move || {
                        let final_info;

                        if has_valid_data {
                            // 情况 A: 数据有效，直接使用预加载 (耗时 < 50ms)
                            *status.lock().unwrap() = "正在处理...".to_string();
                            final_info = cached_info;
                        } else {
                            // 情况 B: 数据无效（启动时 WMI 失败），重新查询 (耗时 ~1s)
                            *status.lock().unwrap() = "正在刷新网络信息...".to_string();
                            ctx_clone.request_repaint();
                            
                            // 重新查询，如果失败则降级使用缓存（至少显示主机名）
                            final_info = get_network_info().unwrap_or(cached_info);
                            
                            *status.lock().unwrap() = "正在处理...".to_string();
                            ctx_clone.request_repaint();
                        }

                        match create_watermark(&final_info, &opts, &font_clone) {
                            Ok(_) => *status.lock().unwrap() = "SUCCESS".to_string(),
                            Err(e) => *status.lock().unwrap() = format!("失败：{}", e),
                        }
                        ctx_clone.request_repaint();
                    });
                }

                if ui.button("清除").clicked() {
                    self.options.show_ip = false; 
                    self.options.show_mac = false; 
                    self.options.show_hostname = false;
                    self.options.remark.clear(); 
                    self.exit_timer = None;
                    *self.status.lock().unwrap() = "配置已重置".to_string();
                }

                if ui.button("还原").clicked() {
                    let user_path = env::var("USERPROFILE").unwrap_or_default();
                    let backup = PathBuf::from(user_path).join("WallpaperTool").join("Wallpaper_Backup.jpg");
                    if backup.exists() {
                        if set_wallpaper(&backup).is_ok() { 
                            *self.status.lock().unwrap() = "RESTORE_SUCCESS".to_string();
                            self.exit_timer = Some(Instant::now());
                        }
                    }
                }
            });
            
            ui.add_space(8.0);
            ui.separator();
            
            ui.horizontal(|ui| {
                ui.label(egui::RichText::new(display_msg)
                    .size(12.0)
                    .color(egui::Color32::from_rgb(160, 160, 160)));

                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.spacing_mut().item_spacing.x = 0.0;
                    ui.hyperlink_to(
                        egui::RichText::new("关于").size(12.0), 
                        "https://github.com/monstertsl/WallpaperTool"
                    );
                });
            });
        });
    }
}

fn main() -> Result<(), eframe::Error> {
    

    unsafe {
        let _ = windows::Win32::UI::HiDpi::SetProcessDpiAwarenessContext(
            windows::Win32::UI::HiDpi::DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        );
    }


    let font_data = fs::read("C:\\Windows\\Fonts\\msyh.ttc")
        .or_else(|_| fs::read("C:\\Windows\\Fonts\\simhei.ttf"))
        .expect("Font not found");
    let drawing_font = Arc::new(Font::try_from_vec(font_data.clone()).unwrap());

    let icon_data = include_bytes!("./ip.png"); 
    let icon = image::load_from_memory(icon_data)
        .expect("Failed to load icon")
        .to_rgba8();
    let (icon_width, icon_height) = icon.dimensions();
    
    let window_icon = egui::IconData {
        rgba: icon.into_raw(),
        width: icon_width,
        height: icon_height,
    };

    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([350.0, 140.0]) 
            .with_resizable(false)
            .with_icon(window_icon), 
        ..Default::default()
    };

    eframe::run_native("壁纸水印工具", options, Box::new(move |cc| {
        let mut fonts = egui::FontDefinitions::default();
        fonts.font_data.insert("font".to_owned(), egui::FontData::from_owned(font_data));
        fonts.families.get_mut(&egui::FontFamily::Proportional).unwrap().insert(0, "font".to_owned());
        fonts.families.get_mut(&egui::FontFamily::Monospace).unwrap().insert(0, "font".to_owned());
        cc.egui_ctx.set_fonts(fonts);
        Box::new(WatermarkApp::new(drawing_font))
    }))
}