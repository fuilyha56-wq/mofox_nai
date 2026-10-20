# MoFox NAI 快速示例

1. 在 MoFox 启用 `mofox_nai`，在 `[generation]` 中填写 OpenAI 兼容生图接口。
2. 在聊天中发送 `/nai 一只橘猫坐在窗台上看雨，柔和的傍晚光线`。
3. 对聊天中的一张图片发送回复，再发送 `/nai 反推`，机器人会把 Tag 作为引用回复发出。
4. 发送 `/nai 历史 20` 查看本聊天已归档图片，再用 `/nai 查看 12` 重新发送记录 `#12`。
5. 配置视觉模型、嵌入模型和 Qdrant 后，可让 LLM 调用 `search_image_database` 做语义检索；`search_danbooru` 可查找安全 Tag 示例。

直接命令示例：

```text
/nai 1girl, long_hair, blue_eyes, cherry_blossoms, sunset, best quality
/nai 生图 雨夜街道上的霓虹灯倒映在水洼中，电影感构图
/nai 反推
/nai 搜索 樱花 黄昏
```

