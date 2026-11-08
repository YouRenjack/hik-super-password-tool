#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线单元测试：门参数的定位、插入与 JSON 兜底逻辑（不需要设备）。

运行：python tests/test_parsers.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hik_super_password as hik  # noqa: E402

XML_TWO_DOORS = """<DoorList>
<Door><doorNo>1</doorNo><name>A</name><superPassword>111111</superPassword></Door>
<Door><doorNo>2</doorNo><name>B</name><superPassword>222222</superPassword></Door>
</DoorList>"""

XML_NO_FIELD = """<Door><doorNo>1</doorNo><name>A</name><openDuration>5</openDuration></Door>"""

XML_SELF_CLOSING = """<Door><doorNo>1</doorNo><superPassword/></Door>"""


def main():
    failures = []

    def check(name, condition, detail=""):
        print(("PASS  " if condition else "FAIL  ") + name + (f"  {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    # 多门：只改目标门
    updated = hik.set_super_password_xml(XML_TWO_DOORS, 2, "999999")
    check("多门设备只修改目标门",
          hik.get_super_password_xml(updated, 2) == "999999"
          and hik.get_super_password_xml(updated, 1) == "111111")

    # 缺少节点：插入到 doorNo 之后
    updated = hik.set_super_password_xml(XML_NO_FIELD, 1, "135790")
    check("缺少 superPassword 节点时自动插入", hik.get_super_password_xml(updated, 1) == "135790", updated)
    check("插入后其它字段保持原样", "<openDuration>5</openDuration>" in updated, updated)

    # 自闭合标签
    updated = hik.set_super_password_xml(XML_SELF_CLOSING, 1, "246810")
    check("自闭合标签被正确展开", hik.get_super_password_xml(updated, 1) == "246810", updated)

    # 门号不存在时报错
    try:
        hik.set_super_password_xml(XML_TWO_DOORS, 9, "111111")
        check("门号不存在时抛错", False)
    except hik.DeviceError:
        check("门号不存在时抛错", True)

    # JSON：已有字段
    doc = {"Door": {"doorNo": 1, "superPassword": "111111", "stressPassword": "222222"}}
    hik.set_super_password_json(doc, 1, "654321")
    check("JSON 修改现有字段", doc["Door"]["superPassword"] == "654321"
          and doc["Door"]["stressPassword"] == "222222")

    # JSON：字段缺失，按门号挂载
    doc = {"Door": [{"doorNo": 1}, {"doorNo": 2, "name": "B"}]}
    hik.set_super_password_json(doc, 2, "777777")
    check("JSON 缺字段时按门号挂载",
          doc["Door"][1].get("superPassword") == "777777" and "superPassword" not in doc["Door"][0])

    # 正则兜底：自定义标签顺序与命名空间
    xml_ns = ('<Door xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">'
              '<doorNo>1</doorNo><superPassword>000000</superPassword></Door>')
    updated = hik.set_super_password_xml(xml_ns, 1, "123456")
    check("保留命名空间与属性", 'xmlns="http://www.hikvision.com/ver20/XMLSchema"' in updated
          and "123456" in updated, updated)

    # ---- Base64 编码探测（真机 V4.x 固件行为）----
    check("Base64 字段被解码", hik.decode_password_field("ODgwNzI1") == ("880725", True))
    check("明文数字不会被误判为 Base64", hik.decode_password_field("880725") == ("880725", False))
    check("空字段保持空", hik.decode_password_field("") == ("", False))
    check("None 原样返回", hik.decode_password_field(None) == (None, False))
    check("非 Base64 字符按明文处理", hik.decode_password_field("12ab!@#") == ("12ab!@#", False))
    check("编码与解码对称",
          hik.encode_password_field("135790", True) == "MTM1Nzkw"
          and hik.decode_password_field("MTM1Nzkw") == ("135790", True))
    check("明文设备写回时不编码", hik.encode_password_field("135790", False) == "135790")

    # DoorParam 根节点（真机结构）也能定位与改写
    doorparam = ('<DoorParam version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
                 '<superPassword>ODgwNzI1</superPassword></DoorParam>')
    check("DoorParam 根节点可读取", hik.get_super_password_xml(doorparam, 1) == "ODgwNzI1")
    rewritten = hik.set_super_password_xml(doorparam, 1, "MTM1Nzkw")
    check("DoorParam 根节点可改写", hik.get_super_password_xml(rewritten, 1) == "MTM1Nzkw", rewritten)

    # 多门 DoorParam 列表：按 doorNo 精确命中
    doorparam_list = ('<DoorParamList>'
                      '<DoorParam><doorNo>1</doorNo><superPassword>MTEx</superPassword></DoorParam>'
                      '<DoorParam><doorNo>2</doorNo><superPassword>MjIy</superPassword></DoorParam>'
                      '</DoorParamList>')
    rewritten = hik.set_super_password_xml(doorparam_list, 2, "OTk5")
    check("DoorParam 列表按门号定位",
          hik.get_super_password_xml(rewritten, 1) == "MTEx"
          and hik.get_super_password_xml(rewritten, 2) == "OTk5", rewritten)

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
