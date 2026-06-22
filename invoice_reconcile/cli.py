import os
import sys
import json
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import click

from .config import Config
from .session import Session, SessionManager
from .importer import import_invoices, import_transactions, ImportError
from .matcher import auto_match
from .manual import (
    manual_match, suspend_invoice, suspend_transaction,
    unsuspend_invoice, unsuspend_transaction, reverse_match,
    list_unmatched, list_history,
)
from .reporter import build_report, export_json, export_csv
from .snapshot import SnapshotManager, SNAPSHOT_VERSION
from .audit import AuditManager, AUDIT_VERSION


STATE_FILE = ".irec_state.json"


def _save_current_session(name: str) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"current_session": name}, f, ensure_ascii=False, indent=2)


def _get_current_session() -> Optional[str]:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("current_session")
    except Exception:
        return None


def _load_cfg_and_sm():
    cfg = Config.load()
    sm = SessionManager(cfg.session_dir, cfg.default_session)
    return cfg, sm


def _resolve_session(session_name: Optional[str]) -> str:
    resolved = session_name or _get_current_session()
    if not resolved:
        cfg, _ = _load_cfg_and_sm()
        resolved = cfg.default_session
    return resolved


def _load_session(session_name: Optional[str]):
    cfg, sm = _load_cfg_and_sm()
    name = _resolve_session(session_name)
    try:
        session = sm.load(name)
    except FileNotFoundError:
        raise click.ClickException(f"会话 '{name}' 不存在，请先用 `irec session create` 创建")
    return cfg, sm, session


def _fmt_status_header(summary) -> str:
    s = summary
    lines = [
        f"═══════════════════════════════════════════════════════════════",
        f"  会话状态: {s['name']}  (ID: {s['session_id']})",
        f"  创建: {s['created_at']}    最后更新: {s['updated_at']}",
        f"───────────────────────────────────────────────────────────────",
        f"  发票:   总{s['invoices']['total']:>4}  已匹配{s['invoices']['matched']:>4}  "
        f"未匹配{s['invoices']['unmatched']:>4}  挂起{s['invoices']['suspended']:>4}",
        f"          金额  总{s['invoices']['amount_total']:>12,.2f}  "
        f"已匹配{s['invoices']['amount_matched']:>12,.2f}",
        f"  流水:   总{s['transactions']['total']:>4}  已匹配{s['transactions']['matched']:>4}  "
        f"未匹配{s['transactions']['unmatched']:>4}  挂起{s['transactions']['suspended']:>4}",
        f"          金额  总{s['transactions']['amount_total']:>12,.2f}  "
        f"已匹配{s['transactions']['amount_matched']:>12,.2f}",
        f"  匹配:   活动{s['matches']['active']:>4}  已撤销{s['matches']['reversed']:>4}  "
        f"历史操作{s['history_count']:>4}  已导入文件{s['imported_files']:>4}",
        f"═══════════════════════════════════════════════════════════════",
    ]
    return "\n".join(lines)


# ============================================================
# CLI Group
# ============================================================
@click.group(help="发票到款核对CLI工具 - irec")
@click.version_option(package_name="invoice-reconcile", prog_name="irec")
def main():
    pass


# ============================================================
# init 命令
# ============================================================
@main.command(help="初始化配置文件和默认会话")
@click.option("--amount-tol", type=float, default=0.01, help="金额容差（默认0.01元）")
@click.option("--days-tol", type=int, default=3, help="日期容差天数（默认3天）")
@click.option("--session-dir", type=str, default=".irec_sessions", help="会话存储目录")
def init(amount_tol, days_tol, session_dir):
    cfg = Config.load()
    cfg.amount_tolerance = amount_tol
    cfg.date_tolerance_days = days_tol
    cfg.session_dir = session_dir
    cfg.customer_name_aliases = cfg.customer_name_aliases or {}
    cfg.save()
    sm = SessionManager(session_dir, cfg.default_session)
    try:
        sm.create(cfg.default_session)
        _save_current_session(cfg.default_session)
        click.echo(f"[OK] 配置文件已初始化: {cfg.get_config_path()}")
        click.echo(f"[OK] 默认会话已创建: {cfg.default_session}  (ID: {sm.load(cfg.default_session).session_id})")
        click.echo(f"[提示] 使用 `irec --help` 查看所有命令")
    except FileExistsError:
        _save_current_session(cfg.default_session)
        click.echo(f"[OK] 配置文件已初始化: {cfg.get_config_path()}")
        click.echo(f"[!!] 默认会话已存在，已切换到当前会话")


# ============================================================
# config 命令组
# ============================================================
@main.group(help="管理配置文件")
def config():
    pass


@config.command("show", help="显示当前配置")
def config_show():
    cfg = Config.load()
    click.echo(f"配置文件: {cfg.get_config_path()}")
    click.echo(f"  金额容差       : ±{cfg.amount_tolerance} 元")
    click.echo(f"  日期容差       : ±{cfg.date_tolerance_days} 天")
    click.echo(f"  匹配策略       : {cfg.match_strategy}")
    click.echo(f"  会话存储目录   : {cfg.session_dir}")
    click.echo(f"  默认会话名     : {cfg.default_session}")
    click.echo(f"  客户名别名映射 :")
    if cfg.customer_name_aliases:
        for canonical, aliases in cfg.customer_name_aliases.items():
            click.echo(f"    {canonical} -> {', '.join(aliases)}")
    else:
        click.echo(f"    (无)")


@config.command("alias", help="添加客户名别名，例: irec config alias '华为技术' '华为' 'Huawei'")
@click.argument("canonical")
@click.argument("aliases", nargs=-1, required=True)
def config_alias(canonical, aliases):
    cfg = Config.load()
    if canonical not in cfg.customer_name_aliases:
        cfg.customer_name_aliases[canonical] = []
    for a in aliases:
        if a not in cfg.customer_name_aliases[canonical]:
            cfg.customer_name_aliases[canonical].append(a)
    cfg.save()
    click.echo(f"[OK] 已更新别名: {canonical} -> {cfg.customer_name_aliases[canonical]}")


@config.command("set", help="设置数值配置，例: irec config set amount_tol 0.05")
@click.argument("key", type=click.Choice(["amount_tol", "days_tol", "default_session", "session_dir"]))
@click.argument("value")
def config_set(key, value):
    cfg = Config.load()
    if key == "amount_tol":
        cfg.amount_tolerance = float(value)
    elif key == "days_tol":
        cfg.date_tolerance_days = int(value)
    elif key == "default_session":
        cfg.default_session = value
    elif key == "session_dir":
        cfg.session_dir = value
    cfg.save()
    click.echo(f"[OK] {key} 已设置为 {value}")


# ============================================================
# session 命令组
# ============================================================
@main.group(help="管理核对会话")
def session():
    pass


@session.command("create", help="创建新会话")
@click.argument("name")
def session_create(name):
    cfg, sm = _load_cfg_and_sm()
    try:
        s = sm.create(name)
        _save_current_session(name)
        click.echo(f"[OK] 会话已创建: {name}  (ID: {s.session_id})")
        click.echo(_fmt_status_header(sm.status_summary(s)))
    except FileExistsError as e:
        raise click.ClickException(str(e))


@session.command("list", help="列出所有会话")
def session_list():
    cfg, sm = _load_cfg_and_sm()
    sessions = sm.list_sessions()
    current = _get_current_session()
    if not sessions:
        click.echo("(暂无会话，使用 `irec session create <名称>` 创建)")
        return
    click.echo(f"{'名称':<20} {'会话ID':<14} {'创建时间':<22} {'最后更新':<22} 当前")
    click.echo("-" * 96)
    for s in sessions:
        marker = "<==" if s["name"] == current else ""
        click.echo(f"{s['name']:<20} {s['session_id']:<14} {s['created_at']:<22} {s['updated_at']:<22} {marker}")


