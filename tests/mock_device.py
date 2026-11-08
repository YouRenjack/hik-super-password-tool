#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_device.py — 可复用的海康门禁一体机模拟器（仅用于离线测试）

行为对齐真实设备：
* 未认证返回 401 + Digest challenge，必须算出正确 response 才放行（真校验，不是走个过场）；
* GET  /ISAPI/AccessControl/Door/param/1  返回门参数 XML；
* PUT  同一路径，校验报文里带 superPassword，成功后改内存状态；
* GET  /ISAPI/System/deviceInfo 返回型号、名称，供批量脚本做“是不是海康”的判断。
"""

import base64
import hashlib
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

REALM = "DS-1"
NONCE = "dcd98b7102dd2f0e8b11d0f600bfb0c093"
OPAQUE = "5ccc069c403ebaf9f0171e9517f40e41"

FIELD_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]+))')

DOOR_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Door xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
<doorNo>1</doorNo>
<name>Door 1</name>
<openDuration>5</openDuration>
<closeTimeout>30</closeTimeout>
<doorOpenTimeout>30</doorOpenTimeout>
<superPassword>{super_password}</superPassword>
<stressPassword>135790</stressPassword>
<magneticType>1</magneticType>
<openButtonType>1</openButtonType>
</Door>
"""

# 真机 DS-K1T6-HS（V4.38 固件）的实际结构：根节点 DoorParam，
# 命名空间 isapi.org，密码字段为 Base64。
DOORPARAM_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<DoorParam version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
<doorName></doorName>
<magneticType>alwaysClose</magneticType>
<openButtonType>alwaysOpen</openButtonType>
<openDuration>5</openDuration>
<disabledOpenDuration>15</disabledOpenDuration>
<magneticAlarmTimeout>30</magneticAlarmTimeout>
<enableDoorLock>true</enableDoorLock>
<enableLeaderCard>false</enableLeaderCard>
<leaderCardMode>disable</leaderCardMode>
<leaderCardOpenDuration>10</leaderCardOpenDuration>
<stressPassword>{stress_password}</stressPassword>
<superPassword>{super_password}</superPassword>
<Leader>
<effectiveTimeEnabled>false</effectiveTimeEnabled>
</Leader>
<doorNotClosedAlarmEnabled>false</doorNotClosedAlarmEnabled>
<doorNotClosedAlarmText></doorNotClosedAlarmText>
</DoorParam>
"""

ERR_BAD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ResponseStatus version="1.0" xmlns="http://www.hikvision.com/ver10/XMLSchema">
<requestURL></requestURL>
<statusCode>6</statusCode>
<statusString>Invalid Content</statusString>
<subStatusCode>badXmlContent</subStatusCode>
<errorCode>1610612739</errorCode>
</ResponseStatus>"""

DEVICE_INFO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DeviceInfo xmlns="http://www.hikvision.com/ver10/XMLSchema" version="1.0">
<deviceName>{name}</deviceName>
<deviceID>88</deviceID>
<model>{model}</model>
<serialNumber>DS-K1T6HSN20240101AA</serialNumber>
<firmwareVersion>V2.3.0 build 240101</firmwareVersion>
<deviceType>AccessControl</deviceType>
</DeviceInfo>
"""


def md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class MockDevice:
    """一台模拟设备。start() 后在 127.0.0.1 的随机端口上监听。"""

    def __init__(self, super_password="246810", user="admin", password="Admin123",
                 model="DS-K1T6-HS", name="Mock Terminal", schema="hik"):
        self.super_password = super_password
        self.stress_password = "135790"
        self.schema = schema        # "hik"=旧固件明文字段；"isapi20"=V4.x 固件 Base64
        self.user = user
        self.password = password
        self.model = model
        self.name = name
        self.put_count = 0          # 收到多少次 PUT，用于断言“有没有真的写入”
        self._server = None
        self._thread = None

    # -- 生命周期 -----------------------------------------------------------
    def start(self):
        self._server = HTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    @property
    def port(self):
        return self._server.server_address[1]

    @property
    def endpoint(self):
        return f"127.0.0.1:{self.port}"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- 业务 ---------------------------------------------------------------
    def door_xml(self):
        if self.schema == "isapi20":
            return DOORPARAM_XML_TEMPLATE.format(
                super_password=b64(self.super_password),
                stress_password=b64(self.stress_password),
            )
        return DOOR_XML_TEMPLATE.format(super_password=self.super_password)

    def device_info_xml(self):
        return DEVICE_INFO_XML.format(name=self.name, model=self.model)


def _make_handler(device):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):     # 静默，避免污染测试输出
            pass

        # -- 认证 ----------------------------------------------------------
        def _authorized(self):
            header = self.headers.get("Authorization") or ""
            if not header.startswith("Digest "):
                return False

            fields = {}
            for key, quoted, bare in FIELD_RE.findall(header[len("Digest "):]):
                fields[key.lower()] = quoted or bare

            if fields.get("username") != device.user:
                return False
            if fields.get("nonce") != NONCE or fields.get("realm") != REALM:
                return False

            uri = fields.get("uri", self.path)
            ha1 = md5(f"{device.user}:{REALM}:{device.password}")
            ha2 = md5(f"{self.command}:{uri}")
            if fields.get("qop") == "auth":
                expected = md5(f"{ha1}:{NONCE}:{fields.get('nc')}:{fields.get('cnonce')}:auth:{ha2}")
            else:
                expected = md5(f"{ha1}:{NONCE}:{ha2}")
            return fields.get("response") == expected

        def _challenge(self):
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate",
                f'Digest realm="{REALM}", qop="auth", nonce="{NONCE}", opaque="{OPAQUE}"',
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send(self, code, body, ctype="application/xml"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -- 路由 ----------------------------------------------------------
        def do_GET(self):
            if not self._authorized():
                return self._challenge()
            if self.path.startswith("/ISAPI/AccessControl/Door/param/1"):
                return self._send(200, device.door_xml())
            if self.path.startswith("/ISAPI/System/deviceInfo"):
                return self._send(200, device.device_info_xml())
            return self._send(404, "<ResponseStatus><statusCode>4</statusCode></ResponseStatus>")

        def do_PUT(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""

            if not self._authorized():
                return self._challenge()
            if not self.path.startswith("/ISAPI/AccessControl/Door/param/1"):
                return self._send(404, "<ResponseStatus><statusCode>4</statusCode></ResponseStatus>")

            match = re.search(r"<superPassword>(.*?)</superPassword>", body, re.S)
            if not match:
                return self._send(400, ERR_BAD_XML)

            value = match.group(1)
            if device.schema == "isapi20":
                # 与真机一致：V4.x 固件只接受 Base64，塞明文直接 badXmlContent
                try:
                    value = base64.b64decode(value, validate=True).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    return self._send(400, ERR_BAD_XML)

            device.super_password = value
            device.put_count += 1
            return self._send(
                200,
                "<ResponseStatus><statusCode>1</statusCode><statusString>OK</statusString>"
                "<subStatusCode>ok</subStatusCode></ResponseStatus>",
            )

    return Handler
