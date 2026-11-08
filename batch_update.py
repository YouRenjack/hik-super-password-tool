#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_update.py — 按表格批量修改海康威视门禁一体机的「超级密码」

用法示例
--------
    python batch_update.py -i devices.xlsx -n 135790 --dry-run
    python batch_update.py -i devices.xlsx -n 135790 --workers 8

表格要求
--------
至少包含一列 IP（支持 192.168.1.64、192.168.1.64:8000、http://192.168.1.64 三种写法）。
可选列：门号、新密码（逐台不同时用）、备注/位置。列名可用中文或英文，见 COLUMN_ALIASES。

设计要点
--------
1. 并发：ThreadPoolExecutor，每台设备一个独立 ISAPI 会话，互不影响。
2. 认证失败不重试：连续输错会锁设备账号，401 直接判失败并（默认）中止后续任务。
3. 只重试网络类错误：超时、连接被拒等，指数退避。
4. 断点续跑：--resume 读上一次的报告，跳过已成功的设备。
5. 全量报告：无论成功失败都落 CSV（UTF-8 BOM，Excel 直接打开不乱码）。
"""

from __future__ import annotations

import argparse
import csv
import getpass
import logging
import os
import re
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# 同目录下的核心库
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hik_super_password as core  # noqa: E402

VERSION = "1.0.0"

LOG = logging.getLogger("hik-batch")

# 列名别名：比对前会做归一化（转小写、去掉空格/下划线/连字符/括号）
COLUMN_ALIASES = {
    "ip": ("ip", "ip地址", "设备ip", "设备地址", "地址", "主机", "host", "ipaddress", "网络地址", "设备ip地址"),
    "door": ("门号", "门", "门编号", "door", "doorno", "doorid", "通道"),
    "password": ("新密码", "密码", "新超级密码", "超级密码", "开门密码", "password",
                 "newpassword", "superpassword", "newpwd"),
    "note": ("备注", "位置", "名称", "设备名称", "地点", "安装位置", "remark", "note", "location", "name"),
}

STATUS_OK = "成功"
STATUS_SKIP = "跳过"
STATUS_FAIL = "失败"
STATUS_ABORT = "已中止"
STATUS_DRYRUN = "预演"
STATUS_BADROW = "参数错误"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Task:
    row: int
    ip: str
    port: int
    use_https: bool
    door: int
    new_password: str
    note: str = ""
    error: str = ""            # 非空表示这行数据本身有问题，不发起请求


@dataclass
class Row:
    """一台设备的处理结果，最终写入 CSV。"""
    row: int
    ip: str
    port: int
    door: int
    status: str
    old_password: str = ""
    new_password: str = ""
    model: str = ""
    device_name: str = ""
    note: str = ""
    elapsed_ms: int = 0
    message: str = ""
    finished_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def normalize_header(text) -> str:
    """把表头归一化，便于中英文、有无空格下划线都能匹配。"""
    if text is None:
        return ""
    cleaned = str(text).strip().lower()
    return re.sub(r"[\s_\-（）()【】\[\]:：]+", "", cleaned)


def mask(value: str) -> str:
    """密码打码：保留首尾各一位，用于写进报告的“原密码”列。"""
    if not value:
        return ""
    if len(value) <= 2:
        return "*" * len(value)
    return value[0] + "*" * (len(value) - 2) + value[-1]


def parse_endpoint(cell: str):
    """解析 IP 单元格，支持 1.2.3.4 / 1.2.3.4:8000 / http(s)://1.2.3.4:8000。"""
    text = str(cell).strip()
    if not text:
        raise ValueError("IP 为空")

    use_https = False
    if "://" in text:
        scheme, text = text.split("://", 1)
        use_https = scheme.lower() == "https"
    text = text.split("/")[0].strip()          # 去掉误粘进来的路径

    port = 443 if use_https else 80
    if ":" in text:                            # 兼容 IPv6 之外的 host:port 写法
        host, _, port_text = text.rpartition(":")
        if port_text.isdigit():
            text, port = host, int(port_text)
        else:
            raise ValueError(f"端口不是数字：{cell}")

    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?", text):
        raise ValueError(f"IP/主机名格式不合法：{cell}")

    return text, port, use_https


def random_password(length=6) -> str:
    """生成纯数字随机密码（首位允许 0，设备端按字符串比较）。"""
    return "".join(secrets.choice("0123456789") for _ in range(length))