@session.command("switch", help="切换当前会话")
@click.argument("name")
def session_switch(name):
    cfg, sm = _load_cfg_and_sm()
    try:
        s = sm.load(name)
        _save_current_session(name)
        click.echo(f"[OK] 已切换到会话: {name}")
        click.echo(_fmt_status_header(sm.status_summary(s)))
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@session.command("delete", help="删除会话（不可恢复）")
@click.argument("name")
@click.option("--yes", is_flag=True, help="跳过确认")
def session_delete(name, yes):
    cfg, sm = _load_cfg_and_sm()
    if not yes:
        click.confirm(f"确定删除会话 '{name}' ？此操作不可恢复", abort=True)
    try:
        sm.delete(name)
        if _get_current_session() == name:
            try:
                _save_current_session(cfg.default_session)
                sm.load(cfg.default_session)
            except FileNotFoundError:
                if os.path.exists(STATE_FILE):
                    os.remove(STATE_FILE)
        click.echo(f"[OK] 会话已删除: {name}")
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


# ============================================================
# status 命令
# ============================================================
@main.command(help="显示当前会话状态")
@click.option("--session", "-s", "session_name", help="指定会话名称，默认当前会话")
def status(session_name):
    cfg, sm, sess = _load_session(session_name)
    click.echo(_fmt_status_header(sm.status_summary(sess)))


# ============================================================
# import 命令组
# ============================================================
@main.group(help="导入发票或银行流水CSV")
def imp():
    pass


@imp.command("invoice", help="导入发票CSV")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--session", "-s", "session_name", help="指定会话")
def imp_invoice(csv_path, session_name):
    cfg, sm, sess = _load_session(session_name)
    try:
        result = import_invoices(sess, sm, csv_path)
        sm.save(sess)
        click.echo(f"[OK] 已导入发票 {result['count']} 张")
        click.echo(_fmt_status_header(sm.status_summary(sess)))
    except ImportError as e:
        err_out = [str(e)]
        if e.errors:
            for err in e.errors:
                err_out.append(f"  ✗ {err}")
        raise click.ClickException("\n".join(err_out))


@imp.command("txn", help="导入银行流水CSV")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--session", "-s", "session_name", help="指定会话")
def imp_txn(csv_path, session_name):
    cfg, sm, sess = _load_session(session_name)
    try:
        result = import_transactions(sess, sm, csv_path)
        sm.save(sess)
        click.echo(f"[OK] 已导入流水 {result['count']} 笔")
        click.echo(_fmt_status_header(sm.status_summary(sess)))
    except ImportError as e:
        err_out = [str(e)]
        if e.errors:
            for err in e.errors:
                err_out.append(f"  ✗ {err}")
        raise click.ClickException("\n".join(err_out))


# ============================================================
# match 命令
# ============================================================
@main.command(help="按规则自动匹配")
@click.option("--session", "-s", "session_name", help="指定会话")
@click.option("--dry-run", is_flag=True, help="预览不保存")
def match(session_name, dry_run):
    cfg, sm, sess = _load_session(session_name)
    result = auto_match(sess, sm, cfg)
    if not dry_run:
        sm.save(sess)
    prefix = "[预览]" if dry_run else "[OK]"
    click.echo(f"{prefix} 自动匹配完成: 新匹配 {result['matches_count']} 组, "
               f"涉及发票 {result['invoices_matched']} 张, 流水 {result['transactions_matched']} 笔")
    if result["matches"]:
        for m in result["matches"]:
            invs = [sess.invoices[i].invoice_no for i in m.invoice_ids if i in sess.invoices]
            txns = [sess.transactions[t].txn_id for t in m.transaction_ids if t in sess.transactions]
            click.echo(f"  #{m.id}  {m.match_type:<6}  发票[{','.join(invs)}]  <->  流水[{','.join(txns)}]  ({m.notes})")
    click.echo(_fmt_status_header(sm.status_summary(sess)))


# ============================================================
# manual 命令组
# ============================================================
@main.group(help="人工操作：匹配、挂起、撤销")
def manual():
    pass


@manual.command("match", help="人工匹配，支持多对多。例: irec manual match -i INV001 -i INV002 -t TXN099")
@click.option("--invoice", "-i", "invoices", multiple=True, required=True, help="发票ID或发票号，可重复")
@click.option("--txn", "-t", "txns", multiple=True, required=True, help="流水ID或流水号，可重复")
@click.option("--note", "-n", help="备注")
@click.option("--session", "-s", "session_name", help="指定会话")
def manual_match_cmd(invoices, txns, note, session_name):
    cfg, sm, sess = _load_session(session_name)
    result = manual_match(sess, sm, list(invoices), list(txns), note or "")
    if not result["success"]:
        raise click.ClickException(result["error"])
    sm.save(sess)
    m = result["match"]
    invs = [sess.invoices[i].invoice_no for i in m.invoice_ids if i in sess.invoices]
    tns = [sess.transactions[t].txn_id for t in m.transaction_ids if t in sess.transactions]
    click.echo(f"[OK] 人工匹配成功 #{m.id}")
    click.echo(f"     发票[{','.join(invs)}]  <->  流水[{','.join(tns)}]")
    click.echo(_fmt_status_header(sm.status_summary(sess)))


@manual.command("suspend-inv", help="挂起发票")
@click.argument("identifier")
@click.option("--reason", "-r", help="挂起原因")
@click.option("--session", "-s", "session_name", help="指定会话")
def manual_suspend_inv(identifier, reason, session_name):
    cfg, sm, sess = _load_session(session_name)
    r = suspend_invoice(sess, sm, identifier, reason or "")
    if not r["success"]:
        raise click.ClickException(r["error"])
    sm.save(sess)
    click.echo(f"[OK] 发票已挂起: {r['invoice'].invoice_no}  原因: {r['invoice'].suspend_reason}")


@manual.command("suspend-txn", help="挂起流水")
@click.argument("identifier")
@click.option("--reason", "-r", help="挂起原因")
@click.option("--session", "-s", "session_name", help="指定会话")
def manual_suspend_txn(identifier, reason, session_name):
    cfg, sm, sess = _load_session(session_name)
    r = suspend_transaction(sess, sm, identifier, reason or "")
    if not r["success"]:
        raise click.ClickException(r["error"])
    sm.save(sess)
    click.echo(f"[OK] 流水已挂起: {r['transaction'].txn_id}  原因: {r['transaction'].suspend_reason}")


@manual.command("unsuspend-inv", help="取消发票挂起")
@click.argument("identifier")
@click.option("--session", "-s", "session_name", help="指定会话")
def manual_unsuspend_inv(identifier, session_name):
    cfg, sm, sess = _load_session(session_name)
    r = unsuspend_invoice(sess, sm, identifier)
    if not r["success"]:
        raise click.ClickException(r["error"])
    sm.save(sess)
    click.echo(f"[OK] 发票已取消挂起: {r['invoice'].invoice_no}")


@manual.command("unsuspend-txn", help="取消流水挂起")
@click.argument("identifier")
@click.option("--session", "-s", "session_name", help="指定会话")
def manual_unsuspend_txn(identifier, session_name):
    cfg, sm, sess = _load_session(session_name)
    r = unsuspend_transaction(sess, sm, identifier)
    if not r["success"]:
        raise click.ClickException(r["error"])
    sm.save(sess)
    click.echo(f"[OK] 流水已取消挂起: {r['transaction'].txn_id}")


@manual.command("reverse", help="撤销匹配记录")
@click.argument("match_id")
@click.option("--reason", "-r", help="撤销原因")
@click.option("--session", "-s", "session_name", help="指定会话")
def manual_reverse(match_id, reason, session_name):
    cfg, sm, sess = _load_session(session_name)
    r = reverse_match(sess, sm, match_id, reason or "")
    if not r["success"]:
        raise click.ClickException(r["error"])
    sm.save(sess)
    m = r["match"]
    click.echo(f"[OK] 已撤销匹配 #{m.id}  原因: {m.reversed_reason}")
    click.echo(_fmt_status_header(sm.status_summary(sess)))


