fn main() {
    // 第二个参数使用 NONE 或空数组，代表不需要额外的宏定义
    embed_resource::compile("icon.rc", embed_resource::NONE);
}