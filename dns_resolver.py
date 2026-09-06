# -*- coding: utf-8 -*-
"""
DNS 兜底解析模块
================
问题背景：当前网络环境（路由器 QWRT 192.168.8.1 + 上游 DNS）对部分域名的
UDP 53 查询被劫持/污染，返回伪造的 `.lan` 解析结果（如 api.deepseek.com →
28.0.15.83、top.baidu.com 解析失败），导致 HTTPS 连接失败（getaddrinfo failed
或连到错误 IP）。

方案：monkey-patch socket.getaddrinfo —— 系统解析失败（gaierror）时，改用
DoH（HTTPS 加密 DNS：阿里 223.5.5.5 / 腾讯 1.12.12.12）查询真实 A 记录，
并过滤污染结果（.lan 后缀、保留/私有/链路本地/多播地址）。

用法：import dns_resolver 即自动生效（幂等，可重复 import）。
"""

import ipaddress
import socket
import threading
import time

import requests

DOH_SERVERS = [
    "https://223.5.5.5/resolve",   # 阿里 DNS
    "https://1.12.12.12/resolve",  # 腾讯 DNS
]
DOH_TIMEOUT = 3.0
CACHE_TTL = 60.0  # 秒：解析结果缓存时长

_cache = {}            # host -> (ip, expire_ts)
_cache_lock = threading.Lock()
_patched = False


def _is_bad_ip(ip: str) -> bool:
    """过滤被污染/不可用的解析结果（保留地址/链路本地/多播等）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_private or addr.is_reserved or addr.is_link_local
        or addr.is_loopback or addr.is_multicast or addr.is_unspecified
    )


def doh_resolve(host: str) -> str:
    """用 DoH 查询 A 记录，返回第一个真实公网 IP；失败返回空串。"""
    if not host or not host.replace(".", "").replace("-", "").isalnum():
        return ""
    for server in DOH_SERVERS:
        try:
            r = requests.get(
                server,
                params={"name": host, "type": "A"},
                timeout=DOH_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            for ans in r.json().get("Answer", []):
                if ans.get("type") == 1:  # A 记录
                    ip = str(ans.get("data", "")).strip()
                    if ip and not ip.lower().endswith(".lan") and not _is_bad_ip(ip):
                        return ip
        except Exception:
            continue
    return ""


def _getaddrinfo_patched(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        if not isinstance(host, str):
            raise
        now = time.time()
        with _cache_lock:
            cached = _cache.get(host)
            if cached and cached[1] > now:
                ip = cached[0]
            else:
                ip = doh_resolve(host)
                _cache[host] = (ip, now + CACHE_TTL)
        if ip:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        raise


def install():
    """安装全局 DNS 兜底（幂等，可重复调用）。"""
    global _patched, _original_getaddrinfo
    if _patched:
        return
    _original_getaddrinfo = socket.getaddrinfo
    socket.getaddrinfo = _getaddrinfo_patched
    _patched = True
    print("[DNS Resolver] 已启用 DoH DNS 兜底（应对被污染的 UDP 53 解析）")


install()
