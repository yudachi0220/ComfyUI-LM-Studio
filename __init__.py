import torch
import numpy as np
from PIL import Image
import io
import base64
import requests
import json
import copy
import subprocess
import sys
import os

# =================================================================================
# Helper functions and classes
# =================================================================================

class Message:
    """A simple wrapper for the message list to be passed between nodes."""
    def __init__(self, messages=None):
        self.messages = messages if messages is not None else []
    def add_message(self, role, content):
        self.messages.append({"role": role, "content": content})
    def get_messages(self):
        return self.messages

# =================================================================================
# 已移除独立的 API Config 和 Select Model 节点，功能已整合到 LMStudio Request 中
# =================================================================================

# =================================================================================
# 节点 1: 综合提示词（整合了系统提示词和用户提示词，支持多图片）
# =================================================================================

class LMS_UserPrompt:
    """
    👤 综合提示词节点
    创建包含系统提示词和用户提示词的完整对话消息，支持单张或多张图片。
    可用于开始新对话或继续多轮对话。
    """
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "system_prompt": ("STRING", {"multiline": True, "default": "你是一个有帮助的助手。", "dynamicPrompts": False}),
                "user_prompt": ("STRING", {"multiline": True, "default": "", "dynamicPrompts": False})
            },
            "optional": {
                "message_in": ("MESSAGE",),
                "image": ("IMAGE",)
            }
        }
    RETURN_TYPES = ("MESSAGE",)
    FUNCTION = "add_user_prompt"
    CATEGORY = "LM Studio 工具"
    
    def add_user_prompt(self, user_prompt, system_prompt="", message_in=None, image=None):
        # 如果有传入消息历史，则复制它；否则创建新的消息对象
        new_message_instance = Message(messages=copy.deepcopy(message_in.get_messages())) if message_in else Message()
        
        # 如果没有传入消息历史且提供了系统提示词，则先添加系统消息
        if not message_in and system_prompt.strip():
            new_message_instance.add_message("system", system_prompt)
        
        final_content = None

        # Case 1: Images are provided (single or multiple)
        if image is not None:
            content_parts = []
            
            # First, add the text part to the list
            if user_prompt.strip():
                content_parts.append({"type": "text", "text": user_prompt})

            # The 'image' input is a batch tensor (N, H, W, C)
            # Iterate through each image in the batch
            for i in range(image.shape[0]):
                single_image_tensor = image[i]
                
                # Convert tensor to PIL Image
                img_np = 255. * single_image_tensor.cpu().numpy()
                img = Image.fromarray(np.clip(img_np, 0, 255).astype(np.uint8))
                
                # Encode image to base64 string
                buffered = io.BytesIO()
                img.save(buffered, format="PNG")
                img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                image_url = f"data:image/png;base64,{img_base64}"
                
                # Append the image part to the content list
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": image_url}
                })
            
            final_content = content_parts
        
        # Case 2: No images are provided
        else:
            if user_prompt.strip():
                final_content = user_prompt

        # Add the constructed message to the conversation history
        if final_content:
            new_message_instance.add_message("user", final_content)
            
        return (new_message_instance,)

# =================================================================================
# 节点 2: LM Studio 请求（整合了 API 配置、模型选择和卸载功能）
# =================================================================================

