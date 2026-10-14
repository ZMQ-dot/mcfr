"""请求来源 IP 的取法。

！这个模块存在的唯一理由，是一个真实且影响很大的坑：

    **X-Forwarded-For 是客户端可以随便伪造的请求头。**

之前 auth / admin / feedback 三处各自写了一份「只要请求里带了这个头，
就取它的第一段」，于是任何人只要在请求里加一行

    X-Forwarded-For: 1.2.3.4

就能在每次请求时换一个身份，所有按 IP 做的限流会**同时失效**：

    · 管理员登录失败限制（5 次 / 15 分钟）→ 可以无限次暴力破解口令
    · 验证码发送限制（每邮箱 / 每 IP / 每天）→ 站点会变成一台
      对着任意邮箱发信的机器，被人拿去轰炸别人
    · 意见反馈的提交频率限制 → 形同虚设
    · 后台「邮件记录」里显示的来源 IP → 全是假的，出事后查不到人

这几层恰好是本站最重要的防线（尤其是管理员登录那一层），所以取 IP
这件事必须做对，而且只能有一份实现 —— 三个地方各写一份，早晚会漏掉一个。

规则（与 Nginx / 反向代理的通行做法一致）：

  1. **默认不信任任何代理**，直接用 TCP 连接的对端地址。这是整个请求里
     唯一无法伪造的来源。单机部署、或者直接用 uvicorn 对外服务，都属于
     这一类 —— 默认值就是为它们准备的。

  2. 只有当 `MCFR_TRUSTED_PROXIES` 里显式列出了反向代理的地址时，
     才去看 X-Forwarded-For，并且**从右往左**读：
     最右边那一跳是紧挨着我们的代理写上去的，最可信；
     最左边是客户端自己写的，最不可信。
     跳过所有可信代理，遇到的第一个「不可信」地址才是真实来源。

  3. 什么都不认得出时返回空串，让调用方退化成「这次不按 IP 限流」，
     而不是「按一个假 IP 限流」—— 后者会让人误以为防护还在。

配置示例（`.env`）：

    # 只信任本机的 Nginx
    MCFR_TRUSTED_PROXIES=127.0.0.1

    # 或者整个内网网段
    MCFR_TRUSTED_PROXIES=127.0.0.1,10.0.0.0/8

没配这一项时，站点**只能**看到直连地址。如果部署在 Nginx 后面却没配它，
限流会把所有访客当成同一个人（都来自 127.0.0.1）—— 这是安全的失败方向：
宁可误伤，也不要让攻击者随便换个头就绕过。
"""

from __future__ import annotations

import ipaddress
import os

from fastapi import Request

ENV_KEY = "MCFR_TRUSTED_PROXIES"

_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] | None = None


def _parse(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """把 `127.0.0.1, 10.0.0.0/8` 这样的串解析成一组网段。"""
    networks = []
    for raw in (value or "").replace(";", ",").split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            # strict=False 允许写 `10.0.0.1/8` 这种「主机地址 + 掩码」的写法
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            print(
                f"[mcfr] !! MCFR_TRUSTED_PROXIES 里的 `{item}` 不是合法的 IP 或网段，已忽略"
            )
    return tuple(networks)


def trusted_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """可信代理网段。第一次调用时读环境变量，之后缓存。"""
    global _networks
    if _networks is None:
        _networks = _parse(os.environ.get(ENV_KEY, ""))
    return _networks


def reset_cache() -> None:
    """改过环境变量之后调一下（测试用；正常启动时不需要）。"""
    global _networks
    _networks = None


def _is_trusted(value: str) -> bool:
    networks = trusted_networks()
    if not networks or not value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in networks)


def client_ip(request: Request) -> str:
    """取这次请求的真实来源 IP；无法确定时返回空串。"""
    peer = request.client.host if request.client else ""

    # 没有配置可信代理 —— 只能相信 TCP 对端，完全不看 X-Forwarded-For
    if not trusted_networks():
        return peer or ""

    # 连到我们的这一跳本身都不是可信代理，那它转发的头自然也不该信
    if not _is_trusted(peer):
        return peer or ""

    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return peer or ""

    chain = [part.strip() for part in forwarded.split(",") if part.strip()]

    # 从右往左找第一个不可信的地址。
    # 右边的都是我们自己（或可信的）代理逐跳追加的，伪造不了；
    # 左边的全是客户端可以自己塞进来的。
    for hop in reversed(chain):
        if not _is_trusted(hop):
            return hop

    # 整条链都是可信代理（单层代理且没带 Client-IP 之类的头），
    # 退回链条最左边那段 —— 它是可信代理声称的客户端地址，比空串有用
    return chain[0] if chain else (peer or "")
