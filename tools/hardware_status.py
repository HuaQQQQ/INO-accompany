# -*- coding: utf-8 -*-
"""INO 硬件状态感知工具：CPU/内存占用 + NVIDIA 显卡温度/占用/显存。"""
import subprocess

from .base import BaseTool


class HardwareStatusTool(BaseTool):
    """读取本机硬件状态（CPU/内存/显卡温度与占用），供 INO 感知电脑状态。"""

    name = "get_hardware_status"
    description = (
        "读取本机硬件实时状态：CPU 占用率、内存占用、NVIDIA 显卡温度/占用率/显存使用。"
        "当 master 问电脑热不热、CPU 或显卡占用高不高、性能状态、显存够不够时调用，"
        "也可以在闲聊中主动感知电脑状态后提及。"
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self):
        super().__init__(self.name, self.description, self.parameters)

    def execute(self) -> str:
        lines = []
        try:
            import psutil
            lines.append(f"CPU 占用：{psutil.cpu_percent(interval=None)}%")
            mem = psutil.virtual_memory()
            lines.append(
                f"内存：{round(mem.used / 1024 ** 3, 1)}/{round(mem.total / 1024 ** 3, 1)} GB（{mem.percent}%）"
            )
        except Exception as e:
            lines.append(f"CPU/内存读取失败：{e}")
        try:
            r = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=8,
            )
            if r.returncode == 0 and r.stdout.strip():
                p = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
                if len(p) >= 5:
                    lines.append(
                        f"显卡 {p[0]}：温度 {p[1]}°C，占用 {p[2]}%，显存 {p[3]}/{p[4]} MB"
                    )
            else:
                lines.append("显卡：未检测到 NVIDIA 独显（nvidia-smi 不可用）")
        except Exception as e:
            lines.append(f"显卡读取失败：{e}")
        return "【本机硬件状态】\n" + "\n".join(lines)