class LMS_Request:
    """📡 LM Studio 请求节点"""
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "api_address": ("STRING", {"default": "http://127.0.0.1:1234/v1"}),
                "api_key": ("STRING", {"default": "lm-studio"}),
                "model_identifier": ("STRING", {"multiline": False, "default": "gemma-2b-it-gguf"}),
                "message": ("MESSAGE",),
                "seed": ("INT", {"default": -1, "min": -1, "max": 0xffffffffffffffff}),
                "context_length": ("INT", {"default": 4096, "min": 10, "max": 100000, "step": 64}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.01}),
                "top_p": ("FLOAT", {"default": 0.95, "min": 0.0, "max": 1.0, "step": 0.01}),
                "unload_after_completion": ("BOOLEAN", {"default": False}),
            },
            "optional": {"上一步": ("*",),}
        }
    RETURN_TYPES = ("MESSAGE", "STRING",)
    RETURN_NAMES = ("消息输出", "文本输出",)
    FUNCTION = "send_request"
    CATEGORY = "LM Studio 工具"
    
    def send_request(self, api_address, api_key, model_identifier, message, seed, context_length, temperature, top_p, unload_after_completion, 上一步=None):
        if not model_identifier or not model_identifier.strip():
            raise ValueError("模型标识符不能为空。")
        
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        payload = {
            "model": model_identifier, "messages": message.get_messages(), "max_tokens": context_length, 
            "temperature": temperature, "top_p": top_p, "stream": False
        }
        if seed != -1: payload["seed"] = seed

        print(f"LM Studio 工具: 正在向 {api_address} 发送请求，使用模型 '{model_identifier}'...")
        try:
            response = requests.post(f"{api_address}/chat/completions", headers=headers, json=payload, timeout=300)
            response.raise_for_status()
            assistant_reply = response.json()['choices'][0]['message']['content']
            print("LM Studio 工具: 请求成功。")
            new_message_instance = Message(messages=copy.deepcopy(message.get_messages()))
            new_message_instance.add_message("assistant", assistant_reply)
            
            # 如果启用了完成后卸载模型
            if unload_after_completion:
                self._unload_models()
            
            return (new_message_instance, assistant_reply,)
        except requests.exceptions.RequestException as e:
            error_message = f"API 请求失败: {e}"
            print(error_message)
            return (message, error_message,)
    
    def _unload_models(self):
        """卸载所有模型的内部方法"""
        command = "lms.exe" if sys.platform == "win32" else "lms"
        full_command = [command, "unload", "--all"]
        try:
            print(f"LM Studio 工具: 正在尝试使用命令卸载所有模型: '{' '.join(full_command)}'")
            result = subprocess.run(full_command, check=True, text=True, capture_output=True)
            output_log = result.stdout.strip() or "成功卸载所有模型。"
            print(f"LM Studio 工具: 命令执行成功。-> LMS CLI 输出: {output_log}")
        except FileNotFoundError:
            print(f"LM Studio 工具: 错误: 未找到 '{command}' 命令。")
        except subprocess.CalledProcessError as e:
            print(f"LM Studio 工具: 执行命令时出错: {e}\n标准错误: {e.stderr.strip()}")
        except Exception as e:
            print(f"LM Studio 工具: 发生意外错误: {e}")

# =================================================================================
# 已移除独立的 Unload Model 节点，功能已整合到 LMStudio Request 中
# =================================================================================

# =================================================================================
# 节点 3: 获取助手消息
# =================================================================================

class LMS_GetAssistantMessage:
    """📌 获取助手消息"""
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"message": ("MESSAGE",), "index": ("INT", {"default": -1, "min": -9999, "max": 9999})}}
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("文本",)
    FUNCTION = "get_message"
    CATEGORY = "LM Studio 工具"
    def get_message(self, message, index):
        assistant_messages = [m for m in message.get_messages() if m.get('role') == 'assistant']
        if not assistant_messages: return ("",)
        
        # 如果索引为0，返回空（不支持0索引）
        if index == 0: return ("",)
        
        try:
            # 正数索引：1代表第一条，2代表第二条，需要减1转换为数组索引
            if index > 0:
                return (assistant_messages[index - 1].get('content', ''),)
            # 负数索引：-1代表最后一条，-2代表倒数第二条，直接使用
            else:
                return (assistant_messages[index].get('content', ''),)
        except IndexError:
            return ("",)

# =================================================================================
# 节点映射
# =================================================================================

NODE_CLASS_MAPPINGS = {
    "LMS_UserPrompt": LMS_UserPrompt,
    "LMS_Request": LMS_Request,
    "LMS_GetAssistantMessage": LMS_GetAssistantMessage,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LMS_UserPrompt": "👤 综合提示词",
    "LMS_Request": "📡 LMStudio 请求",
    "LMS_GetAssistantMessage": "📌 获取助手消息",
}