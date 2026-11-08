#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hik_super_password.py — 海康威视门禁一体机「超级密码」读写核心库 + 单台命令行

适用：DS-K1T6 系列（含 DS-K1T6-HS / K1T6QT / K1T671 / K1T341 等同代固件）等
      支持 ISAPI 的门禁一体机、人脸识别终端、门禁主机。

概念区分
--------
* 超级密码 (superPassword)：门参数里的 4~8 位数字，在设备键盘/触摸屏上输入即可直接开门。
  它就是本模块操作的对象。
* 管理员密码 / 激活密码：设备登录密码（网页、SADP、iVMS-4200 用的那个），不是超级密码。
* 胁迫码 (stressPassword)：遇到胁迫时输入开门并上报胁迫事件，同样在门参数里。

实现方式
--------
ISAPI over HTTP(S) + HTTP Digest 认证，对 /ISAPI/AccessControl/Door/param/<门号>
做“读-改-写”：只替换 superPassword 字段，其余参数（门锁动作时间、门磁类型、
首人常开时间、胁迫码等）原样回写，避免误改其它配置。

本文件既可作为单台设备的命令行工具，也可被 batch_update.py 作为库调用。
依赖：仅 Python 3.8+ 标准库。
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from xml.sax.saxutils import escape as xml_escape

VERSION = "1.2.0"

DEFAULT_USER = "admin"
SUPER_PASSWORD_RE = re.compile(r"^\d{4,8}$")

# 不同固件版本对门参数的路径略有差异，按顺序尝试，命中即用。
DOOR_PATHS = (
    "/ISAPI/AccessControl/Door/param/{door}",
    "/ISAPI/AccessControl/DoorParam/{door}",
)

DEVICE_INFO_PATH = "/ISAPI/System/deviceInfo"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 2


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class DeviceError(Exception):
    """设备返回错误、网络错误或数据结构无法识别。"""


class AuthError(DeviceError):
    """认证失败（用户名/密码错误，或账号被锁定）。"""


# --------------------------------------------------------------------------- #
# ISAPI 客户端
# --------------------------------------------------------------------------- #
class Device:
    """一台设备的 ISAPI 会话。每个实例持有独立的 opener，可安全用于多线程。"""

    def __init__(self, host, port, use_https, user, password, timeout=10.0, verify_tls=False):
        self.host = host
        self.port = port
        self.use_https = use_https
        self.user = user
        self.password = password
        self.timeout = timeout
        self.verify_tls = verify_tls
        self._opener = self._build_opener()

    # -- URL ---------------------------------------------------------------
    @property
    def scheme(self):
        return "https" if self.use_https else "http"

    @property
    def base(self):
        default_port = 443 if self.use_https else 80
        if self.port == default_port:
            return f"{self.scheme}://{self.host}"
        return f"{self.scheme}://{self.host}:{self.port}"

    # -- 认证 ---------------------------------------------------------------
    def _build_opener(self):
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, self.base, self.user, self.password)
        handlers = [urllib.request.HTTPDigestAuthHandler(manager)]

        if self.use_https:
            context = ssl.create_default_context()
            if not self.verify_tls:          # 设备普遍使用自签证书
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))

        return urllib.request.build_opener(*handlers)

    # -- 请求 ---------------------------------------------------------------
    def request(self, method, path, body=None, content_type="application/xml",
                accept="application/xml, */*"):
        url = self.base + path
        data = body.encode("utf-8") if isinstance(body, str) else body

        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", content_type)
        req.add_header("Accept", accept)

        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            if exc.code == 401:
                raise AuthError(
                    "认证失败 (HTTP 401)。请确认：\n"
                    "  1) 用户名/密码是【设备登录/激活密码】，不是超级密码；\n"
                    "  2) 账号未被锁定（多次输错会锁定，可等 30 分钟或重启设备）；\n"
                    "  3) 设备开启了 ISAPI/HTTP（网页能正常登录即可）。"
                )
            return exc.code, text
        except urllib.error.URLError as exc:
            raise DeviceError(f"无法连接 {url}：{exc.reason}") from None
        except TimeoutError:
            raise DeviceError(f"连接 {url} 超时（{self.timeout}s），请检查 IP / 端口 / 防火墙。") from None

    # -- 便捷 ---------------------------------------------------------------
    def read(self, path, accept="application/xml, */*"):
        status, text = self.request("GET", path, accept=accept)
        return status, text

    def write(self, path, body, content_type="application/xml"):
        status, text = self.request("PUT", path, body=body, content_type=content_type,
                                    accept="application/xml, application/json, */*")
        return status, text