# ============================================================
# list 命令组
# ============================================================
@main.group("show", help="查看列表：未匹配、匹配、历史")
def show():
    pass


@show.command("unmatched", help="查看未匹配项（待复核）")
@click.option("--with-suspended", is_flag=True, help="包含挂起项")
@click.option("--session", "-s", "session_name", help="指定会话")
def list_unmatched_cmd(with_suspended, session_name):
    cfg, sm, sess = _load_session(session_name)
    data = list_unmatched(sess, with_suspended)
    click.echo(_fmt_status_header(sm.status_summary(sess)))
    click.echo()
    click.echo(f"▶ 未匹配发票 ({len(data['invoices'])}):")
    if data["invoices"]:
        click.echo(f"  {'ID':<14} {'发票号':<14} {'客户名称':<20} {'金额':>12} {'日期':<12} {'挂起':<6}")
        click.echo("  " + "-" * 82)
        for i in data["invoices"]:
            sus = "是:" + i["suspend_reason"] if i["suspended"] else "否"
            click.echo(f"  {i['id']:<14} {i['invoice_no']:<14} {i['customer_name'][:20]:<20} {i['amount']:>12,.2f} {i['invoice_date']:<12} {sus:<6}")
    else:
        click.echo("  (无)")
    click.echo()
    click.echo(f"▶ 未匹配流水 ({len(data['transactions'])}):")
    if data["transactions"]:
        click.echo(f"  {'ID':<14} {'流水号':<18} {'对方单位':<22} {'金额':>12} {'日期':<12} {'挂起':<6}")
        click.echo("  " + "-" * 88)
        for t in data["transactions"]:
            sus = "是:" + t["suspend_reason"] if t["suspended"] else "否"
            click.echo(f"  {t['id']:<14} {t['txn_id']:<18} {t['counterparty'][:22]:<22} {t['amount']:>12,.2f} {t['txn_date']:<12} {sus:<6}")
    else:
        click.echo("  (无)")


@show.command("matches", help="查看匹配记录")
@click.option("--all", "show_all", is_flag=True, help="包含已撤销的匹配")
@click.option("--session", "-s", "session_name", help="指定会话")
def list_matches_cmd(show_all, session_name):
    cfg, sm, sess = _load_session(session_name)
    click.echo(_fmt_status_header(sm.status_summary(sess)))
    click.echo()
    matches = [m for m in sess.matches.values() if show_all or not m.reversed]
    matches = sorted(matches, key=lambda m: m.matched_at, reverse=True)
    click.echo(f"▶ 匹配记录 ({len(matches)}):")
    if matches:
        for m in matches:
            invs = [sess.invoices[i].invoice_no for i in m.invoice_ids if i in sess.invoices]
            txns = [sess.transactions[t].txn_id for t in m.transaction_ids if t in sess.transactions]
            inv_sum = sum(sess.invoices[i].amount for i in m.invoice_ids if i in sess.invoices)
            txn_sum = sum(sess.transactions[t].amount for t in m.transaction_ids if t in sess.transactions)
            rev_tag = " [已撤销]" if m.reversed else ""
            click.echo(f"  #{m.id}  [{m.match_type}]{rev_tag}  {m.matched_at}")
            click.echo(f"     发票: {', '.join(invs)}  合计 {inv_sum:,.2f}")
            click.echo(f"     流水: {', '.join(txns)}  合计 {txn_sum:,.2f}")
            if m.reversed:
                click.echo(f"     撤销时间: {m.reversed_at}  原因: {m.reversed_reason}")
            if m.notes:
                click.echo(f"     备注: {m.notes}")
    else:
        click.echo("  (无)")


@show.command("history", help="查看操作历史")
@click.option("--limit", "-n", type=int, default=50, help="显示条数")
@click.option("--action", help="按动作类型过滤（import_invoices, import_transactions, auto_match, manual_match, reverse_match, suspend_invoice, suspend_transaction, unsuspend_invoice, unsuspend_transaction）")
@click.option("--session", "-s", "session_name", help="指定会话")
def list_history_cmd(limit, action, session_name):
    cfg, sm, sess = _load_session(session_name)
    click.echo(_fmt_status_header(sm.status_summary(sess)))
    click.echo()
    data = list_history(sess, limit, action)
    click.echo(f"▶ 操作历史 ({len(data)}/{len(sess.history)}):")
    if data:
        for h in data:
            detail_str = json.dumps(h["details"], ensure_ascii=False)
            if len(detail_str) > 140:
                detail_str = detail_str[:140] + "..."
            click.echo(f"  [{h['timestamp']}] {h['action']:<24} :: {detail_str}")
    else:
        click.echo("  (无)")


# ============================================================
# report 命令
# ============================================================
@main.command(help="导出当前核对报告")
@click.option("--format", "-f", "fmt", type=click.Choice(["json", "csv", "both"]), default="both", help="导出格式")
@click.option("--output", "-o", "output_path", default="irec_report", help="输出路径（json写文件，csv写目录）")
@click.option("--session", "-s", "session_name", help="指定会话")
def report(fmt, output_path, session_name):
    cfg, sm, sess = _load_session(session_name)
    rpt = build_report(sess, cfg)
    paths = []
    if fmt in ("json", "both"):
        json_path = output_path if output_path.endswith(".json") else output_path + ".json"
        export_json(rpt, json_path)
        paths.append(("JSON 报告", json_path))
    if fmt in ("csv", "both"):
        csv_dir = output_path[:-5] if output_path.endswith(".json") else output_path
        csv_files = export_csv(rpt, csv_dir)
        for name, p in csv_files.items():
            paths.append((f"CSV-{name}", p))
    click.echo(f"[OK] 核对报告已导出")
    for tag, p in paths:
        click.echo(f"  {tag:<18} -> {os.path.abspath(p)}")
    click.echo()
    click.echo(_fmt_status_header(rpt["summary"]))


# ============================================================
# audit 命令组
# ============================================================
@main.group(help="审计包管理：一键归档/恢复/复盘结账状态，含摘要、配置、明细、指纹、日志")
def audit():
    pass


@audit.command("export", help="导出当前会话为审计归档包（含报告、指纹、日志全套）")
@click.option("--output", "-o", "filename", help="输出文件名（默认自动生成）")
@click.option("--notes", "-n", default="", help="审计包备注")
@click.option("--operator", default="", help="操作人")
@click.option("--session", "-s", "session_name", help="指定会话，默认当前会话")
def audit_export(filename, notes, operator, session_name):
    cfg, sm, sess = _load_session(session_name)
    audit_mgr = AuditManager()
    try:
        path, pkg = audit_mgr.export(sess, cfg, filename=filename, notes=notes, operator=operator)
        sm.save(sess)
        info = audit_mgr.info(os.path.basename(path)) or {}
        click.echo(f"[OK] 审计包已导出 -> {os.path.abspath(path)}")
        click.echo(f"  审计包ID      : {pkg.metadata.audit_id}")
        click.echo(f"  审计包版本    : v{pkg.metadata.audit_version} (格式 v{AUDIT_VERSION})")
        click.echo(f"  导出时间      : {pkg.metadata.created_at}")
        click.echo(f"  原会话        : {pkg.metadata.original_session_name} (ID: {pkg.metadata.original_session_id})")
        click.echo(f"  内容完整性    : {'✓ SHA-256 校验通过' if info.get('content_hash_valid') else '✗ 校验失败'}")
        click.echo(f"  发票/流水/匹配/历史: {info.get('invoices_count', 0)}/{info.get('transactions_count', 0)}/"
                   f"{info.get('matches_count', 0)}/{info.get('history_count', 0)}")
        click.echo(f"  来源文件数    : {info.get('source_files_count', 0)}")
        if operator:
            click.echo(f"  操作人        : {operator}")
        if notes:
            click.echo(f"  备注          : {notes}")
        click.echo()
        click.echo(f"  归档清单:")
        for fname, desc in pkg.manifest.get("files", {}).items():
            click.echo(f"    - {fname:<40}  {desc}")
    except Exception as e:
        raise click.ClickException(f"导出失败: {e}")


