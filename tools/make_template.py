#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_template.py — 生成《设备清单》模板 devices_template.xlsx

表头用中文，批处理脚本会自动识别（也支持 IP/ip/door/password 等英文别名）。
重新生成：python tools/make_template.py
"""

import os

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "devices_template.xlsx")

HEADERS = ["设备IP", "门号", "新密码", "备注"]
WIDTHS = [22, 8, 12, 28]

# 两行示例：正式使用时请全部替换成真实设备
SAMPLES = [
    ["192.168.1.64", 1, "135790", "示例-北门（请删除）"],
    ["192.168.1.65", 1, "246810", "示例-南门（请删除）"],
]


def main():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "设备清单"

    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # ---- 表头 ----
    for index, title in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=1, column=index, value=title)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
        sheet.column_dimensions[get_column_letter(index)].width = WIDTHS[index - 1]
    sheet.row_dimensions[1].height = 22

    # ---- 示例数据 ----
    for row_index, sample in enumerate(SAMPLES, start=2):
        for col_index, value in enumerate(sample, start=1):
            cell = sheet.cell(row=row_index, column=col_index, value=value)
            cell.border = border
            cell.alignment = Alignment(horizontal="center" if col_index in (2, 3) else "left")
            if col_index in (1, 2, 3):
                cell.font = Font(color="808080")     # 示例行用灰色，提醒替换

    # ---- 预置空行格式，方便直接粘贴 200 台设备 ----
    for row_index in range(2, 202):
        for col_index in range(1, len(HEADERS) + 1):
            cell = sheet.cell(row=row_index, column=col_index)
            cell.border = border
            cell.alignment = Alignment(horizontal="center" if col_index in (2, 3) else "left")

    # 门号限定为 1~4 的整数，防止填出 0 或 "一号门" 之类
    door_validation = DataValidation(type="whole", operator="between", formula1="1", formula2="4",
                                     allow_blank=True, showErrorMessage=True,
                                     errorTitle="门号不合法", error="门号应为 1~4 的整数（多数一体机只有 1 个门）")
    sheet.add_data_validation(door_validation)
    door_validation.add(f"B2:B201")

    # 新密码限定 4~8 位数字
    password_validation = DataValidation(type="textLength", operator="between", formula1="4", formula2="8",
                                         allow_blank=True, showErrorMessage=True,
                                         errorTitle="密码长度不合法",
                                         error="超级密码为 4~8 位数字；留空则使用命令行 -n 指定的统一密码")
    sheet.add_data_validation(password_validation)
    password_validation.add("C2:C201")

    sheet.freeze_panes = "A2"

    # ---- 说明页 ----
    guide = workbook.create_sheet("填写说明")
    guide.column_dimensions["A"].width = 100
    lines = [
        ("设备清单填写说明", True),
        ("", False),
        ("1. 只需填「设备IP」这一列，其余可以留空：", False),
        ("   · 门号留空 = 1；多门设备（门禁主机）才需要填 2/3/4", False),
        ("   · 新密码留空 = 使用命令行 -n 指定的统一密码，或 --random 每台随机", False),
        ("2. IP 支持三种写法：192.168.1.64、192.168.1.64:8000、https://192.168.1.64", False),
        ("3. 同一 IP 可以重复出现，但门号必须不同，否则脚本只会处理第一条。", False),
        ("4. 表头必须是「设备IP」（也认 IP / IP地址 / 设备地址 / host），", False),
        ("   前 10 行里都可以，脚本会自动找到表头行。", False),
        ("5. 建议先跑一次预演，确认全部设备可达、账号正确，再真正修改：", False),
        ("      python batch_update.py -i devices_template.xlsx -n 135790 --dry-run", False),
        ("      python batch_update.py -i devices_template.xlsx -n 135790 --workers 8", False),
        ("6. 跑完会生成 output/report_*.csv，含每台设备的成功/失败原因；", False),
        ("   失败修复后加 --resume output/report_xxx.csv 重跑，只补没成功的。", False),
        ("7. 重要：执行前请先确认这些 IP 确实是你负责的设备，并备份原超级密码。", False),
    ]
    for row_index, (text, bold) in enumerate(lines, start=1):
        cell = guide.cell(row=row_index, column=1, value=text)
        cell.font = Font(bold=bold, size=13 if bold else 10)
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    workbook.save(OUTPUT)
    print(f"已生成：{OUTPUT}")


if __name__ == "__main__":
    main()
