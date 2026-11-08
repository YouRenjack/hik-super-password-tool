#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单台脚本端到端测试：起一个 mock 设备，用真实 CLI 跑一遍。

运行：python tests/test_roundtrip.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mock_device import MockDevice  # noqa: E402

CLI = os.path.join(os.path.dirname(HERE), "hik_super_password.py")


def run_cli(*args):
    return subprocess.run(
        [sys.executable, CLI, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def main():
    device = MockDevice(super_password="246810")
    device.start()
    failures = []

    def check(name, condition, detail=""):
        print(("PASS  " if condition else "FAIL  ") + name + (f"  {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    base = ["--host", "127.0.0.1", "--port", str(device.port), "--password", "Admin123"]

    try:
        result = run_cli(*base, "--show", "--reveal")
        check("show 读取到当前超级密码", result.returncode == 0 and "246810" in result.stdout,
              result.stdout + result.stderr)

        result = run_cli(*base, "--show")
        check("默认打码不泄露明文", "246810" not in result.stdout, result.stdout)

        result = run_cli(*base, "-n", "112233", "--dry-run")
        check("dry-run 成功且未写入", result.returncode == 0 and device.put_count == 0,
              result.stdout + result.stderr)

        result = run_cli(*base, "-n", "112233")
        check("修改成功", result.returncode == 0 and device.super_password == "112233",
              result.stdout + result.stderr)
        check("回读校验出现在输出中", "校验通过" in result.stdout, result.stdout)

        result = run_cli(*base, "-n", "112233")
        check("相同密码提示无需修改", "无需修改" in result.stdout and device.put_count == 1, result.stdout)

        result = run_cli(*base, "-n", "abcd")
        check("非 4~8 位数字被拒绝", result.returncode != 0 and device.super_password == "112233",
              result.stderr)

        result = run_cli("--host", "127.0.0.1", "--port", str(device.port),
                         "--password", "wrong", "--show")
        check("认证失败返回码为 2", result.returncode == 2, result.stdout + result.stderr)

        result = run_cli("--host", "127.0.0.1", "--port", str(device.port), "--password", "Admin123",
                         "--dump", os.path.join(HERE, "_dump.xml"), "--show")
        check("--dump 能导出原始门参数",
              os.path.isfile(os.path.join(HERE, "_dump.xml")) and "superPassword" in
              open(os.path.join(HERE, "_dump.xml"), encoding="utf-8").read(), result.stdout)
        if os.path.isfile(os.path.join(HERE, "_dump.xml")):
            os.remove(os.path.join(HERE, "_dump.xml"))
    finally:
        device.stop()

    # ---- 第二台：V4.x 固件（DoorParam + Base64 存储），对齐真机 DS-K1T6-HS ----
    legacy = MockDevice(super_password="880725", schema="isapi20", name="大同楼222")
    legacy.start()
    base2 = ["--host", "127.0.0.1", "--port", str(legacy.port), "--password", "Admin123"]

    try:
        result = run_cli(*base2, "--show", "--reveal")
        check("Base64 设备：读出的是解码后的明文 880725",
              result.returncode == 0 and "880725" in result.stdout, result.stdout + result.stderr)
        check("Base64 设备：不会把编码串当明文显示",
              "ODgwNzI1" not in result.stdout, result.stdout)

        result = run_cli(*base2, "-n", "135790")
        check("Base64 设备：写回时自动编码，修改成功",
              result.returncode == 0 and legacy.super_password == "135790",
              result.stdout + result.stderr)

        result = run_cli(*base2, "--show", "--reveal")
        check("Base64 设备：回读为 135790", "135790" in result.stdout, result.stdout)
    finally:
        legacy.stop()

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