@audit.command("import", help="从审计归档包恢复为新会话")
@click.argument("audit_file")
@click.option("--as", "as_name", default=None, help="导入为新会话名称")
@click.option("--overwrite", is_flag=True, help="同名会话存在时覆盖（危险）")
@click.option("--reject", is_flag=True, help="同名会话存在时拒绝（默认行为）")
@click.option("--auto-rename", is_flag=True, help="同名会话存在时自动重命名（另存新副本）")
@click.option("--apply-config", is_flag=True, help="同时恢复审计包中的配置到当前工作目录")
@click.option("--switch", "do_switch", is_flag=True, help="导入后切换到新会话")
@click.option("--compare-session", "-c", "compare_session_name", default=None,
              help="指定会话进行来源重复对比，默认当前会话")
def audit_import(audit_file, as_name, overwrite, reject, auto_rename, apply_config, do_switch,
                 compare_session_name):
    cfg, sm = _load_cfg_and_sm()
    audit_mgr = AuditManager()

    try:
        analysis = audit_mgr.analyze(audit_file, current_config=cfg)
    except FileNotFoundError:
        raise click.ClickException(f"审计包不存在: {audit_file}")
    except ValueError as e:
        raise click.ClickException(f"审计包分析失败: {e}")

    conflict_count = sum([overwrite, reject, auto_rename])
    if conflict_count > 1:
        raise click.ClickException("--overwrite / --reject / --auto-rename 三选一，不能同时指定")
    if overwrite:
        conflict_mode = "overwrite"
    elif reject:
        conflict_mode = "reject"
    elif auto_rename:
        conflict_mode = "rename"
    else:
        conflict_mode = "ask"

    desired = as_name or analysis.get("original_session_name", "restored")
    if sm.exists(desired) and conflict_mode == "ask":
        if not click.confirm(
            f"\n会话 '{desired}' 已存在！\n"
            f"  - 输入 Y = 覆盖此会话（所有现有数据丢失）\n"
            f"  - 输入 N = 终止导入，请改用 --as <新名称> 或 --auto-rename"
        ):
            click.echo("[已取消] 导入终止。可用 --as <新名称> 重命名导入，或 --auto-rename 自动加后缀")
            return
        conflict_mode = "overwrite"

    if analysis.get("missing_config_keys") and not apply_config:
        click.echo(f"[提示] 审计包中缺少配置项: {', '.join(analysis['missing_config_keys'])}")
        click.echo(f"       导入时将使用当前配置（加 --apply-config 可恢复审计包里的配置）")

    if analysis.get("config_drift") and not apply_config:
        drift = analysis["config_drift"]
        click.echo(f"[提示] 配置漂移检测: 共 {len(drift)} 项与当前工作目录配置不同:")
        for k, v in drift.items():
            click.echo(f"       - {k}: 审计包={v['audit']}, 当前={v['current']}")
        click.echo(f"       加 --apply-config 可覆盖为审计包中的配置")

    if not analysis.get("version_ok", True):
        raise click.ClickException(f"版本不兼容: {analysis.get('version_message', '')}")

    if not analysis.get("content_hash_valid", False):
        if not click.confirm(
            f"\n[警告] 审计包内容哈希校验失败！文件可能被篡改或损坏。\n"
            f"       是否仍然尝试导入？（可能出现数据不一致）"
        ):
            click.echo("[已取消] 因完整性校验失败而终止")
            return

    compare_sess = None
    if compare_session_name:
        try:
            compare_sess = sm.load(compare_session_name)
        except FileNotFoundError:
            raise click.ClickException(f"对比会话不存在: {compare_session_name}")

    try:
        result = audit_mgr.import_audit(
            audit_file, sm,
            target_session_name=as_name,
            conflict_mode=conflict_mode,
            apply_config=apply_config,
            current_config=cfg,
            compare_session=compare_sess,
            save_precheck=True,
        )
    except FileExistsError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"导入失败: {e}")

    new_sess = sm.load(result["session_name"])
    summary = sm.status_summary(new_sess)

    click.echo(f"[OK] 审计包已导入为会话 '{result['session_name']}'")
    click.echo(f"  新会话ID      : {result['session_id']}")
    click.echo(f"  来源审计包ID  : {result['audit_id']}")
    click.echo(f"  预检ID        : {result.get('precheck_id', 'N/A')}")
    click.echo(f"  原会话        : {result['original_session_name']} (ID: {result['original_session_id']})")
    if result["overwritten"]:
        click.echo(f"  [!!]          : 已覆盖已存在的同名会话")
    if result["renamed"]:
        click.echo(f"  [!!]          : 因重名已自动重命名为 '{result['session_name']}'（另存新副本）")
    if apply_config:
        click.echo(f"  配置          : ✓ 已恢复审计包中的配置")
    if result.get("config_drift"):
        drift_keys = list(result["config_drift"].keys())
        click.echo(f"  配置漂移项    : {', '.join(drift_keys)}")
    if result.get("duplicate_sources"):
        click.echo(f"  重复来源文件  : {len(result['duplicate_sources'])} 个")
    if result["warnings"]:
        for w in result["warnings"]:
            click.echo(f"  {w}")
    if result["missing_config_keys"]:
        click.echo(f"  缺失配置项    : {', '.join(result['missing_config_keys'])} (使用默认值)")

    if do_switch:
        _save_current_session(result["session_name"])
        click.echo(f"  [OK]          : 已切换到新会话")

    click.echo()
    click.echo(_fmt_status_header(summary))


@audit.command("list", help="列出所有审计包")
def audit_list():
    audit_mgr = AuditManager()
    audits = audit_mgr.list_audits()
    if not audits:
        click.echo("(暂无审计包，使用 `irec audit export` 创建)")
        return
    click.echo(f"审计包存储目录: {audit_mgr.audit_dir}")
    click.echo()
    click.echo(f"{'文件':<42} {'审计ID':<16} {'创建时间':<22} {'原会话':<14} 发票 流水 匹配 历史 来源 完整性")
    click.echo("-" * 170)
    for a in audits:
        if a.get("audit_id") == "corrupt":
            click.echo(f"{a['file']:<42} {'[损坏]':<16} {'-':<22} {'-':<14} - - - - - -")
            continue
        hash_tag = "✓" if a.get("content_hash_valid") else "✗"
        click.echo(
            f"{a['file']:<42} {a['audit_id']:<16} {a['created_at']:<22} "
            f"{a['original_session_name'][:14]:<14} "
            f"{a.get('invoices_count', 0):>4} "
            f"{a.get('transactions_count', 0):>4} "
            f"{a.get('matches_count', 0):>4} "
            f"{a.get('history_count', 0):>4} "
            f"{a.get('source_files_count', 0):>4} "
            f"  {hash_tag}"
        )