# --------------------------------------------------------------------------- #
# 门参数：XML 定位与改写
# --------------------------------------------------------------------------- #
def _xml_door_scope(xml, door_no):
    """在整篇 XML 中定位某个门的片段区间。单门设备直接返回全文。

    兼容两种根节点：<Door>（旧固件）与 <DoorParam>（V4.x 固件，
    命名空间 http://www.isapi.org/ver20/XMLSchema）。
    """
    blocks = list(re.finditer(r"<(Door|DoorParam)\b[^>]*>.*?</\1>", xml, re.S))
    if len(blocks) <= 1:
        return 0, len(xml)

    for block in blocks:
        if re.search(r"<doorNo>\s*%d\s*</doorNo>" % door_no, block.group(0)):
            return block.start(), block.end()

    raise DeviceError(
        f"门参数里找不到门号 {door_no}（共 {len(blocks)} 个 Door 节点）。"
        "请用 --dump 保存配置后确认门号。"
    )


def get_super_password_xml(xml, door_no):
    start, end = _xml_door_scope(xml, door_no)
    scope = xml[start:end]

    match = re.search(r"<superPassword\b[^>]*>(.*?)</superPassword>", scope, re.S)
    if match:
        return match.group(1).strip()
    if re.search(r"<superPassword\b[^>]*/>", scope):
        return ""                      # 自闭合标签 = 未设置
    return None                        # 完全没有该节点


def set_super_password_xml(xml, door_no, value):
    start, end = _xml_door_scope(xml, door_no)
    scope = xml[start:end]
    escaped = xml_escape(value)

    # 1) 已有成对标签：只换值，保留原属性
    match = re.search(r"<superPassword\b([^>]*)>.*?</superPassword>", scope, re.S)
    if match:
        new_scope = (scope[:match.start()]
                     + f"<superPassword{match.group(1)}>{escaped}</superPassword>"
                     + scope[match.end():])
        return xml[:start] + new_scope + xml[end:]

    # 2) 自闭合标签：展开
    match = re.search(r"<superPassword\b([^>]*)/>", scope)
    if match:
        new_scope = (scope[:match.start()]
                     + f"<superPassword{match.group(1)}>{escaped}</superPassword>"
                     + scope[match.end():])
        return xml[:start] + new_scope + xml[end:]

    # 3) 节点不存在：插到 <doorNo> 之后，标签顺序对设备解析没有影响
    anchor = re.search(r"<doorNo>[^<]*</doorNo>", scope)
    if anchor:
        new_scope = scope[:anchor.end()] + f"<superPassword>{escaped}</superPassword>" + scope[anchor.end():]
        return xml[:start] + new_scope + xml[end:]

    raise DeviceError("门参数 XML 结构无法识别（缺少 doorNo），请用 --dump 保存后人工查看。")


# --------------------------------------------------------------------------- #
# 门参数：JSON 兜底（部分新固件默认走 JSON）
# --------------------------------------------------------------------------- #
def _walk(node, path=()):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, path + (index,))


def get_super_password_json(payload, door_no, key="superpassword"):
    target = key.lower()
    for _, node in _walk(payload):
        for name, value in node.items():
            if name.lower() == target:
                return "" if value is None else str(value)
    return None