# --------------------------------------------------------------------------- #
# 表格读取
# --------------------------------------------------------------------------- #
class BatchError(Exception):
    """致命错误：表格读不了、参数配置错误等。"""


def _rows_from_xlsx(path, sheet):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # 环境没装 openpyxl 时给出可执行建议
        raise BatchError(
            "读取 .xlsx 需要 openpyxl。请执行 pip install openpyxl，"
            "或把表格另存为 .csv 后再运行（CSV 不需要任何第三方库）。"
        ) from exc

    workbook = load_workbook(path, read_only=True, data_only=True)
    if isinstance(sheet, str):
        if sheet not in workbook.sheetnames:
            raise BatchError(f"表格里没有名为 {sheet!r} 的工作表，现有：{workbook.sheetnames}")
        worksheet = workbook[sheet]
    else:
        worksheet = workbook.worksheets[sheet or 0]

    return [tuple(row) for row in worksheet.iter_rows(values_only=True)]


def _rows_from_csv(path):
    for encoding in ("utf-8-sig", "gbk", "utf-8"):
        try:
            with open(path, "r", encoding=encoding, newline="") as handle:
                return [tuple(row) for row in csv.reader(handle)]
        except UnicodeDecodeError:
            continue
    raise BatchError(f"无法识别 {path} 的编码，请另存为 UTF-8 或 GBK 的 CSV。")


def load_table(path, sheet=None):
    """读取表格，返回 (表头列表, 数据行列表)。自动跳过标题行，定位真正的表头。"""
    if not os.path.isfile(path):
        raise BatchError(f"找不到表格文件：{path}")

    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        raw = _rows_from_xlsx(path, sheet)
    elif ext == ".csv":
        raw = _rows_from_csv(path)
    else:
        raise BatchError(f"不支持的表格格式 {ext}，请使用 .xlsx 或 .csv。")

    if not raw:
        raise BatchError("表格是空的。")

    # 真实表格常有标题行/说明行，这里在前 10 行里找“含 IP 列名”的那一行当表头
    keywords = {normalize_header(x) for x in COLUMN_ALIASES["ip"]}
    header_index = None
    for index, row in enumerate(raw[:10]):
        cells = {normalize_header(cell) for cell in row if cell is not None}
        if cells & keywords:
            header_index = index
            break

    if header_index is None:
        raise BatchError(
            "前 10 行里没找到 IP 列。表头请包含 IP / IP地址 / 设备IP 之一，"
            f"当前第一行是：{raw[0]}"
        )

    headers = [normalize_header(cell) for cell in raw[header_index]]
    body = raw[header_index + 1:]
    return headers, body


def map_columns(headers):
    """把表头映射成 ip/door/password/note 四个角色，找不到的角色为 None。"""
    mapping = {}
    for role, aliases in COLUMN_ALIASES.items():
        alias_set = {normalize_header(x) for x in aliases}
        for index, header in enumerate(headers):
            if header and header in alias_set:
                mapping[role] = index
                break
    return mapping


