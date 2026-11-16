"""通过可配置模型接口或命令行程序调用代理。"""
import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .files import now, write_json


def parse_object(content):
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("代理交付需要结构化对象")
    return value


def role_prompt(role):
    if role not in {"project_start", "coordinator", "researcher", "collector", "critic", "retrospective"}:
        raise ValueError("未知代理角色")
    return (Path(__file__).parent / "prompts" / (role + ".md")).read_text(encoding="utf-8")


class CompatibleAgent:
    """调用兼容Chat Completions消息格式的模型服务。"""

    def __init__(self, config):
        self.config = config

    def run(self, role, task, context, directory):
        directory = Path(directory)
        if (directory / "request.json").exists():
            raise ValueError("本次代理目录已有请求，请先读取原件，再为新任务指定新目录")
        request_data = {"role": role, "task": task, "context": context, "requested_at": now()}
        write_json(directory / "request.json", request_data)
        secret = os.environ.get(self.config.get("api_key_env", "PM_PAPER_API_KEY"))
        if not secret:
            raise ValueError("模型接口密钥环境变量未设置")
        base = self.config["base_url"].rstrip("/")
        payload = {
            "model": self.config["model"], "stream": False,
            "max_tokens": self.config.get("max_tokens", 4000),
            "messages": [{"role": "system", "content": role_prompt(role)},
                         {"role": "user", "content": json.dumps(request_data, ensure_ascii=False)}],
        }
        request = Request(base + "/chat/completions", data=json.dumps(payload).encode(), method="POST",
                          headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.config.get("timeout_seconds", 120)) as response:
                raw = json.load(response)
            choice = raw["choices"][0]
            content = choice["message"]["content"]
            receipt = {"received_at": now(), "model": raw.get("model"), "response_id": raw.get("id"),
                       "usage": raw.get("usage"), "finish_reason": choice.get("finish_reason"), "content": content}
            write_json(directory / "response.json", receipt)
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise ValueError("模型交付被截断或过滤，保留原文后等待处理")
            result = parse_object(content)
            write_json(directory / "result.json", result)
            return result
        except HTTPError as error:
            write_json(directory / "error.json", {"at": now(), "http_status": error.code, "error": error.reason})
            raise RuntimeError("模型接口请求失败，HTTP状态为" + str(error.code)) from None
        except Exception as error:
            message = str(error).replace(secret, "[密钥已隐藏]")
            write_json(directory / "error.json", {"at": now(), "error": message})
            raise RuntimeError(message) from None


class CommandAgent:
    """给外部代理的标准输入发送对象，从标准输出读取交付。"""

    def __init__(self, config):
        self.config = config

    def run(self, role, task, context, directory):
        directory = Path(directory)
        if (directory / "request.json").exists():
            raise ValueError("本次代理目录已有请求，请先读取原件，再为新任务指定新目录")
        command = self.config["command"]
        if not isinstance(command, list) or not command or any(not isinstance(value, str) for value in command):
            raise ValueError("代理命令需要明确的参数数组")
        request = {"role": role, "system_prompt": role_prompt(role), "task": task, "context": context, "requested_at": now()}
        write_json(directory / "request.json", request)
        try:
            completed = subprocess.run(command, input=json.dumps(request, ensure_ascii=False), text=True, encoding="utf-8",
                                       capture_output=True, timeout=self.config.get("timeout_seconds", 120),
                                       cwd=self.config.get("cwd"), check=False)
            write_json(directory / "response.json", {"received_at": now(), "exit_code": completed.returncode,
                                                     "stdout": completed.stdout, "stderr": completed.stderr})
            if completed.returncode:
                raise RuntimeError("外部代理进程退出码为" + str(completed.returncode))
            result = parse_object(completed.stdout)
            write_json(directory / "result.json", result)
            return result
        except Exception as error:
            write_json(directory / "error.json", {"at": now(), "error": str(error)})
            raise


def create_agent(config):
    kind = config.get("type", "openai-compatible")
    if kind == "openai-compatible":
        return CompatibleAgent(config)
    if kind == "command":
        return CommandAgent(config)
    raise ValueError("代理接口类型未知，可实现相同的run方法接入其他服务")