@audit.command("info", help="查看单个审计包的详细信息")
@click.argument("audit_file")
def audit_info(audit_file):
    audit_mgr = AuditManager()
    try:
        cfg, _ = _load_cfg_and_sm()
        info = audit_mgr.info(audit_file)
        analysis = audit_mgr.analyze(audit_file, current_config=cfg)
    except FileNotFoundError:
        raise click.ClickException(f"审计包不存在: {audit_file}")
    except ValueError as e:
        raise click.ClickException(f"审计包读取失败: {e}")

    if not info:
        raise click.ClickException(f"无法读取审计包: {audit_file}")

    click.echo(f"═══════════════════════════════════════════════════════════════")
    click.echo(f"  审计包文件    : {info['file']}")
    click.echo(f"  完整路径      : {info['path']}")
    click.echo(f"───────────────────────────────────────────────────────────────")
    click.echo(f"  审计包ID      : {info['audit_id']}")
    click.echo(f"  审计包格式版本: v{info['audit_version']}  (当前支持 v{AUDIT_VERSION})")
    click.echo(f"  应用版本      : {info['app_version']}")
    click.echo(f"  创建时间      : {info['created_at']}")
    click.echo(f"───────────────────────────────────────────────────────────────")
    click.echo(f"  原会话名称    : {info['original_session_name']}")
    click.echo(f"  原会话ID      : {info['original_session_id']}")
    click.echo(f"  操作人        : {info.get('operator') or '(无)'}")
    click.echo(f"  备注          : {info.get('notes') or '(无)'}")
    click.echo(f"───────────────────────────────────────────────────────────────")
    version_tag = "✓ 兼容" if analysis.get("version_ok") else f"✗ {analysis.get('version_message', '不兼容')}"
    click.echo(f"  版本兼容性    : {version_tag}")
    hash_tag = "✓ 通过" if info.get("content_hash_valid") else "✗ 失败（文件可能被篡改）"
    click.echo(f"  完整性哈希    : {hash_tag}")
    cfg_tag = "✓ 完整" if analysis.get("config_complete") else f"⚠ 缺失: {', '.join(analysis.get('missing_config_keys', []))}"
    click.echo(f"  配置完整性    : {cfg_tag}")
    drift = analysis.get("config_drift", {})
    if drift:
        click.echo(f"  配置漂移      : {len(drift)} 项与当前配置不同:")
        for k, v in drift.items():
            click.echo(f"    - {k}: 审计包={v['audit']}, 当前={v['current']}")
    else:
        click.echo(f"  配置漂移      : ✓ 与当前配置一致")
    dup = analysis.get("imported_file_hashes", [])
    click.echo(f"  来源文件数    : {info.get('source_files_count', 0)}")
    click.echo(f"───────────────────────────────────────────────────────────────")
    s = info.get("summary", {})
    if s:
        click.echo(f"  发票统计      : 总{s.get('invoices', {}).get('total', 0)}  "
                   f"已匹配{s.get('invoices', {}).get('matched', 0)}  "
                   f"未匹配{s.get('invoices', {}).get('unmatched', 0)}  "
                   f"挂起{s.get('invoices', {}).get('suspended', 0)}")
        click.echo(f"  流水统计      : 总{s.get('transactions', {}).get('total', 0)}  "
                   f"已匹配{s.get('transactions', {}).get('matched', 0)}  "
                   f"未匹配{s.get('transactions', {}).get('unmatched', 0)}  "
                   f"挂起{s.get('transactions', {}).get('suspended', 0)}")
        click.echo(f"  匹配统计      : 活动{s.get('matches', {}).get('active', 0)}  "
                   f"已撤销{s.get('matches', {}).get('reversed', 0)}  "
                   f"历史操作{s.get('history_count', 0)}  "
                   f"已导入文件{s.get('imported_files', 0)}")
    click.echo(f"───────────────────────────────────────────────────────────────")
    if analysis.get("warnings"):
        click.echo(f"  警告:")
        for w in analysis["warnings"]:
            click.echo(f"    ⚠ {w}")
    if analysis.get("source_fingerprints"):
        fps = analysis["source_fingerprints"]
        click.echo(f"  来源文件指纹 ({len(fps)} 个):")
        for fh, fp in list(fps.items())[:5]:
            label = fp.get("file_label", fp.get("file_basename", fh))
            inv_c = fp.get("invoice_count", 0)
            txn_c = fp.get("transaction_count", 0)
            click.echo(f"    {fh[:16]}...  {label}  (发票{inv_c}张, 流水{txn_c}笔)")
        if len(fps) > 5:
            click.echo(f"    ... 等共 {len(fps)} 个")
    click.echo(f"═══════════════════════════════════════════════════════════════")


@audit.command("replay", help="将审计包的操作日志回放到指定会话（只追加历史，不还原数据）")
@click.argument("audit_file")
@click.option("--session", "-s", "session_name", help="目标会话，默认当前会话")
def audit_replay(audit_file, session_name):
    cfg, sm, sess = _load_session(session_name)
    audit_mgr = AuditManager()
    try:
        result = audit_mgr.replay_log(audit_file, sess, sm)
        sm.save(sess)
        click.echo(f"[OK] 操作日志已回放到会话 '{sess.name}'")
        click.echo(f"  回放条数      : {result['replayed_count']}")
        click.echo(f"  当前历史总数  : {len(sess.history)}")
    except Exception as e:
        raise click.ClickException(f"回放失败: {e}")


@audit.command("delete", help="删除审计包文件")
@click.argument("audit_file")
@click.option("--yes", is_flag=True, help="跳过确认")
def audit_delete(audit_file, yes):
    audit_mgr = AuditManager()
    if not yes:
        info = audit_mgr.info(audit_file)
        disp = audit_file
        if info:
            disp = f"{info['file']} (会话: {info['original_session_name']}, 创建: {info['created_at']})"
        click.confirm(f"确定删除审计包 {disp} ？此操作不可恢复", abort=True)
    try:
        audit_mgr.delete(audit_file)
        click.echo(f"[OK] 审计包已删除")
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@audit.command("check-sources", help="检查审计包与现有会话的导入来源重复情况")
@click.argument("audit_file")
@click.option("--session", "-s", "session_name", help="指定现有会话进行对比，默认当前会话")
def audit_check_sources(audit_file, session_name):
    from pathlib import Path
    cfg, sm, sess = _load_session(session_name)
    audit_mgr = AuditManager()
    try:
        from .audit import AuditPackage
        path = Path(audit_file)
        if not path.exists():
            path = audit_mgr.audit_dir / audit_file
            if not path.exists() and not audit_file.endswith(".irecaudit"):
                path = audit_mgr.audit_dir / (audit_file + ".irecaudit")

        analysis = audit_mgr.analyze(audit_file, current_config=cfg)
        audit_hashes = set(analysis.get("imported_file_hashes", []))
        sess_hashes = set(sess.imported_files.keys())
    except FileNotFoundError:
        raise click.ClickException(f"审计包不存在: {audit_file}")
    except ValueError as e:
        raise click.ClickException(f"审计包读取失败: {e}")

    common = audit_hashes & sess_hashes
    click.echo(f"对比: 审计包 '{analysis.get('original_session_name', '?')}'  <->  会话 '{sess.name}'")
    click.echo()
    if common:
        click.echo(f"[!!] 发现 {len(common)} 个重复导入来源（相同CSV已在两边导入过）:")
        for h in common:
            src_audit = "(审计包中标记缺失)"
            src_sess = "(会话中标记缺失)"
            for hh, label in sess.imported_files.items():
                if hh == h:
                    src_sess = label
            src_audit = f"hash {h[:12]}..."
            click.echo(f"    • {src_sess}  <->  {src_audit}")
        click.echo()
        click.echo(f"  这意味着如果两边独立做了匹配操作，数据可能出现不一致。")
        click.echo(f"  建议: 导入后仔细核对未匹配列表和匹配记录。")
    else:
        click.echo(f"[OK] 未发现重复导入来源。")

    audit_only = audit_hashes - sess_hashes
    sess_only = sess_hashes - audit_hashes
    click.echo()
    click.echo(f"仅在审计包中导入的文件: {len(audit_only)} 个")
    click.echo(f"仅在目标会话中导入的文件: {len(sess_only)} 个")


@audit.command("precheck", help="导入预检：不落库检测重名、冲突模式、配置漂移、缺失文件、版本兼容、重复来源等风险")
@click.argument("audit_file")
@click.option("--as", "as_name", default=None, help="目标会话名称，默认使用审计包原会话名")
@click.option("--overwrite", is_flag=True, help="同名会话存在时覆盖（危险）")
@click.option("--reject", is_flag=True, help="同名会话存在时拒绝（默认行为）")
@click.option("--auto-rename", is_flag=True, help="同名会话存在时自动重命名（另存新副本）")
@click.option("--apply-config", is_flag=True, help="同时恢复审计包中的配置到当前工作目录")
@click.option("--compare-session", "-c", "compare_session_name", default=None,
              help="指定会话进行来源重复对比，默认当前会话")