# --------------------------------------------------------------------------- #
# 任务构建
# --------------------------------------------------------------------------- #
def build_tasks(headers, body, mapping, args):
    tasks = []

    if "ip" not in mapping:
        raise BatchError(f"表头里没有可识别的 IP 列，识别到：{mapping}")

    for offset, row in enumerate(body, start=1):
        excel_row = offset + 1  # 仅用于提示，真实行号可能因表头偏移而不同
        first_cell = row[mapping["ip"]] if mapping["ip"] < len(row) else None

        # 整行空白直接忽略（Excel 末尾常见空行）
        if all(cell in (None, "") for cell in row):
            continue

        raw_ip = "" if first_cell is None else str(first_cell).strip()
        door = args.door
        note = ""

        if "door" in mapping and mapping["door"] < len(row) and row[mapping["door"]] not in (None, ""):
            try:
                door = int(str(row[mapping["door"]]).strip())
            except ValueError:
                tasks.append(Task(excel_row, raw_ip, 0, False, 0, "", error=f"门号不是整数：{row[mapping['door']]}"))
                continue

        if "note" in mapping and mapping["note"] < len(row) and row[mapping["note"]] is not None:
            note = str(row[mapping["note"]]).strip()

        # 新密码优先级：表格单元格 > 命令行 -n > 随机生成
        cell_password = ""
        if "password" in mapping and mapping["password"] < len(row):
            cell = row[mapping["password"]]
            if cell not in (None, ""):
                cell_password = str(cell).strip()

        if cell_password:
            # 两者都给了却不一致时明确告警，避免“以为在改 A 实际改成 B”
            if args.new_password and args.new_password != cell_password:
                LOG.warning("第 %d 行：表格填的新密码 %s 与命令行 -n %s 不一致，按表格执行。",
                            excel_row, cell_password, args.new_password)
            new_password = cell_password
        else:
            new_password = args.new_password or (random_password(args.random_length) if args.random else "")

        if not raw_ip:
            tasks.append(Task(excel_row, "", 0, False, door, new_password, note, error="IP 为空"))
            continue

        try:
            host, port, use_https = parse_endpoint(raw_ip)
        except ValueError as exc:
            tasks.append(Task(excel_row, raw_ip, 0, False, door, new_password, note, error=str(exc)))
            continue

        if args.port:                      # 命令行 --port 优先（表格里没写端口时）
            port = args.port
        if args.https:                     # 命令行 --https 强制走 HTTPS
            use_https = True

        if not new_password:
            # 表格该行留空、命令行也没给 -n、又没开 --random
            tasks.append(Task(
                excel_row, host, port, use_https, door, "", note,
                error="表格该行没填新密码，命令行也没给 -n（或缺 --random）",
            ))
            continue

        if not core.SUPER_PASSWORD_RE.match(new_password):
            tasks.append(Task(
                excel_row, host, port, use_https, door, new_password, note,
                error=f"新密码 {new_password!r} 不是 4~8 位数字",
            ))
            continue

        tasks.append(Task(excel_row, host, port, use_https, door, new_password, note))

    return tasks


# --------------------------------------------------------------------------- #
# 单台处理
# --------------------------------------------------------------------------- #
def process_one(task: Task, args, stop_event, counter):
    """处理一台设备。返回 Row，异常一律收敛成失败状态，不向上抛。"""
    started = time.perf_counter()

    def finish(status, **kwargs):
        # 进度只在“这台设备真正结束”时记一次，重试不算
        with counter["lock"]:
            counter["done"] += 1
            done = counter["done"]
        LOG.info("[%d/%d] 处理完成：%s", done, counter["total"], task.ip or "(空)")
        return Row(
            row=task.row, ip=task.ip, port=task.port, door=task.door,
            status=status, note=task.note,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            **kwargs,
        )

    if task.error:
        return finish(STATUS_BADROW, new_password=task.new_password, message=task.error)

    # 已有任务因认证失败中止时，后续任务不再发起，避免把账号锁死
    if stop_event.is_set():
        return finish(STATUS_ABORT, new_password=task.new_password, message="前序设备认证失败，已中止")

    attempt = 0
    last_error = ""
    model = ""
    name = ""

    while attempt <= args.retries:
        attempt += 1
        try:
            device = core.Device(
                host=task.ip, port=task.port, use_https=task.use_https,
                user=args.user, password=args.password,
                timeout=args.timeout, verify_tls=args.verify_tls,
            )

            info = core.probe_device(device) if args.probe else {}
            model = info.get("model", "")
            name = info.get("name", "")

            # 防呆：IP 可能被回收/改分配，改之前确认对面确实是海康设备
            if args.require_hikvision and model and not model.upper().startswith("DS-"):
                return finish(STATUS_SKIP, new_password=task.new_password, model=model,
                              device_name=name, message=f"非海康设备（{model}），未修改")

            result = core.change_super_password(
                device, task.door, task.new_password, dry_run=args.dry_run,
            )

            status = {
                "changed": STATUS_OK,
                "skipped": STATUS_SKIP,
                "dry-run": STATUS_DRYRUN,
            }[result["status"]]

            message = {
                STATUS_OK: f"已修改并回读校验通过（{result['endpoint']}）",
                STATUS_SKIP: "超级密码已是目标值，无需修改",
                STATUS_DRYRUN: "预演：仅读取并生成报文，未写入",
            }[status]

            return finish(status, old_password=mask(result["old"] or ""), new_password=task.new_password,
                          model=model, device_name=name, message=message)

        except core.AuthError as exc:
            # 认证失败绝不重试：反复输错会锁定设备账号（默认还会中止整批）
            if args.abort_on_auth:
                stop_event.set()
            return finish(STATUS_FAIL, new_password=task.new_password, model=model, device_name=name,
                          message=f"认证失败，请检查账号密码是否正确：{exc}".splitlines()[0])

        except core.DeviceError as exc:
            last_error = str(exc).replace("\n", " ")[:300]
            if attempt <= args.retries:
                time.sleep(min(2 ** attempt, 5))   # 指数退避，最多 5 秒
                continue

        except Exception as exc:                    # 兜底，避免单台异常拖垮整个批次
            last_error = f"{type(exc).__name__}: {exc}"[:300]

    return finish(STATUS_FAIL, new_password=task.new_password, model=model, device_name=name,
                  message=last_error or "未知错误")


