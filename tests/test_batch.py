#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量脚本端到端测试：两台在线 mock 设备 + 一台不可达设备 + 一行坏数据。

覆盖：并发处理、成功/失败/参数错误分类、报告落盘、dry-run 不写入、断点续跑、去重。

运行：python tests/test_batch.py
"""

import csv
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mock_device import MockDevice  # noqa: E402

ROOT = os.path.dirname(HERE)
BATCH = os.path.join(ROOT, "batch_update.py")


def read_report(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def run_batch(*args, cwd):
    return subprocess.run(
        [sys.executable, BATCH, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
    )


def main():
    device_a = MockDevice(super_password="111000")
    device_b = MockDevice(super_password="222000")
    device_a.start()
    device_b.start()

    workdir = tempfile.mkdtemp(prefix="hik_batch_test_")
    csv_path = os.path.join(workdir, "devices.csv")
    report_path = os.path.join(workdir, "report.csv")

    failures = []

    def check(name, condition, detail=""):
        print(("PASS  " if condition else "FAIL  ") + name + (f"  {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    try:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["设备IP", "门号", "新密码", "备注"])
            writer.writerow([device_a.endpoint, 1, "111111", "大门 A"])
            writer.writerow([device_b.endpoint, 1, "222222", "大门 B"])
            writer.writerow(["127.0.0.1:1", 1, "333333", "不可达设备"])
            writer.writerow(["这不是IP", 1, "444444", "坏数据"])

        common = ["-i", csv_path, "--password", "Admin123",
                  "--workers", "4", "--timeout", "2", "--retries", "0",
                  "--report", report_path]

        # ---- 第一次：真实修改 ----
        result = run_batch(*common, cwd=workdir)
        rows = read_report(report_path) if os.path.isfile(report_path) else []
        by_key = {(row["IP"], row["端口"]): row for row in rows}
        # 两份“第一轮报告”：后面 dry-run 会覆盖 report.csv，续跑测试要用原始那份
        report_first = os.path.join(workdir, "report_first.csv")
        if os.path.isfile(report_path):
            shutil.copyfile(report_path, report_first)

        check("退出码为 1（存在失败项）", result.returncode == 1, str(result.returncode))
        check("报告行数 = 4", len(rows) == 4, str(len(rows)))
        check("设备 A 修改成功", device_a.super_password == "111111"
              and ("127.0.0.1", str(device_a.port)) in by_key, str(device_a.super_password))
        check("设备 B 修改成功", device_b.super_password == "222222", str(device_b.super_password))

        statuses = sorted(row["状态"] for row in rows)
        check("状态分类正确（2 成功 / 1 失败 / 1 参数错误）",
              statuses == sorted(["成功", "成功", "失败", "参数错误"]), str(statuses))
        check("报告里原密码是打码的", all("*" in row["原密码"] for row in rows if row["原密码"]),
              str([row["原密码"] for row in rows]))
        check("报告记录了设备型号", any(row["设备型号"].startswith("DS-") for row in rows),
              str([row["设备型号"] for row in rows]))

        # ---- 第二次：dry-run 不应写入 ----
        fingerprint_a, fingerprint_b = device_a.super_password, device_b.super_password
        puts_a, puts_b = device_a.put_count, device_b.put_count
        result = run_batch(*common, "-n", "999999", "--dry-run", cwd=workdir)

        check("dry-run 不写入设备 A", device_a.super_password == fingerprint_a
              and device_a.put_count == puts_a, f"{device_a.super_password} puts={device_a.put_count}")
        check("dry-run 不写入设备 B", device_b.super_password == fingerprint_b
              and device_b.put_count == puts_b, f"{device_b.super_password} puts={device_b.put_count}")

        # ---- 第三次：断点续跑，跳过已成功的两台 ----
        result = run_batch(*common, "--resume", report_first, cwd=workdir)
        rows2 = read_report(report_path)
        check("续跑后只处理未成功的 2 行", len(rows2) == 2, str(len(rows2)))
        check("续跑没有重复写设备", device_a.put_count == puts_a and device_b.put_count == puts_b,
              f"puts={device_a.put_count},{device_b.put_count}")

        # ---- 第四次：--limit 只跑一台 ----
        result = run_batch(*common, "-n", "555555", "--limit", "1", cwd=workdir)
        rows3 = read_report(report_path)
        check("--limit 1 只处理 1 台", len(rows3) == 1, str(len(rows3)))

    finally:
        device_a.stop()
        device_b.stop()
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