@click.option("--save/--no-save", default=True, help="是否保存预检结果（默认保存，跨重启可查）")
def audit_precheck(audit_file, as_name, overwrite, reject, auto_rename,
                   apply_config, compare_session_name, save):
    from .audit import PrecheckStore, PrecheckResult
    cfg, sm = _load_cfg_and_sm()
    audit_mgr = AuditManager()

    conflict_count = sum([overwrite, reject, auto_rename])
    if conflict_count > 1:
        raise click.ClickException("--overwrite / --reject / --auto-rename 三选一，不能同时指定")
    if overwrite:
        conflict_mode = "overwrite"
    elif reject:
        conflict_mode = "reject"
    elif auto_rename:
        conflict_mode = "rename"
    else:
        conflict_mode = "reject"

    compare_sess = None
    if compare_session_name:
        try:
            compare_sess = sm.load(compare_session_name)
        except FileNotFoundError:
            raise click.ClickException(f"对比会话不存在: {compare_session_name}")
    else:
        curr = _get_current_session()
        if curr and sm.exists(curr):
            compare_sess = sm.load(curr)

    try:
        result = audit_mgr.precheck(
            audit_file, sm,
            target_session_name=as_name,
            conflict_mode=conflict_mode,
            apply_config=apply_config,
            current_config=cfg,
            compare_session=compare_sess,
        )
    except FileNotFoundError:
        raise click.ClickException(f"审计包不存在: {audit_file}")
    except ValueError as e:
        raise click.ClickException(f"预检失败: {e}")

    if save:
        store = PrecheckStore()
        store.save(result)

    _print_precheck_result(result)


@audit.command("precheck-list", help="列出已保存的预检结果（跨重启可查）")
def audit_precheck_list():
    from .audit import PrecheckStore
    store = PrecheckStore()
    results = store.list()
    if not results:
        click.echo("(暂无预检记录，使用 `irec audit precheck <审计包>` 创建)")
        return
    click.echo(f"预检记录存储目录: {store.precheck_dir}")
    click.echo()
    click.echo(f"{'预检ID':<24} {'审计包':<30} {'预检时间':<20} {'目标会话':<24} 可导入")
    click.echo("-" * 128)
    for r in results[:20]:
        tag = "✓" if r.get("importable") else "✗"
        click.echo(
            f"{r.get('precheck_id', '')[:24]:<24} "
            f"{r.get('audit_file', '')[:30]:<30} "
            f"{r.get('precheck_at', '')[:20]:<20} "
            f"{r.get('target_session_name', '')[:24]:<24} "
            f"  {tag}"
        )
    if len(results) > 20:
        click.echo(f"... 等共 {len(results)} 条记录")


@audit.command("precheck-show", help="查看指定预检结果的详细信息")
@click.argument("precheck_id")
def audit_precheck_show(precheck_id):
    from .audit import PrecheckStore
    store = PrecheckStore()
    result = store.load(precheck_id)
    if not result:
        raise click.ClickException(f"预检记录不存在: {precheck_id}")
    _print_precheck_result(result)


@audit.command("precheck-clear", help="清空所有已保存的预检结果")
@click.option("--yes", is_flag=True, help="跳过确认")
def audit_precheck_clear(yes):
    from .audit import PrecheckStore
    store = PrecheckStore()
    if not yes:
        click.confirm("确定清空所有预检结果？此操作不可恢复", abort=True)
    count = store.clear()
    click.echo(f"[OK] 已清空 {count} 条预检记录")


def _print_precheck_result(result):
    from .audit import PrecheckResult
    click.echo()
    click.echo(f"═══════════════════════════════════════════════════════════════")
    click.echo(f"  导入预检报告")
    click.echo(f"═══════════════════════════════════════════════════════════════")
    click.echo(f"  预检ID        : {result.precheck_id}")
    click.echo(f"  预检时间      : {result.precheck_at}")
    click.echo(f"  审计包文件    : {result.audit_file}")
    click.echo(f"  审计包ID      : {result.audit_id}")
    click.echo(f"  目标会话名    : {result.target_session_name}")
    click.echo(f"  冲突模式      : {result.conflict_mode}")
    click.echo(f"  应用配置      : {'是' if result.apply_config else '否'}")
    click.echo(f"───────────────────────────────────────────────────────────────")

    status = "✓ 可以导入" if result.importable else "✗ 无法导入"
    status_color = "green" if result.importable else "red"
    click.echo(f"  预检结论      : {status}")
    click.echo()

    click.echo(f"  ▶ 会话冲突分析")
    if result.session_exists:
        click.echo(f"    目标会话已存在: 是")
        if result.will_overwrite:
            click.echo(f"    处理方式: 覆盖（overwrite）→ 现有会话数据将被替换")
        elif result.will_rename:
            click.echo(f"    处理方式: 自动重命名（auto-rename）→ 另存为新副本")
            click.echo(f"    重命名为: {result.rename_to}")
        elif result.will_reject:
            click.echo(f"    处理方式: 拒绝（reject）→ 导入将被中止")
        else:
            click.echo(f"    处理方式: 未知")
    else:
        click.echo(f"    目标会话已存在: 否")
        click.echo(f"    处理方式: 创建新会话")
    click.echo(f"    最终会话名: {result.resolved_name}")
    click.echo()

    click.echo(f"  ▶ 版本与完整性检查")
    ver_tag = "✓ 兼容" if result.version_ok else f"✗ {result.version_message}"
    click.echo(f"    版本兼容性  : {ver_tag}")
    hash_tag = "✓ 通过" if result.content_hash_valid else "✗ 失败（文件可能被篡改）"
    click.echo(f"    完整性哈希  : {hash_tag}")
    click.echo()

    click.echo(f"  ▶ 配置检查")
    if result.missing_config_keys:
        click.echo(f"    缺失配置项  : {len(result.missing_config_keys)} 项")
        for k in result.missing_config_keys:
            click.echo(f"      - {k}")
    else:
        click.echo(f"    配置完整性  : ✓ 完整")

    if result.config_drift:
        drift = result.config_drift
        click.echo(f"    配置漂移    : {len(drift)} 项与当前工作目录不同")
        for k, v in drift.items():
            click.echo(f"      - {k}:")
            click.echo(f"        审计包  : {v['audit']}")
            click.echo(f"        当前    : {v['current']}")
        if not result.apply_config:
            click.echo(f"    提示       : 导入时将使用当前配置（加 --apply-config 可恢复审计包配置）")
    else:
        click.echo(f"    配置漂移    : ✓ 与当前配置一致")
    click.echo()

    dup = result.duplicate_sources
    click.echo(f"  ▶ 重复导入来源检查")
    if dup.get("both"):
        click.echo(f"    重复来源    : {len(dup['both'])} 个（两边都导入过相同文件）")
        for item in dup["both"][:5]:
            click.echo(f"      • {item}")
        if len(dup["both"]) > 5:
            click.echo(f"      ... 等共 {len(dup['both'])} 个")
        click.echo(f"    注意       : 若两边独立做过匹配操作，数据可能不一致")
        click.echo(f"    建议       : 导入后仔细核对未匹配列表和匹配记录")
    else:
        click.echo(f"    重复来源    : ✓ 未发现重复导入来源")
    click.echo(f"    仅审计包    : {len(dup.get('audit_only', []))} 个文件")
    click.echo(f"    仅目标会话  : {len(dup.get('target_only', []))} 个文件")
    click.echo()

    s = result.summary
    if s:
        click.echo(f"  ▶ 数据概览")
        click.echo(f"    原会话名    : {s.get('original_session_name', '?')}")
        click.echo(f"    原会话ID    : {s.get('original_session_id', '?')}")
        click.echo(f"    发票数      : {s.get('invoices_count', 0)}")
        click.echo(f"    流水数      : {s.get('transactions_count', 0)}")
        click.echo(f"    匹配记录    : {s.get('matches_count', 0)}")
        click.echo(f"    历史操作    : {s.get('history_count', 0)}")
        click.echo(f"    来源文件    : {s.get('imported_files_count', 0)}")
        click.echo()

    if result.warnings:
        click.echo(f"  ▶ 警告 ({len(result.warnings)} 条)")
        for w in result.warnings:
            click.echo(f"    ⚠ {w}")
        click.echo()

    if result.errors:
        click.echo(f"  ▶ 错误 ({len(result.errors)} 条)")
        for e in result.errors:
            click.echo(f"    ✗ {e}")
        click.echo()

    click.echo(f"═══════════════════════════════════════════════════════════════")

    if result.importable:
        import_cmd = f"irec audit import \"{result.audit_file}\""
        if result.target_session_name:
            import_cmd += f" --as \"{result.target_session_name}\""
        if result.conflict_mode == "overwrite":
            import_cmd += " --overwrite"
        elif result.conflict_mode == "rename":
            import_cmd += " --auto-rename"
        if result.apply_config:
            import_cmd += " --apply-config"
        click.echo(f"  执行导入: {import_cmd}")
    else:
        click.echo(f"  建议: 请先解决上述错误后再尝试导入")

    click.echo(f"═══════════════════════════════════════════════════════════════")
    click.echo()