# --------------------------------------------------------------------------- #
# 断点续跑
# --------------------------------------------------------------------------- #
def load_done(report_path):
    """从旧报告里读出已成功的 (ip, door, new_password) 组合。"""
    done = set()
    if not report_path or not os.path.isfile(report_path):
        return done
    try:
        with open(report_path, "r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("状态") == STATUS_OK:
                    done.add((row.get("IP", ""), row.get("门号", ""), row.get("新密码", "")))
    except OSError as exc:
        LOG.warning("读取续跑报告失败：%s", exc)
    return done


# --------------------------------------------------------------------------- #
# 报告输出
# --------------------------------------------------------------------------- #
REPORT_FIELDS = ["序号", "IP", "端口", "门号", "状态", "原密码", "新密码",
                 "设备型号", "设备名称", "备注", "耗时(ms)", "说明", "完成时间"]


def write_report(path, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for row in sorted(rows, key=lambda item: item.row):
            writer.writerow({
                "序号": row.row, "IP": row.ip, "端口": row.port, "门号": row.door,
                "状态": row.status, "原密码": row.old_password, "新密码": row.new_password,
                "设备型号": row.model, "设备名称": row.device_name, "备注": row.note,
                "耗时(ms)": row.elapsed_ms, "说明": row.message, "完成时间": row.finished_at,
            })


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        prog="batch_update.py",
        description="按表格批量修改海康门禁一体机的超级密码（开门密码）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python batch_update.py -i devices.xlsx -n 135790 --workers 8 --dry-run",
    )
    parser.add_argument("-i", "--input", required=True, help="设备清单 .xlsx / .csv")
    parser.add_argument("--sheet", help="xlsx 工作表名或序号，默认第一个")
    parser.add_argument("-n", "--new-password", help="统一的 6 位新密码；表格里有“新密码”列时以表格为准")
    parser.add_argument("--random", action="store_true", help="每台生成不同的随机数字密码")
    parser.add_argument("--random-length", type=int, default=6, help="随机密码位数，默认 6")
    parser.add_argument("--user", default=core.DEFAULT_USER, help="设备登录用户名，默认 admin")
    parser.add_argument("--password", help="设备登录密码；省略时读环境变量 HIK_PASSWORD，再不行交互输入")
    parser.add_argument("--door", type=int, default=1, help="默认门号（表格无门号列时使用），默认 1")
    parser.add_argument("--port", type=int, help="强制覆盖端口（默认按表格，缺省 80）")
    parser.add_argument("--workers", type=int, default=8, help="并发线程数，默认 8")
    parser.add_argument("--timeout", type=float, default=8.0, help="单台请求超时秒数，默认 8")
    parser.add_argument("--retries", type=int, default=1, help="网络错误重试次数，默认 1（认证失败不重试）")
    parser.add_argument("--dry-run", action="store_true", help="只读不写，验证连通性与账号")
    parser.add_argument("--limit", type=int, help="只处理前 N 台，便于先小批量试跑")
    parser.add_argument("--resume", metavar="REPORT", help="读取上次报告，跳过已成功的设备")
    parser.add_argument("--report", help="报告输出路径，默认 output/report_时间戳.csv")
    parser.add_argument("--log", help="日志文件路径，默认与报告同名的 .log")
    parser.add_argument("--no-probe", dest="probe", action="store_false",
                        help="跳过设备信息读取（更快）")
    parser.add_argument("--no-require-hikvision", dest="require_hikvision", action="store_false",
                        help="不校验设备是否为海康（默认校验）")
    parser.add_argument("--allow-auth-continue", dest="abort_on_auth", action="store_false",
                        help="认证失败后仍继续处理后续设备（默认停止，防止批量锁号）")
    parser.add_argument("--https", action="store_true", help="全部使用 HTTPS")
    parser.add_argument("--verify-tls", action="store_true", help="HTTPS 时校验证书")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def resolve_password(args):
    if args.password:
        return args.password
    if os.environ.get("HIK_PASSWORD"):
        return os.environ["HIK_PASSWORD"]
    return getpass.getpass(f"{args.user}@设备 的登录密码（所有设备相同）: ")


def setup_logging(args, report_path):
    log_path = args.log or os.path.splitext(report_path)[0] + ".log"
    os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)

    LOG.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    LOG.handlers.clear()

    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOG.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(console)

    return log_path


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    report_path = args.report or os.path.join("output", f"report_{timestamp}.csv")
    log_path = setup_logging(args, report_path)

    started_at = time.perf_counter()

    try:
        headers, body = load_table(args.input, args.sheet)
        mapping = map_columns(headers)

        # 密码来源校验：命令行 / 表格列 / 随机，三者至少有一个
        if "password" not in mapping and not args.new_password and not args.random:
            raise BatchError(
                "没有可用的新密码：请用 -n 135790 指定统一密码，"
                "或加 --random 每台随机生成，或在表格里加“新密码”列。"
            )
        tasks = build_tasks(headers, body, mapping, args)

        if args.limit:
            tasks = tasks[:args.limit]

        if not tasks:
            LOG.error("表格里没有可处理的数据行。")
            return 1

    except BatchError as exc:
        LOG.error("错误：%s", exc)
        return 1

    # 表格校验通过后再要密码，避免白输入一遍
    args.password = resolve_password(args)

    done_set = load_done(args.resume)
    if done_set:
        before = len(tasks)
        tasks = [t for t in tasks if (t.ip, str(t.door), t.new_password) not in done_set]
        LOG.info("断点续跑：跳过 %d 台已成功的设备。", before - len(tasks))

    if not tasks:
        LOG.info("没有需要处理的设备。")
        return 0

    # 同一 IP + 端口 + 门号 重复出现时只处理一次，避免并发改同一台设备
    seen = set()
    unique_tasks = []
    for task in tasks:
        key = (task.ip, task.port, task.door)
        if key in seen:
            LOG.warning("第 %d 行与前面的设备重复（%s 门%s），已跳过。", task.row, task.ip, task.door)
            continue
        seen.add(key)
        unique_tasks.append(task)
    tasks = unique_tasks

    LOG.info("=" * 78)
    LOG.info("设备清单：%s", os.path.abspath(args.input))
    LOG.info("待处理  ：%d 台    并发：%d    超时：%ss    模式：%s",
             len(tasks), args.workers, args.timeout, "预演(只读)" if args.dry_run else "实际修改")
    LOG.info("报告     ：%s", os.path.abspath(report_path))
    LOG.info("=" * 78)

    stop_event = threading.Event()
    counter = {"done": 0, "total": len(tasks), "lock": threading.Lock()}
    rows = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(process_one, task, args, stop_event, counter): task
            for task in tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                row = future.result()
            except Exception as exc:                 # 理论上不会发生，兜底
                row = Row(task.row, task.ip, task.port, task.door, STATUS_FAIL,
                          new_password=task.new_password, message=f"{type(exc).__name__}: {exc}")
            rows.append(row)

            icon = {STATUS_OK: "✓", STATUS_SKIP: "=", STATUS_DRYRUN: "·",
                    STATUS_FAIL: "✗", STATUS_ABORT: "-", STATUS_BADROW: "!"}.get(row.status, "?")
            LOG.info("%s  %-15s 门%s  %-6s %s", icon, row.ip or "(空)", row.door, row.status,
                     row.message[:80])

    write_report(report_path, rows)

    counts = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1

    elapsed = time.perf_counter() - started_at
    LOG.info("-" * 78)
    LOG.info("完成：共 %d 台，耗时 %.1fs", len(rows), elapsed)
    for status in (STATUS_OK, STATUS_SKIP, STATUS_DRYRUN, STATUS_FAIL, STATUS_ABORT, STATUS_BADROW):
        if counts.get(status):
            LOG.info("  %-6s %d", status, counts[status])
    LOG.info("报告：%s", os.path.abspath(report_path))
    LOG.info("日志：%s", os.path.abspath(log_path))

    failures = counts.get(STATUS_FAIL, 0) + counts.get(STATUS_BADROW, 0)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