def set_super_password_json(payload, door_no, value):
    for _, node in _walk(payload):
        for key in list(node.keys()):
            if key.lower() == "superpassword":
                node[key] = value
                return payload

    # 没有该字段：挂在匹配门号的对象上
    fallback = None
    for _, node in _walk(payload):
        keys = {k.lower() for k in node}
        if "doorno" in keys:
            door_value = next(v for k, v in node.items() if k.lower() == "doorno")
            if str(door_value) == str(door_no):
                node["superPassword"] = value
                return payload
            if fallback is None:
                fallback = node

    if fallback is not None:
        fallback["superPassword"] = value
        return payload

    raise DeviceError("JSON 门参数结构无法识别，请用 --dump 保存后人工查看。")


# --------------------------------------------------------------------------- #
# 读取 / 写入
# --------------------------------------------------------------------------- #
def fetch_door(device, door_no, debug=False):
    """返回 (path, fmt, text)。fmt 为 'xml' 或 'json'。"""
    attempts = []

    for template in DOOR_PATHS:
        path = template.format(door=door_no)
        status, text = device.read(path)
        if debug:
            print(f"[debug] GET {path} -> HTTP {status}", file=sys.stderr)
        if status == 200 and text.strip():
            return path, "xml", text
        attempts.append(f"GET {path} -> HTTP {status}")

    for template in DOOR_PATHS:
        path = template.format(door=door_no) + "?format=json"
        status, text = device.read(path, accept="application/json")
        if debug:
            print(f"[debug] GET {path} -> HTTP {status}", file=sys.stderr)
        if status == 200 and text.strip():
            try:
                json.loads(text)
            except ValueError:
                attempts.append(f"GET {path} -> 返回内容不是 JSON")
                continue
            return path, "json", text
        attempts.append(f"GET {path} -> HTTP {status}")

    raise DeviceError(
        "读取门参数失败，设备未在下列地址返回门参数：\n  "
        + "\n  ".join(attempts)
        + "\n可能原因：固件不支持 ISAPI 门参数接口 / 该型号无键盘与超级密码功能 / 端口或路径不同。"
    )