# ============================================================
# snapshot 命令组
# ============================================================
@main.group(help="快照管理：导出/导入/查看核对会话快照")
def snapshot():
    pass


@snapshot.command("export", help="导出当前会话为快照包")
@click.option("--output", "-o", "filename", help="输出文件名（默认自动生成）")
@click.option("--notes", "-n", default="", help="快照备注")
@click.option("--session", "-s", "session_name", help="指定会话，默认当前会话")
def snapshot_export(filename, notes, session_name):
    cfg, sm, sess = _load_session(session_name)
    snap_mgr = SnapshotManager()
    try:
        path, pkg = snap_mgr.export(sess, cfg, filename=filename, notes=notes)
        sm.save(sess)
        info = snap_mgr.info(os.path.basename(path)) or {}
        click.echo(f"[OK] 快照已导出 -> {os.path.abspath(path)}")
        click.echo(f"  快照ID        : {pkg.metadata.snapshot_id}")
        click.echo(f"  快照版本      : v{pkg.metadata.snapshot_version} (格式 v{SNAPSHOT_VERSION})")
        click.echo(f"  导出时间      : {pkg.metadata.created_at}")
        click.echo(f"  原会话        : {pkg.metadata.original_session_name} (ID: {pkg.metadata.original_session_id})")
        click.echo(f"  内容完整性    : {'✓ SHA-256 校验通过' if info.get('content_hash_valid') else '✗ 校验失败'}")
        click.echo(f"  发票/流水/匹配/历史: {info.get('invoices_count', 0)}/{info.get('transactions_count', 0)}/"
                   f"{info.get('matches_count', 0)}/{info.get('history_count', 0)}")
        if notes:
            click.echo(f"  备注          : {notes}")
    except Exception as e:
        raise click.ClickException(f"导出失败: {e}")


@snapshot.command("import", help="从快照包恢复为新会话")
@click.argument("snapshot_file")
@click.option("--as", "as_name", default=None, help="导入为新会话名称")
@click.option("--overwrite", is_flag=True, help="同名会话存在时覆盖（危险）")
@click.option("--reject", is_flag=True, help="同名会话存在时拒绝（默认行为）")
@click.option("--auto-rename", is_flag=True, help="同名会话存在时自动重命名")
@click.option("--apply-config", is_flag=True, help="同时恢复快照中的配置到当前工作目录")
@click.option("--switch", "do_switch", is_flag=True, help="导入后切换到新会话")
def snapshot_import(snapshot_file, as_name, overwrite, reject, auto_rename, apply_config, do_switch):
    cfg, sm = _load_cfg_and_sm()
    snap_mgr = SnapshotManager()

    try:
        analysis = snap_mgr.analyze(snapshot_file)
    except FileNotFoundError:
        raise click.ClickException(f"快照文件不存在: {snapshot_file}")
    except ValueError as e:
        raise click.ClickException(f"快照分析失败: {e}")

    conflict_count = sum([overwrite, reject, auto_rename])
    if conflict_count > 1:
        raise click.ClickException("--overwrite / --reject / --auto-rename 三选一，不能同时指定")
    if overwrite:
        conflict_mode = "overwrite"
    elif reject:
        conflict_mode = "reject"
    elif auto_rename:
        conflict_mode = "rename"
    else:
        conflict_mode = "ask"

    desired = as_name or analysis.get("original_session_name", "restored")
    if sm.exists(desired) and conflict_mode == "ask":
        if not click.confirm(
            f"\n会话 '{desired}' 已存在！\n"
            f"  - 输入 Y = 覆盖此会话（所有现有数据丢失）\n"
            f"  - 输入 N = 终止导入，请改用 --as <新名称> 或 --auto-rename"
        ):
            click.echo("[已取消] 导入终止。可用 --as <新名称> 重命名导入，或 --auto-rename 自动加后缀")
            return
        conflict_mode = "overwrite"

    if analysis.get("missing_config_keys") and not apply_config:
        click.echo(f"[提示] 快照中缺少配置项: {', '.join(analysis['missing_config_keys'])}")
        click.echo(f"       导入时将使用当前配置（加 --apply-config 可恢复快照里的配置）")

    if not analysis.get("version_ok", True):
        raise click.ClickException(f"版本不兼容: {analysis.get('version_message', '')}")

    if not analysis.get("content_hash_valid", False):
        if not click.confirm(
            f"\n[警告] 快照内容哈希校验失败！文件可能被篡改或损坏。\n"
            f"       是否仍然尝试导入？（可能出现数据不一致）"
        ):
            click.echo("[已取消] 因完整性校验失败而终止")
            return

    try:
        result = snap_mgr.import_snapshot(
            snapshot_file, sm,
            target_session_name=as_name,
            conflict_mode=conflict_mode,
            apply_config=apply_config,
        )
    except FileExistsError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"导入失败: {e}")

    new_sess = sm.load(result["session_name"])
    summary = sm.status_summary(new_sess)

    click.echo(f"[OK] 快照已导入为会话 '{result['session_name']}'")
    click.echo(f"  新会话ID      : {result['session_id']}")
    click.echo(f"  来源快照ID    : {result['snapshot_id']}")
    click.echo(f"  原会话        : {result['original_session_name']} (ID: {result['original_session_id']})")
    if result["overwritten"]:
        click.echo(f"  [!!]          : 已覆盖已存在的同名会话")
    if result["renamed"]:
        click.echo(f"  [!!]          : 因重名已自动重命名为 '{result['session_name']}'")
    if apply_config:
        click.echo(f"  配置          : ✓ 已恢复快照中的配置")
    if result["warnings"]:
        for w in result["warnings"]:
            click.echo(f"  {w}")
    if result["missing_config_keys"]:
        click.echo(f"  缺失配置项    : {', '.join(result['missing_config_keys'])} (使用默认值)")

    if do_switch:
        _save_current_session(result["session_name"])
        click.echo(f"  [OK]          : 已切换到新会话")

    click.echo()
    click.echo(_fmt_status_header(summary))


