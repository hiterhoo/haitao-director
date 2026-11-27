# workflows/：真实工作流放这里

部署后，从实例的 ComfyUI 里打开对应的工作流，选“导出（API 格式）”，
把 JSON 保存成下面三个文件名，放进这个目录，平台就走真实生成。
没放之前，平台只排队、不真生成，并会在任务里提示缺哪个文件。

- qwen_image.json — Qwen-Image 2.1 生图（文生图 / 图生图）
- minimax_h3.json — MiniMax H3 生视频（文生 / 图生 / 参考生 / 首尾帧）
- qwen3_tts.json — Qwen3-TTS 1.7B 配音

模板里提示词的位置建议写 {{PROMPT}}，后端会把页面上的提示词替换进去。