def decode_password_field(raw):
    """解码设备返回的密码字段。

    海康部分固件（如 DS-K1T6-HS V4.38）把 superPassword / stressPassword 以
    Base64 存储，例如 "ODgwNzI1" 实际是 "880725"。另一些固件直接存明文。
    这里做自动判别：只有“能被 Base64 解开、且解出来是可打印短字符串”才认为是编码值。

    返回 (明文, 是否为 Base64 编码)。
    """
    if raw is None:
        return None, False

    text = raw.strip()
    if not text:
        return "", False

    # 长度必须是 4 的倍数，字符集符合 Base64，才值得尝试
    if len(text) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", text):
        try:
            plain = base64.b64decode(text, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return text, False
        if plain and plain.isprintable() and len(plain) <= 32:
            return plain, True

    return text, False


def encode_password_field(value, as_base64):
    """按读取时探测到的编码方式写回，保证设备接受。"""
    if not as_base64:
        return value
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def detect_doc_encoding(text, fmt):
    """当前字段缺失时，用同一份文档里的 stressPassword 反推编码方式。"""
    if fmt == "json":
        raw = get_super_password_json(json.loads(text), 1, key="stresspassword")
    else:
        match = re.search(r"<stressPassword\b[^>]*>(.*?)</stressPassword>", text, re.S)
        raw = match.group(1) if match else None
    return decode_password_field(raw)[1]


def read_current(device, door_no, debug=False):
    """读取并解码当前超级密码。

    返回 dict：value(明文或 None) / raw(原始字段) / b64(是否 Base64) /
              path / fmt / text
    """
    path, fmt, text = fetch_door(device, door_no, debug)
    if fmt == "json":
        raw = get_super_password_json(json.loads(text), door_no)
    else:
        raw = get_super_password_xml(text, door_no)

    value, is_b64 = decode_password_field(raw)
    return {"value": value, "raw": raw, "b64": is_b64,
            "path": path, "fmt": fmt, "text": text}


def check_write_response(status, text, fmt):
    if status != 200:
        raise DeviceError(f"写入失败：HTTP {status}\n{text[:800]}")

    if fmt == "json":
        try:
            payload = json.loads(text)
        except ValueError:
            return
        code = payload.get("statusCode")
        if code not in (None, 1, "1"):
            raise DeviceError(f"写入被拒绝：{payload.get('statusString') or payload.get('subStatusCode') or code}")
        return

    code = re.search(r"<statusCode>\s*(\d+)\s*</statusCode>", text)
    if code and code.group(1) != "1":
        detail = re.search(r"<subStatusCode>\s*([^<]+)\s*</subStatusCode>", text)
        reason = re.search(r"<statusString>\s*([^<]+)\s*</statusString>", text)
        raise DeviceError(
            f"写入被拒绝：statusCode={code.group(1)} "
            f"{reason.group(1) if reason else ''} "
            f"{detail.group(1) if detail else ''}".strip()
        )


def change_super_password(device, door_no, new_password, dry_run=False, debug=False):
    """读-改-写-回读校验，返回结构化结果，供单台 CLI 与批量脚本共用。

    返回 dict：
        status   : "changed" | "skipped" | "dry-run"
        old      : 修改前的超级密码（None 表示原先没有该字段）
        new      : 目标超级密码
        endpoint : 实际使用的 ISAPI 路径
        format   : "xml" | "json"
        body     : 提交的报文（dry-run 时可打印）
    """
    info = read_current(device, door_no, debug)
    current = info["value"]
    fmt, text = info["fmt"], info["text"]

    # 设备可能用 Base64 存密码；读时探测到哪种编码，写回就用哪种
    as_b64 = info["b64"] if info["raw"] is not None else detect_doc_encoding(text, fmt)
    stored = encode_password_field(new_password, as_b64)

    if fmt == "json":
        payload = json.loads(text)
        body = json.dumps(set_super_password_json(payload, door_no, stored),
                          ensure_ascii=False, separators=(",", ":"))
        content_type = "application/json"
    else:
        body = set_super_password_xml(text, door_no, stored)
        content_type = "application/xml"

    result = {
        "status": "changed",
        "old": current,
        "new": new_password,
        "endpoint": info["path"],
        "format": fmt,
        "body": body,
    }

    if current == new_password:
        result["status"] = "skipped"
        return result

    if dry_run:
        result["status"] = "dry-run"
        return result

    status, response = device.write(info["path"], body, content_type=content_type)
    check_write_response(status, response, fmt)

    # 回读校验：避免“提示成功其实没生效”
    confirmed = read_current(device, door_no, debug)["value"]
    if confirmed != new_password:
        raise DeviceError(
            f"回读校验失败：设备当前值为 {confirmed!r}，与期望值不一致。"
            "部分固件对超级密码有额外校验（如必须为纯数字或长度受限），请检查设备网页端提示。"
        )

    return result


def probe_device(device):
    """读取设备基础信息，用于批量时确认“改的是哪台机器”。失败不影响主流程。"""
    try:
        status, text = device.read(DEVICE_INFO_PATH)
    except DeviceError:
        return {}

    if status != 200 or not text.strip():
        return {}

    info = {}
    for tag, key in (("deviceName", "name"), ("model", "model"),
                     ("serialNumber", "serial"), ("firmwareVersion", "firmware"),
                     ("deviceType", "type")):
        match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        if match:
            info[key] = match.group(1).strip()

    if not info and text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return {}
        for key in ("deviceName", "model", "serialNumber", "firmwareVersion"):
            if key in payload:
                info[key] = payload[key]

    return info


# --------------------------------------------------------------------------- #
# 命令行（单台）
# --------------------------------------------------------------------------- #
def do_show(device, door_no, reveal, dump_path, debug=False):
    info = read_current(device, door_no, debug)
    value = info["value"]

    if dump_path:
        with open(dump_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(info["text"])
        print(f"原始门参数已保存：{os.path.abspath(dump_path)}")

    print(f"设备      : {device.base}")
    print(f"接口      : {info['path']}  ({info['fmt'].upper()})")
    print(f"门号      : {door_no}")

    if value is None:
        print("超级密码  : 未在该门参数中找到 superPassword 字段")
    elif value == "":
        print("超级密码  : 未设置")
    else:
        shown = value if reveal else "*" * len(value)
        encoded = "，设备内为 Base64 存储" if info["b64"] else ""
        print(f"超级密码  : {shown}   （--reveal 显示明文{encoded}）")

    return EXIT_OK


def do_set(device, door_no, new_password, dry_run, debug=False):
    result = change_super_password(device, door_no, new_password, dry_run=dry_run, debug=debug)

    if result["status"] == "skipped":
        print(f"当前超级密码已经是 {new_password}，无需修改。")
        return EXIT_OK

    print(f"设备      : {device.base}")
    print(f"接口      : {result['endpoint']}  ({result['format'].upper()})")
    print(f"门号      : {door_no}")
    print(f"原密码    : {'(未设置)' if not result['old'] else result['old']}")
    print(f"新密码    : {result['new']}")

    if result["status"] == "dry-run":
        print("\n[dry-run] 将要提交的报文：\n" + result["body"])
        print("\n[dry-run] 未写入设备。")
        return EXIT_OK

    print("写入成功，正在回读校验……")
    print("校验通过，超级密码已更新。")
    print("提示：新密码在设备键盘/触摸屏上输入即可开门，无需再刷卡或刷脸。")
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hik_super_password.py",
        description="读取/修改海康威视门禁一体机的超级密码（门参数 superPassword）。",
        epilog="示例：python hik_super_password.py --host 192.168.1.64 --password Admin123 --show",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="设备 IP 或域名，例如 192.168.1.64")
    parser.add_argument("--port", type=int, help="HTTP/HTTPS 端口，默认 80（--https 时 443）")
    parser.add_argument("--https", action="store_true", help="使用 HTTPS")
    parser.add_argument("--user", default=DEFAULT_USER, help=f"设备登录用户名，默认 {DEFAULT_USER}")
    parser.add_argument(
        "--password",
        help="设备登录/激活密码；省略时读环境变量 HIK_PASSWORD，再不行交互输入",
    )
    parser.add_argument("--door", type=int, default=1, help="门号，默认 1")
    parser.add_argument("-n", "--new-password", help="新的超级密码（4~8 位数字）；省略则只读取")
    parser.add_argument("--show", action="store_true", help="只读取当前超级密码")
    parser.add_argument("--reveal", action="store_true", help="读取时显示明文（默认打码）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要提交的报文，不写入")
    parser.add_argument("--dump", metavar="FILE", help="把原始门参数保存到文件")
    parser.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数，默认 10")
    parser.add_argument("--force", action="store_true", help="跳过“4~8 位纯数字”格式校验")
    parser.add_argument("--verify-tls", action="store_true", help="HTTPS 时校验证书（设备多为自签，默认不校验）")
    parser.add_argument("--debug", action="store_true", help="打印每次请求的地址与状态码")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def resolve_password(args):
    if args.password:
        return args.password
    env = os.environ.get("HIK_PASSWORD")
    if env:
        return env
    return getpass.getpass(f"{args.user}@{args.host} 的设备登录密码: ")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.new_password is None:
        args.show = True

    if args.new_password is not None and not SUPER_PASSWORD_RE.match(args.new_password):
        message = "超级密码按手册应为 4~8 位数字。"
        if not args.force:
            parser.error(message + "确实要这样设置请加 --force。")
        print(f"警告：{message}（已加 --force，继续）", file=sys.stderr)

    port = args.port if args.port else (443 if args.https else 80)

    device = Device(
        host=args.host,
        port=port,
        use_https=args.https,
        user=args.user,
        password=resolve_password(args),
        timeout=args.timeout,
        verify_tls=args.verify_tls,
    )

    try:
        if args.show:
            return do_show(device, args.door, args.reveal, args.dump, args.debug)
        return do_set(device, args.door, args.new_password, args.dry_run, args.debug)
    except AuthError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_AUTH
    except DeviceError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