@snapshot.command("list", help="列出所有快照")
def snapshot_list():
    snap_mgr = SnapshotManager()
    snaps = snap_mgr.list_snapshots()
    if not snaps:
        click.echo("(暂无快照，使用 `irec snapshot export` 创建)")
        return
    click.echo(f"快照存储目录: {snap_mgr.snapshot_dir}")
    click.echo()
    click.echo(f"{'文件':<40} {'快照ID':<18} {'创建时间':<22} {'原会话':<16} 发票 流水 匹配 历史 完整性")
    click.echo("-" * 160)
    for s in snaps:
        if s.get("snapshot_id") == "corrupt":
            click.echo(f"{s['file']:<40} {'[损坏]':<18} {'-':<22} {'-':<16} - - - - -")
            continue
        hash_tag = "✓" if s.get("content_hash_valid") else "✗"
        click.echo(
            f"{s['file']:<40} {s['snapshot_id']:<18} {s['created_at']:<22} "
            f"{s['original_session_name'][:16]:<16} "
            f"{s.get('invoices_count', 0):>4} "
            f"{s.get('transactions_count', 0):>4} "
            f"{s.get('matches_count', 0):>4} "
            f"{s.get('history_count', 0):>4} "
            f"  {hash_tag}"
        )


@snapshot.command("info", help="查看单个快照的详细信息")
@click.argument("snapshot_file")
def snapshot_info(snapshot_file):
    snap_mgr = SnapshotManager()
    try:
        info = snap_mgr.info(snapshot_file)
        analysis = snap_mgr.analyze(snapshot_file)
    except FileNotFoundError:
        raise click.ClickException(f"快照文件不存在: {snapshot_file}")
    except ValueError as e:
        raise click.ClickException(f"快照读取失败: {e}")

    if not info:
        raise click.ClickException(f"无法读取快照: {snapshot_file}")

    click.echo(f"═══════════════════════════════════════════════════════════════")
    click.echo(f"  快照文件      : {info['file']}")
    click.echo(f"  完整路径      : {info['path']}")
    click.echo(f"───────────────────────────────────────────────────────────────")
    click.echo(f"  快照ID        : {info['snapshot_id']}")
    click.echo(f"  快照格式版本  : v{info['snapshot_version']}  (当前支持 v{SNAPSHOT_VERSION})")
    click.echo(f"  应用版本      : {info['app_version']}")
    click.echo(f"  创建时间      : {info['created_at']}")
    click.echo(f"───────────────────────────────────────────────────────────────")
    click.echo(f"  原会话名称    : {info['original_session_name']}")
    click.echo(f"  原会话ID      : {info['original_session_id']}")
    click.echo(f"  备注          : {info.get('notes') or '(无)'}")
    click.echo(f"───────────────────────────────────────────────────────────────")
    version_tag = "✓ 兼容" if analysis.get("version_ok") else f"✗ {analysis.get('version_message', '不兼容')}"
    click.echo(f"  版本兼容性    : {version_tag}")
    hash_tag = "✓ 通过" if info.get("content_hash_valid") else "✗ 失败（文件可能被篡改）"
    click.echo(f"  完整性哈希    : {hash_tag}")
    cfg_tag = "✓ 完整" if analysis.get("config_complete") else f"⚠ 缺失: {', '.join(analysis.get('missing_config_keys', []))}"
    click.echo(f"  配置完整性    : {cfg_tag}")
    click.echo(f"───────────────────────────────────────────────────────────────")
    s = info.get("summary", {})
    if s:
        click.echo(f"  发票统计      : 总{s.get('invoices', {}).get('total', 0)}  "
                   f"已匹配{s.get('invoices', {}).get('matched', 0)}  "
                   f"未匹配{s.get('invoices', {}).get('unmatched', 0)}  "
                   f"挂起{s.get('invoices', {}).get('suspended', 0)}")
        click.echo(f"  流水统计      : 总{s.get('transactions', {}).get('total', 0)}  "
                   f"已匹配{s.get('transactions', {}).get('matched', 0)}  "
                   f"未匹配{s.get('transactions', {}).get('unmatched', 0)}  "
                   f"挂起{s.get('transactions', {}).get('suspended', 0)}")
        click.echo(f"  匹配统计      : 活动{s.get('matches', {}).get('active', 0)}  "
                   f"已撤销{s.get('matches', {}).get('reversed', 0)}  "
                   f"历史操作{s.get('history_count', 0)}  "
                   f"已导入文件{s.get('imported_files', 0)}")
    click.echo(f"───────────────────────────────────────────────────────────────")
    if analysis.get("warnings"):
        click.echo(f"  警告:")
        for w in analysis["warnings"]:
            click.echo(f"    ⚠ {w}")
    if analysis.get("imported_file_hashes"):
        click.echo(f"  已导入文件来源标记 ({len(analysis['imported_file_hashes'])} 个，用于防止重复导入):")
        for h in analysis["imported_file_hashes"][:5]:
            click.echo(f"    {h[:16]}...")
        if len(analysis["imported_file_hashes"]) > 5:
            click.echo(f"    ... 等共 {len(analysis['imported_file_hashes'])} 个")
    click.echo(f"═══════════════════════════════════════════════════════════════")


@snapshot.command("delete", help="删除快照文件")
@click.argument("snapshot_file")
@click.option("--yes", is_flag=True, help="跳过确认")
def snapshot_delete(snapshot_file, yes):
    snap_mgr = SnapshotManager()
    if not yes:
        info = snap_mgr.info(snapshot_file)
        disp = snapshot_file
        if info:
            disp = f"{info['file']} (会话: {info['original_session_name']}, 创建: {info['created_at']})"
        click.confirm(f"确定删除快照 {disp} ？此操作不可恢复", abort=True)
    try:
        snap_mgr.delete(snapshot_file)
        click.echo(f"[OK] 快照已删除")
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@snapshot.command("check-conflicts", help="检查快照与现有会话的导入来源冲突")
@click.argument("snapshot_file")
@click.option("--session", "-s", "session_name", help="指定现有会话进行对比，默认当前会话")
def snapshot_check_conflicts(snapshot_file, session_name):
    cfg, sm, sess = _load_session(session_name)
    snap_mgr = SnapshotManager()
    try:
        from .snapshot import SnapshotPackage
        from pathlib import Path
        path = Path(snapshot_file)
        if not path.exists():
            path = snap_mgr.snapshot_dir / snapshot_file
            if not path.exists() and not snapshot_file.endswith(".irecsnap"):
                path = snap_mgr.snapshot_dir / (snapshot_file + ".irecsnap")

        analysis = snap_mgr.analyze(snapshot_file)
        snap_hashes = set(analysis.get("imported_file_hashes", []))
        sess_hashes = set(sess.imported_files.keys())
    except FileNotFoundError:
        raise click.ClickException(f"快照文件不存在: {snapshot_file}")
    except ValueError as e:
        raise click.ClickException(f"快照读取失败: {e}")

    common = snap_hashes & sess_hashes
    click.echo(f"对比: 快照 '{analysis.get('original_session_name', '?')}'  <->  会话 '{sess.name}'")
    click.echo()
    if common:
        click.echo(f"[!!] 发现 {len(common)} 个重复导入来源（相同CSV已在两边导入过）:")
        for h in common:
            src_snap = "(快照中标记缺失)"
            src_sess = "(会话中标记缺失)"
            # 查找实际标记
            for hh, label in sess.imported_files.items():
                if hh == h:
                    src_sess = label
            # 从快照分析中找不到 imported_files 的 value，只标记 hash
            src_snap = f"hash {h[:12]}..."
            click.echo(f"    • {src_sess}  <->  {src_snap}")
        click.echo()
        click.echo(f"  这意味着如果两边独立做了匹配操作，数据可能出现不一致。")
        click.echo(f"  建议: 导入后仔细核对未匹配列表和匹配记录。")
    else:
        click.echo(f"[OK] 未发现重复导入来源。")

    snap_only = snap_hashes - sess_hashes
    sess_only = sess_hashes - snap_hashes
    click.echo()
    click.echo(f"仅在快照中导入的文件: {len(snap_only)} 个")
    click.echo(f"仅在目标会话中导入的文件: {len(sess_only)} 个")


if __name__ == "__main__":
    main()
