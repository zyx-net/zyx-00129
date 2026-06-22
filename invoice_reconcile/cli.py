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


if __name__ == "__main__":
    main()
