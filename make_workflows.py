#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成可视化平台用的四份 ComfyUI API 格式 workflow JSON。
生图：Qwen-Image 2.1（已验证：/root/gen_cat_ref.py 的工作流）
视频：MiniMax H3 Ref2VA + Ref2V Turbo 8 步 LoRA（已验证：/root/submit_r2v.py 静音链路）
音频：Qwen3-TTS VoiceDesign 1.7B（FB_Qwen3TTSVoiceDesign + SaveAudio）+ 克隆音色
模板里的 {{PROMPT}} / {{TEXT}} 由 app.py 的 apply_params 替换。
注意：minimax_h3_upscale.json（超分）不在此生成，它是单独导出的后处理工作流；
重跑本脚本会覆盖其余四份，实例上手工调过的工作流先备份。
"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
WF_DIR = os.path.join(BASE, "workflows")

qwen_image = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1_bf16.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_bf16.safetensors", "type": "qwen_image"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{PROMPT}}", "clip": ["2", 0]}},
    "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
    "6": {"class_type": "EmptyLatentImage", "inputs": {"width": 1344, "height": 768, "batch_size": 1}},
    "7": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0], "seed": 777, "steps": 30, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "visual_image"}},
}

minimax_h3 = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_int8_convrot.safetensors"}},
    "4": {"class_type": "LoraLoader", "inputs": {"lora_name": "minimax_h3_ref2v_turbo_8step_v1.0_768p_bf16.safetensors", "strength_model": 1.0, "strength_clip": 1.0, "model": ["1", 0], "clip": ["2", 0]}},
    "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
    "6": {"class_type": "LoadImage", "inputs": {"image": "cat_ref.png"}},
    "7": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"clip": ["4", 1], "vae": ["3", 0], "prompt": "{{PROMPT}}", "width": 1344, "height": 768, "length": 124, "ref_image_size": "match", "ref_images": {"ref_image_0": ["6", 0]}}},
    "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["7", 0], "negative": ["5", 0], "latent_image": ["7", 1], "seed": 20261118, "steps": 8, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
    "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "audio": ["13", 0], "fps": 24}},
    "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "visual_video", "format": "auto", "codec": "auto"}},
    # 音频支路（2026-11-28 补）：H3 原生立体声，官方 R2V 模板同款接法。
    # 音频 VAE 文件 minimax_h3_audio_vae_fp32.safetensors 需在实例 models/vae/ 下，开机时核对。
    "12": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
    "13": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["12", 0]}},
}

# FL2VA 首帧/首尾帧生视频（2026-11-28 新增）：MiniMaxH3ImageToVideo 节点，
# first_frame 真首帧锁定（不是参考图）。需 fl2va 检查点 + fl2v turbo LoRA 两个文件，开机时核对。
minimax_h3_fl2va = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_int8_convrot.safetensors"}},
    "4": {"class_type": "LoraLoader", "inputs": {"lora_name": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors", "strength_model": 1.0, "strength_clip": 1.0, "model": ["1", 0], "clip": ["2", 0]}},
    "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
    "6": {"class_type": "LoadImage", "inputs": {"image": "cat_ref.png"}},
    "7": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"clip": ["4", 1], "vae": ["3", 0], "prompt": "{{PROMPT}}", "width": 1344, "height": 768, "length": 124, "first_frame": ["6", 0]}},
    "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["7", 0], "negative": ["5", 0], "latent_image": ["7", 1], "seed": 20261118, "steps": 8, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
    "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "audio": ["13", 0], "fps": 24}},
    "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "visual_video", "format": "auto", "codec": "auto"}},
    "12": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
    "13": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["12", 0]}},
}

qwen3_tts = {
    "1": {"class_type": "FB_Qwen3TTSVoiceDesign", "inputs": {"text": "{{TEXT}}", "instruct": "A young male voice speaking Mandarin Chinese, nervous and tense tone", "model_choice": "1.7B", "device": "auto", "precision": "bf16", "language": "Auto", "seed": 0}},
    "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0], "filename_prefix": "audio/visual_tts"}},
}

qwen3_tts_clone = {
    "1": {"class_type": "LoadAudio", "inputs": {"audio": ""}},
    "2": {"class_type": "FB_Qwen3TTSVoiceClone", "inputs": {"target_text": "{{TEXT}}", "ref_audio": ["1", 0], "ref_text": "", "model_choice": "1.7B", "device": "auto", "precision": "bf16", "language": "Auto", "seed": 0}},
    "3": {"class_type": "SaveAudio", "inputs": {"audio": ["2", 0], "filename_prefix": "audio/visual_tts_clone"}},
}

for name, wf in [("qwen_image.json", qwen_image), ("minimax_h3.json", minimax_h3), ("minimax_h3_fl2va.json", minimax_h3_fl2va), ("qwen3_tts.json", qwen3_tts), ("qwen3_tts_clone.json", qwen3_tts_clone)]:
    path = os.path.join(WF_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False, indent=2)
    print("wrote", path, len(wf), "nodes")
