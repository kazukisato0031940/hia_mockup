# -*- coding: utf-8 -*-
"""
産業医面談管理（別システムの機能を本システムへ組み込んだモジュール）

労働安全衛生法に基づく「健康診断」「長時間労働」「ストレスチェック」の情報を横断し、
産業医面談の対象者抽出から面談記録・労基署報告までを支援する。

  A．認証・共通基盤        本体（app.py）の認証・ロール・操作ログをそのまま使う
  B．対象者スクリーニング  /oh              面談候補の自動抽出と確認・処理
  C．健診受診管理          /oh/kenshin      定期健診・深夜健診の受診状況
  D．メール配信            /oh/mail         受診勧奨・ストレスチェック案内
  E．面談結果入力          /oh/interview    面談記録（産業医のみ）
  F．労基署報告            /oh/report       様式第6号・ストレスチェック結果等報告書
  G．データ取込            /oh/upload       労働時間・ストレスチェック・健診結果

ロール
  産業医（doctor）… 医学的判断（面談対象の承認・就業区分の判定・面談所見・報告書の記名）
  人事  （hr）    … 運用事務（取込・マスタ・メール配信・対応区分・報告書の出力）
  当社スタッフ・健保担当者・企業担当者は「人事」と同じ運用事務の範囲で操作できる。
  医学的判断の項目は、人事側から送信されてもサーバ側で無視する。

※ 医薬品・疾患・治療に関する表示は参考情報であり、就業判定・措置の最終判断は
   産業医・医師が行う前提の画面構成にしている。
"""
import csv
import io
import re
from datetime import date, datetime
from functools import wraps

from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, url_for)

bp = Blueprint("oh", __name__, url_prefix="/oh")

# 本体（app.py）のモジュール。init_app で受け取る。
H = None


def init_app(flask_app, app_module):
    """app.py から呼び出して、共通処理を受け取りブループリントを登録する"""
    global H
    H = app_module
    flask_app.register_blueprint(bp)
    return bp


# ================================================================ 区分の定義
# 総合判定は日本人間ドック・予防医療学会の判定区分（2026年4月1日改定）に準拠する
JUDGES = ["A", "B", "C", "D", "E"]
JUDGE_LABEL = {"A": "異常なし", "B": "軽度異常", "C": "要再検査・生活改善",
               "D": "要精密検査・治療", "E": "治療中"}
FINDING_JUDGES = ("C", "D", "E")          # 有所見（C判定以上）

WORK_CLASSES = ["通常勤務", "就業制限", "要休業"]
# 対応区分（ステータス）。人事の運用の流れにそって並べています。
#   未判定 →（人事が依頼）→ 産業医依頼中 →（産業医が就業区分を判定）→
#   通常勤務／就業制限／要休業 →（人事が案内を送付）→ 保健師対応中 →
#  （ご本人が検査結果を取り込む）→ 再検査対応済み
HR_WAIT = "未判定"
HR_REQ = "産業医依頼中"
HR_NURSE = "保健師対応中"
HR_DONE = "再検査対応済み"
HR_CLASSES = [HR_WAIT, HR_REQ] + WORK_CLASSES + [HR_NURSE, HR_DONE]
# 産業医の判定結果がそのまま入る区分（人事はここから案内を送ります）
HR_JUDGED = list(WORK_CLASSES)
# 産業医の検索条件に出すステータス（自分が判定するものだけ）
DOCTOR_HR_CLASSES = [HR_WAIT] + list(WORK_CLASSES)
# 以前の区分名 → いまの区分名（移行と取込のゆれ対策）
HR_CLASS_OLD = {"産業医判定済": "通常勤務", "要精査・加療指示": "保健師対応中",
                "経過観察": "通常勤務"}

# 対応区分（ステータス）ごとの「短い状態名」と、ロールごとに行うこと。
# 画面（一括処理・マイページ・面談ダッシュボード）に同じ内容を出すため、ここで一元管理します。
HR_CLASS_GUIDE = {
    HR_WAIT: {
        "short": "判定待ち",
        "action": "産業医へ依頼",
        "hr": ["健診結果の取り込み状況を確認する。",
               "産業医の判定対象者を抽出し、判定依頼を行う。",
               "判定期限・未対応者を管理する。"],
        "doctor": [],
        "nurse": [],
        "note": "ステータスの「産業医へ依頼」を押すと、産業医依頼中になります。",
    },
    HR_REQ: {
        "short": "産業医の判定待ち",
        "action": "",
        "hr": ["判定期限・未対応者を管理する。",
               "産業医からの判定結果を待つ。"],
        "doctor": ["健診結果を確認する。",
                   "必要に応じて過去の健診結果、業務内容、労働時間などを確認する。",
                   "就業区分や就業上の配慮について医学的な意見を入力する。"],
        "nurse": [],
        "note": "産業医が就業区分を判定すると、その区分（通常勤務・就業制限・要休業）が"
                "そのままステータスになります。",
    },
    "通常勤務": {
        "short": "産業医判定済",
        "action": "案内を送付",
        "hr": ["産業医の判定結果を確認する。",
               "対象者へ結果を通知する（ステータスのボタンから案内を送ります）。",
               "必要に応じて保健指導・面談を調整する。"],
        "doctor": ["健診結果を医学的に評価する。",
                   "通常勤務と判定する。",
                   "必要なフォローを意見として記録する。"],
        "nurse": [],
        "note": "",
    },
    "就業制限": {
        "short": "産業医判定済",
        "action": "案内を送付",
        "hr": ["就業上の措置が必要な対象者の対応計画を立てる。",
               "対象者へ結果通知・再検査／受診の案内を送る（ステータスのボタン）。",
               "受診状況や報告期限を管理する。"],
        "doctor": ["就業区分を「就業制限」と判定する。",
                   "業務上のリスクを評価し、必要な就業上の配慮を記録する。",
                   "精密検査・治療の必要性や緊急性を確認する。"],
        "nurse": ["面談（保健指導）を実施し、面談結果入力で記録する（確定すると対応済み）。"],
        "note": "受診・治療の指示（要精査・加療）は医療機関の判断によるもので、"
                "就業制限の判定とは別に管理します。",
    },
    "要休業": {
        "short": "産業医判定済",
        "action": "案内を送付",
        "hr": ["休業の手続き・社内調整を行う。",
               "対象者へ結果通知・受診の案内を送る（ステータスのボタン）。",
               "受診状況や報告期限を管理する。"],
        "doctor": ["就業区分を「要休業」と判定する。",
                   "就業上の措置・面談・フォローアップを判断する。",
                   "主治医との連携が必要かを確認する。"],
        "nurse": ["面談（保健指導）を実施し、面談結果入力で記録する（確定すると対応済み）。"],
        "note": "",
    },
    HR_NURSE: {
        "short": "フォロー中",
        "action": "",
        "hr": ["保健師への対応依頼を登録する。",
               "面談・保健指導の実施状況を確認する。",
               "必要な勤務上の配慮や社内調整を行う。"],
        "doctor": ["保健師からの相談・報告を受ける。",
                   "医学的判断が必要なケースを確認する。",
                   "必要に応じて面談、就業意見の見直し、主治医との連携を行う。"],
        "nurse": ["対象者への面談・保健指導・受診勧奨を実施する。",
                  "対応内容や次回フォロー日を記録する。"],
        "note": "ご本人が健康マイページから検査結果を取り込むと、"
                "再検査対応済みになります。",
    },
    HR_DONE: {
        "short": "対応完了",
        "action": "",
        "hr": ["再検査の受診・結果提出の状況を確認する。",
               "対応完了日を記録する。",
               "就業措置が継続中か、解除・見直しが必要かを確認する。"],
        "doctor": ["再検査結果や主治医の意見書などを確認する。",
                   "就業区分・就業上の配慮を再評価する。",
                   "継続フォロー、再検査、通常勤務への復帰などの意見を記録する。"],
        "nurse": [],
        "note": "",
    },
}
# ロールの並びと見出し（画面で同じ順・同じ名前にします）
GUIDE_ROLES = [("hr", "人事"), ("doctor", "産業医"), ("nurse", "保健師")]

ST_WAIT = "産業医確認待ち"
ST_APPROVED = "面談対象（承認済）"
ST_MAILED = "勧奨メール送信済"
ST_BOOKED = "面談予約済"
ST_DONE = "面談完了"
ST_OUT = "対象外"
STATUSES = [ST_WAIT, ST_APPROVED, ST_MAILED, ST_BOOKED, ST_DONE, ST_OUT]
# 検索でまとめて選べるステータス（産業医の一覧はこの5つで絞り込みます）
ST_TARGET = "面談対象"
STATUS_GROUPS = {ST_TARGET: [ST_APPROVED, ST_MAILED, ST_BOOKED]}
DOCTOR_STATUSES = [ST_WAIT, ST_TARGET, ST_DONE, ST_OUT]
# 「承認を取消」を出すステータス（承認より先へ進んでいるもの）
APPROVED_STATUSES = [ST_APPROVED, ST_MAILED, ST_BOOKED, ST_DONE]

KINDS = ["定期", "深夜"]
# メール配信管理で扱う種別。「健康管理のお知らせ」は加入者健康一覧から送るもので、
# テンプレートの編集と配信履歴はこの画面（メール配信管理）にまとめています。
MAIL_KINDS = ["面談受診勧奨", "ストレスチェック案内", "健康管理のお知らせ"]
METHODS = ["対面", "オンライン", "電話"]
PURPOSES = ["健診有所見", "長時間労働", "高ストレス", "本人からの申出", "その他"]

OT_LIMIT = 80        # 面談候補として抽出する時間外労働（月）
OT_LIMIT2 = 100      # 医師の面接指導が特に必要な水準

# 一覧の表示列（A-06 表示列カスタマイズ）
# 部署・時間外（月）・ストレスCKの列は出しません（詳細表示のモーダルで確認します）
LIST_COLS = [("emp", "加入者ID"), ("empno", "社員番号"), ("name", "氏名"),
             ("age", "年齢"), ("judge", "健診判定"), ("reason", "抽出理由"),
             ("memo", "メモ"), ("status", "ステータス")]
LIST_COL_KEYS = [k for k, _ in LIST_COLS]

# 旧表記のデータを学会区分へ自動でマッピングする（B-04）
JUDGE_MAP = {
    "a": "A", "b": "B", "c": "C", "d": "D", "e": "E",
    "normal": "A", "基準内": "A", "異常なし": "A", "異常無し": "A", "1": "A",
    "caution": "C", "要注意": "C", "軽度異常": "B", "経過観察": "B", "2": "B",
    "要再検査": "C", "要再検": "C", "生活改善": "C", "要指導": "C", "3": "C",
    "medical": "D", "要医療": "D", "要精密検査": "D", "要精検": "D", "要治療": "D", "4": "D",
    "治療中": "E", "医療中": "E", "5": "E",
}


def norm_judge(v):
    """判定の表記を学会区分（A〜E）にそろえる。判別できなければ None"""
    v = (v or "").strip()
    if not v:
        return None
    if v in JUDGES:
        return v
    key = v.lower()
    if key in JUDGE_MAP:
        return JUDGE_MAP[key]
    for k, mapped in JUDGE_MAP.items():
        if k and k in key:
            return mapped
    return None


def worst_judge(vals):
    """複数の判定のうち、いちばん重いものを返す"""
    best = None
    for v in vals:
        j = norm_judge(v)
        if j and (best is None or JUDGES.index(j) > JUDGES.index(best)):
            best = j
    return best


# ================================================================ 年度
def fy_of(d):
    """日付（YYYY-MM-DD）から年度（4月〜翌3月）を求める"""
    m = re.match(r"(\d{4})-(\d{2})", (d or "").replace("/", "-"))
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return str(y - 1 if mo <= 3 else y)


def current_fy():
    t = date.today()
    return str(t.year - 1 if t.month <= 3 else t.year)


def fy_span(fy):
    """年度の開始年月・終了年月（2026 → 2026-04, 2027-03）"""
    y = int(fy)
    return f"{y}-04", f"{y + 1}-03"


def fy_choices(n=5):
    cur = int(current_fy())
    return [str(cur - i) for i in range(n)]


def age_of(birth, at=None):
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", (birth or "").replace("/", "-"))
    if not m:
        return None
    b = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    t = at or date.today()
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


# ================================================================ 権限
def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not H.current_account():
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def is_hr(acc=None):
    """人事（運用事務）のアカウントか。取込・メール配信・対応区分・出力を行う。"""
    acc = acc or H.current_account()
    return bool(acc) and H.sub_role(acc) == "hr"


def is_doctor(acc=None):
    """医学的判断（承認・就業区分・面談記録・記名）ができるか。
    企業担当者のサブロールが「産業医（doctor）」のアカウントだけが行える。"""
    acc = acc or H.current_account()
    return bool(acc) and H.sub_role(acc) == "doctor"


def is_nurse(acc=None):
    """保健師（sub_role=nurse）か。保健指導の面談を面談結果入力で記録し、記録すると対応済みになる。"""
    acc = acc or H.current_account()
    return bool(acc) and H.sub_role(acc) == "nurse"


# 保健師が扱うステータス（産業医の判定で保健指導の対象になる区分と、保健師対応中）
NURSE_HR_CLASSES = ["就業制限", "要休業", HR_NURSE]


def is_ops(acc=None):
    """運用事務（取込・メール配信・対応区分・報告書の出力）ができるか。
    産業医以外のアカウント（人事・企業担当者・健保担当者・当社スタッフ）が行える。"""
    acc = acc or H.current_account()
    return bool(acc) and not is_doctor(acc)


def need(*keys):
    """機能制御（ロール・サブロールごとの利用可否）で画面・操作を制限する。
    医学的判断（oh.approve／oh.interview／oh.sign）は設定で変更できず、産業医のみ。
    複数のキーを渡したときは、どれか1つでも使えれば通します
    （例：確認・処理の保存は、面談対象者一覧からでも加入者健康一覧からでも行います）。"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            acc = H.current_account()
            if not acc:
                return redirect(url_for("login", next=request.path))
            if not any(H.feature_allowed(k, acc) for k in keys):
                return H.deny_feature(keys[0])
            return fn(*a, **kw)
        return wrapper
    return deco


# 医学的判断の操作（産業医のみ・設定で変更できない）
def doctor_only(fn):
    return need("oh.approve")(fn)


# 運用事務の操作（既定では産業医以外）
def ops_only(fn):
    return need("oh.upload")(fn)


def actor():
    acc = H.current_account()
    return acc["name"] if acc else "—"


def role_label():
    """画面・メモ・報告書に出す肩書き（サブロールがあればそれを使う）"""
    acc = H.current_account()
    if not acc:
        return ""
    s = H.sub_role(acc)
    if s:
        return H.SUB_ROLE_LABELS[s]
    return H.ROLE_LABELS.get(acc["role"], acc["role"])


# ================================================================ 共通の取得処理
def chunks(seq, n=400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def scoped_members(acc, f=None):
    """閲覧できる加入者を、氏名・部署・企業で絞り込んで取得する"""
    db = H.get_db()
    where, params = H.member_where(acc)
    sql = ("SELECT m.id, m.name, m.kana, m.birth, m.sex, m.email, m.employee_code,"
           " m.member_no, m.subscriber_id, m.night_work, m.kenpo_id, m.office_id,"
           " m.cert_mark, m.cert_branch, m.relation,"
           " c.name AS company_name, o.name AS office_name, d.name AS dept_name"
           " FROM member m"
           " LEFT JOIN company c ON c.id=m.company_id"
           " LEFT JOIN office o ON o.id=m.office_id"
           " LEFT JOIN department d ON d.id=m.dept_id"
           " WHERE " + where)
    p = list(params)
    f = f or {}
    if f.get("name"):
        sql += " AND (m.name LIKE ? OR m.kana LIKE ? OR IFNULL(m.employee_code,'') LIKE ?)"
        p += [f"%{f['name']}%"] * 3
    if f.get("mid"):
        # 加入者ID（採番）・被保険者証番号・社員番号のいずれでも探せるようにする
        sql += (" AND (IFNULL(m.subscriber_id,'') LIKE ? OR IFNULL(m.member_no,'') LIKE ?"
                " OR IFNULL(m.employee_code,'') LIKE ?)")
        p += [f"%{f['mid']}%"] * 3
    if f.get("dept"):
        sql += " AND (IFNULL(d.name,'') LIKE ? OR IFNULL(o.name,'') LIKE ?)"
        p += [f"%{f['dept']}%"] * 2
    if f.get("company"):
        sql += " AND IFNULL(c.name,'') LIKE ?"
        p.append(f"%{f['company']}%")
    return db.execute(sql + " ORDER BY c.code, o.code, m.employee_code, m.member_no",
                      p).fetchall()


def fetch_map(sql, ids, key="member_id", params_head=(), params_tail=()):
    """member_id をキーにした辞書を作る（IDが多い場合は分割して問い合わせる）"""
    db, out = H.get_db(), {}
    for part in chunks(ids):
        q = sql.replace("{IN}", ",".join("?" * len(part)))
        for r in db.execute(q, list(params_head) + list(part) + list(params_tail)):
            out[r[key]] = r
    return out


def fetch_rows(sql, ids, params_head=(), params_tail=()):
    db, out = H.get_db(), []
    for part in chunks(ids):
        q = sql.replace("{IN}", ",".join("?" * len(part)))
        out += db.execute(q, list(params_head) + list(part) + list(params_tail)).fetchall()
    return out


def overtime_of(ids, fy):
    """年度内で最も長い月の時間外労働を、加入者ごとに求める"""
    a, b = fy_span(fy)
    rows = fetch_rows("SELECT member_id, ym, hours, holiday_hours FROM oh_overtime"
                      " WHERE member_id IN ({IN}) AND ym BETWEEN ? AND ?", ids,
                      params_tail=(a, b))
    out = {}
    for r in rows:
        cur = out.get(r["member_id"])
        if not cur or (r["hours"] or 0) > (cur["hours"] or 0):
            out[r["member_id"]] = {"ym": r["ym"], "hours": r["hours"] or 0}
    return out


def memo_of(ids):
    """最新のメモを加入者ごとに求める"""
    rows = fetch_rows(
        "SELECT m1.member_id, m1.body, m1.author, m1.role, m1.created_at FROM oh_memo m1"
        " WHERE m1.member_id IN ({IN}) AND m1.id ="
        " (SELECT MAX(m2.id) FROM oh_memo m2 WHERE m2.member_id=m1.member_id)", ids)
    return {r["member_id"]: r for r in rows}


# 抽出理由に載せる異常値の件数（多いときは「ほかn件」とまとめます）
ABNORMAL_MAX = 5


def abnormal_items(items):
    """健診の明細から、判定がC以上（有所見）の項目を値つきで並べる。

    「BMI 28.4」「血圧 148/94 mmHg」のように数値を出して、
    どの検査がどれだけ外れているのかが一覧で分かるようにします。
    （検査値の意味づけ・受診の必要性の判断は産業医・医師が行います）
    """
    out = []
    for i in items or []:
        if norm_judge(i["judge"]) not in FINDING_JUDGES:
            continue
        v = (i["value"] or "").strip()
        if not v:
            continue
        unit = (i["unit"] or "").strip()
        out.append(f"{i['name']} {v}{unit}")
    return out


def reasons_of(judge, ot, stress, items=None):
    """抽出理由を組み立てる（複数該当時は併記）"""
    rs = []
    if judge in FINDING_JUDGES:
        ab = abnormal_items(items)
        if ab:
            text = "／".join(ab[:ABNORMAL_MAX])
            if len(ab) > ABNORMAL_MAX:
                text += f"／ほか{len(ab) - ABNORMAL_MAX}件"
            rs.append(f"健診有所見（{judge}）{text}")
        else:
            rs.append(f"健診有所見（{judge}判定：{JUDGE_LABEL[judge]}）")
    if ot and (ot["hours"] or 0) > OT_LIMIT:
        lv = "100時間超" if (ot["hours"] or 0) > OT_LIMIT2 else "80時間超"
        rs.append(f"長時間労働（{ot['ym']} 時間外{ot['hours']:.0f}時間・{lv}）")
    if stress and stress["high"]:
        s = f"高ストレス（点数{stress['score']}）" if stress["score"] is not None else "高ストレス"
        if stress["applied"]:
            s += "・本人から面接指導の申出あり"
        rs.append(s)
    return rs


def reason_display(rs):
    """一覧に出す抽出理由。「健診有所見（C）」などの前置きは外して値だけにする"""
    out = []
    for t in rs or []:
        t = re.sub(r"^健診有所見（[A-E]）", "", t)
        t = re.sub(r"^健診有所見（[A-E]判定：[^）]*）", "健診有所見", t)
        out.append(t.strip())
    return "／".join(x for x in out if x)


def build_rows(acc, fy, f=None, kind="定期"):
    """対象者一覧の行を作る。面談候補は自動抽出して oh_candidate に反映する。"""
    db = H.get_db()
    members = scoped_members(acc, f)
    ids = [m["id"] for m in members]
    if not ids:
        return []
    ken = fetch_map("SELECT * FROM oh_kenshin WHERE member_id IN ({IN})"
                    " AND fiscal_year=? AND kind=?", ids, params_tail=(fy, kind))
    st = fetch_map("SELECT * FROM oh_stress WHERE member_id IN ({IN}) AND fiscal_year=?",
                   ids, params_tail=(fy,))
    cand = fetch_map("SELECT * FROM oh_candidate WHERE member_id IN ({IN})"
                     " AND fiscal_year=?", ids, params_tail=(fy,))
    iv = fetch_map("SELECT * FROM oh_interview WHERE member_id IN ({IN})"
                   " AND fiscal_year=?", ids, params_tail=(fy,))
    ot = overtime_of(ids, fy)
    memo = memo_of(ids)
    # 健診の明細（抽出理由に異常値を出すため。まとめて取ります）
    kitems = {}
    kids = [k["id"] for k in ken.values()]
    if kids:
        for r in fetch_rows("SELECT * FROM oh_kenshin_item WHERE kenshin_id IN ({IN})"
                            " ORDER BY sort, id", kids):
            kitems.setdefault(r["kenshin_id"], []).append(r)

    rows, changed = [], False
    for m in members:
        k = ken.get(m["id"])
        judge = norm_judge(k["judge"]) if k else None
        s = st.get(m["id"])
        o = ot.get(m["id"])
        rs = reasons_of(judge, o, s, kitems.get(k["id"]) if k else None)
        c = cand.get(m["id"])
        # 面談候補の自動抽出（B-01）。理由が変わったら更新する。
        if rs:
            text = "／".join(rs)
            if not c:
                db.execute("INSERT INTO oh_candidate (member_id, fiscal_year, reasons,"
                           " status, updated_at) VALUES (?,?,?,?,?)",
                           (m["id"], fy, text, ST_WAIT, H.now()))
                c = db.execute("SELECT * FROM oh_candidate WHERE member_id=?"
                               " AND fiscal_year=?", (m["id"], fy)).fetchone()
                changed = True
            elif (c["reasons"] or "") != text:
                db.execute("UPDATE oh_candidate SET reasons=?, updated_at=? WHERE id=?",
                           (text, H.now(), c["id"]))
                c = db.execute("SELECT * FROM oh_candidate WHERE id=?", (c["id"],)).fetchone()
                changed = True
        rows.append({
            "m": m, "id": m["id"], "age": age_of(m["birth"]),
            "judge": judge, "kenshin": k, "exam_date": k["exam_date"] if k else None,
            "stress": s, "overtime": o, "reasons": rs,
            "reason_text": reason_display(rs), "cand": c, "iv": iv.get(m["id"]),
            "memo": memo.get(m["id"]),
            "status": (c["status"] if c else "—"),
            "work_class": (c["work_class"] if c else None),
            "hr_class": (c["hr_class"] if c else "未判定"),
            "due_on": (c["due_on"] if c else None),
            "done_on": (c["done_on"] if c else None),
            "follow_on": (c["follow_on"] if c else None),
        })
    if changed:
        db.commit()
    return rows


def apply_row_filter(rows, f):
    """健診判定・抽出条件・ステータス・受診状況で絞り込む（画面の条件）"""
    js = [j for j in (f.get("judges") or []) if j in JUDGES]
    if js:
        rows = [r for r in rows if r["judge"] in js]
    cond = f.get("cond")
    if cond == "kenshin":
        rows = [r for r in rows if r["judge"] in FINDING_JUDGES]
    elif cond == "overtime":
        rows = [r for r in rows if r["overtime"] and r["overtime"]["hours"] > OT_LIMIT]
    elif cond == "stress":
        rows = [r for r in rows if r["stress"] and r["stress"]["high"]]
    elif cond == "any":
        rows = [r for r in rows if r["reasons"]]
    elif cond == "none":
        rows = [r for r in rows if not r["reasons"]]
    if f.get("status"):
        # 「面談対象」は承認済・勧奨メール送信済・面談予約済をまとめて指す（産業医の検索）
        want = STATUS_GROUPS.get(f["status"], [f["status"]])
        rows = [r for r in rows if r["status"] in want]
    if f.get("hr_class"):
        rows = [r for r in rows if r["hr_class"] == f["hr_class"]]
    if f.get("done") == "done":
        rows = [r for r in rows if r["exam_date"]]
    elif f.get("done") == "not":
        rows = [r for r in rows if not r["exam_date"]]
    # 面談結果入力の「入力状況」（入力済＝面談日が入っている）
    if f.get("iv") == "done":
        rows = [r for r in rows if r["iv"] and r["iv"]["met_on"]]
    elif f.get("iv") == "not":
        rows = [r for r in rows if not (r["iv"] and r["iv"]["met_on"])]
    return rows


def prev_fy(fy):
    """前年度の年度（文字列）。比較用のグラフで使う。"""
    try:
        return str(int(fy) - 1)
    except (TypeError, ValueError):
        return fy


def month_series(rows, kind):
    """月別の件数を 4月〜翌3月 の12個の配列にする（ダッシュボードのグラフ用）

    kind="iv"  … 面談を実施した月（面談記録の実施日）
    kind="ken" … 健診を受診した月（健診記録の受診日）
    """
    out = [0] * 12
    for r in rows:
        d = None
        if kind == "iv":
            iv = r.get("iv")
            d = iv["met_on"] if iv and iv["met_on"] else None
        else:
            d = r.get("exam_date")
        if not d or len(str(d)) < 7:
            continue
        try:
            m = int(str(d)[5:7])
        except ValueError:
            continue
        if 1 <= m <= 12:
            out[(m - 4) % 12] += 1
    return out


def flow_counts(rows):
    """業務フローナビの件数（A-04）"""
    c = {"cand": 0, "wait": 0, "approved": 0, "mailed": 0, "done": 0, "out": 0}
    for r in rows:
        if r["reasons"]:
            c["cand"] += 1
        s = r["status"]
        if s == ST_WAIT:
            c["wait"] += 1
        elif s == ST_APPROVED:
            c["approved"] += 1
        elif s in (ST_MAILED, ST_BOOKED):
            c["mailed"] += 1
        elif s == ST_DONE:
            c["done"] += 1
        elif s == ST_OUT:
            c["out"] += 1
    return c


def view_key(acc):
    """表示列の保存キー。ロール＋サブロール単位で保存する
    （産業医と人事で見たい列が違うため）"""
    s = H.sub_role(acc)
    return acc["role"] + ("/" + s if s else "")


def visible_cols(acc):
    """ロール（サブロール）別の表示列（A-06）"""
    row = H.get_db().execute("SELECT cols FROM oh_view_col WHERE role=? AND screen=?",
                             (view_key(acc), "list")).fetchone()
    if not row:
        return list(LIST_COL_KEYS)
    keep = [c for c in row["cols"].split(",") if c in LIST_COL_KEYS]
    return keep or list(LIST_COL_KEYS)


def ensure_candidate(mid, fy):
    db = H.get_db()
    row = db.execute("SELECT * FROM oh_candidate WHERE member_id=? AND fiscal_year=?",
                     (mid, fy)).fetchone()
    if row:
        return row
    db.execute("INSERT INTO oh_candidate (member_id, fiscal_year, status, updated_at)"
               " VALUES (?,?,?,?)", (mid, fy, ST_WAIT, H.now()))
    return db.execute("SELECT * FROM oh_candidate WHERE member_id=? AND fiscal_year=?",
                      (mid, fy)).fetchone()


def owns_member(acc, mid):
    """自分の閲覧範囲の加入者かどうか（URLを直接呼ばれた場合の防御）"""
    where, params = H.member_where(acc)
    return bool(H.get_db().execute(
        f"SELECT 1 FROM member m WHERE m.id=? AND {where}", [mid] + list(params)).fetchone())


def member_row(acc, mid):
    where, params = H.member_where(acc)
    return H.get_db().execute(
        "SELECT m.*, c.name AS company_name, o.name AS office_name, d.name AS dept_name"
        " FROM member m LEFT JOIN company c ON c.id=m.company_id"
        " LEFT JOIN office o ON o.id=m.office_id"
        " LEFT JOIN department d ON d.id=m.dept_id"
        f" WHERE m.id=? AND {where}", [mid] + list(params)).fetchone()


def form_filter():
    f = {k: (request.args.get(k) or "").strip()
         for k in ("name", "mid", "dept", "company", "cond", "status", "hr_class",
                   "done", "iv")}
    f["judges"] = request.args.getlist("judge")
    f["fy"] = (request.args.get("fy") or current_fy()).strip()
    return f


def common(fy):
    acc = H.current_account()
    return {"acc": acc, "fy": fy, "fys": fy_choices(), "JUDGES": JUDGES,
            "today": date.today().isoformat(),
            "JUDGE_LABEL": JUDGE_LABEL, "WORK_CLASSES": WORK_CLASSES,
            "HR_CLASSES": HR_CLASSES, "HR_DONE": HR_DONE, "STATUSES": STATUSES, "METHODS": METHODS,
            "PURPOSES": PURPOSES, "OT_LIMIT": OT_LIMIT, "OT_LIMIT2": OT_LIMIT2,
            "DOCTOR_STATUSES": DOCTOR_STATUSES,
            "APPROVED_STATUSES": APPROVED_STATUSES,
            # 対応区分ごとの進め方（人事・産業医・保健師が行うこと）
            "HR_CLASS_GUIDE": HR_CLASS_GUIDE, "GUIDE_ROLES": GUIDE_ROLES,
            "DOCTOR_HR_CLASSES": DOCTOR_HR_CLASSES,
            # 画面に出す操作は機能制御に合わせる
            "CAN_MED": H.feature_allowed("oh.approve", acc),
            "CAN_IV": H.feature_allowed("oh.interview", acc),
            "CAN_IV_VIEW": H.feature_allowed("oh.interview.view", acc),
            "CAN_SIGN": H.feature_allowed("oh.sign", acc),
            "CAN_HR": H.feature_allowed("oh.hr_class", acc),
            "CAN_UPLOAD": H.feature_allowed("oh.upload", acc),
            "CAN_MAIL": H.feature_allowed("oh.mail", acc),
            "CAN_KENSHIN": H.feature_allowed("oh.kenshin", acc),
            "CAN_OPS": is_ops(acc),
            "IS_DOCTOR": is_doctor(acc), "IS_NURSE": is_nurse(acc),
            # 保健師の「一覧に戻る」は加入者健康一覧
            "LIST_URL": (url_for("hm.hm_list", fy=fy) if is_nurse(acc) else url_for("oh.oh_list", fy=fy)),
            "ROLE_NAME": role_label(), "LIST_COLS": LIST_COLS}


# ================================================================ B．対象者スクリーニング
@bp.route("/")
@need("oh.list")
def oh_list():
    """面談対象者一覧（B-01〜B-08）"""
    acc = H.current_account()
    f = form_filter()
    rows = build_rows(acc, f["fy"], f)
    flow_c = flow_counts(rows)
    rows = apply_row_filter(rows, f)
    total = len(rows)
    done = sum(1 for r in rows if r["exam_date"])
    cols = visible_cols(acc)
    # 社員番号は加入者IDのつぎに出す（名簿と突き合わせるため、どのロールでも出します）
    if "empno" not in cols:
        at = cols.index("emp") + 1 if "emp" in cols else 0
        cols.insert(at, "empno")
    return render_template("sanmen_list.html", rows=rows, f=f, flow_c=flow_c,
                           cols=cols, total=total, done=done,
                           notdone=total - done, **common(f["fy"]))


@bp.route("/dashboard")
@need("oh.dashboard")
def oh_dashboard():
    """面談ダッシュボード（全体の件数と進み具合をまとめて見る画面）

    対象者一覧から、業務フローの進み具合・サマリ・内訳をこの画面に分けました。
    件数を押すと、その条件でしぼり込んだ対象者一覧が開きます。
    """
    acc = H.current_account()
    f = form_filter()
    rows = build_rows(acc, f["fy"], f)
    flow_c = flow_counts(rows)
    total = len(rows)
    done = sum(1 for r in rows if r["exam_date"])
    # 抽出理由の内訳（1人で複数の理由に該当することがあります）
    reason_n = {"kenshin": 0, "overtime": 0, "stress": 0, "applied": 0, "multi": 0}
    for r in rows:
        rs = r["reasons"] or []
        for text in rs:
            if text.startswith("健診有所見"):
                reason_n["kenshin"] += 1
            elif text.startswith("長時間労働"):
                reason_n["overtime"] += 1
            elif text.startswith("高ストレス"):
                reason_n["stress"] += 1
                if "申出あり" in text:
                    reason_n["applied"] += 1
        if len(rs) > 1:
            reason_n["multi"] += 1
    # ワークフローのステータス内訳
    status_n = {}
    for r in rows:
        status_n[r["status"]] = status_n.get(r["status"], 0) + 1
    # 健診の判定区分の内訳（参考情報。受診の必要性は産業医・医師が判断します）
    judge_n = {}
    for r in rows:
        if r["judge"]:
            judge_n[r["judge"]] = judge_n.get(r["judge"], 0) + 1
    # 月次推移（4月〜翌3月）。前年度と並べて比較できるようにする。
    monthly = {"iv": {"cur": month_series(rows, "iv"),
                      "prev": month_series(build_rows(acc, prev_fy(f["fy"]), f), "iv")},
               "ken": {"cur": month_series(rows, "ken"),
                       "prev": month_series(build_rows(acc, prev_fy(f["fy"]), f), "ken")}}
    return render_template("sanmen_dashboard.html", f=f, flow_c=flow_c, total=total,
                           done=done, notdone=total - done, reason_n=reason_n,
                           status_n=status_n, judge_n=judge_n, monthly=monthly,
                           **common(f["fy"]))


@bp.route("/cols", methods=["POST"])
@need("oh.list")
def oh_cols():
    """表示列の保存・初期化（A-06）"""
    acc, db = H.current_account(), H.get_db()
    if request.form.get("reset"):
        db.execute("DELETE FROM oh_view_col WHERE role=? AND screen='list'",
                   (view_key(acc),))
        db.commit()
        flash("表示列を初期状態に戻しました。", "ok")
    else:
        keep = [c for c in request.form.getlist("cols") if c in LIST_COL_KEYS]
        # 氏名は必ず表示する
        if "name" not in keep:
            keep.insert(0, "name")
        db.execute("INSERT INTO oh_view_col (role, screen, cols) VALUES (?,?,?)"
                   " ON CONFLICT(role, screen) DO UPDATE SET cols=excluded.cols",
                   (view_key(acc), "list", ",".join(keep)))
        db.commit()
        flash("表示列を保存しました。", "ok")
    return redirect(request.form.get("back") or url_for("oh.oh_list"))


@bp.route("/save", methods=["POST"])
# 面談対象者一覧（産業医）と加入者健康一覧（人事・健保）の両方から使います
@need("oh.list", "health.view")
def oh_save():
    """確認・処理モーダルの保存（B-08〜B-14）。一括処理にも同じ入口を使う。

    医学的判断（承認・就業区分・面談不要）は産業医のみ。人事から送信された場合は
    サーバ側で無視する（A-03）。対応区分は人事のみが設定できる（B-11）。
    """
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    ids = [int(x) for x in request.form.getlist("member_ids") if str(x).isdigit()]
    if request.form.get("member_id", type=int):
        ids.append(request.form.get("member_id", type=int))
    ids = [i for i in dict.fromkeys(ids) if owns_member(acc, i)]
    if not ids:
        flash("対象者が選択されていません。", "error")
        return redirect(request.form.get("back") or url_for("oh.oh_list"))

    action = request.form.get("action") or "save"
    work_class = (request.form.get("work_class") or "").strip()
    hr_class = (request.form.get("hr_class") or "").strip()
    memo = (request.form.get("memo") or "").strip()
    ex_reason = (request.form.get("exclude_reason") or "").strip()
    due_on = (request.form.get("due_on") or "").strip()
    follow_on = (request.form.get("follow_on") or "").strip()
    done_on = (request.form.get("done_on") or "").strip()
    med = is_doctor(acc)

    # 人事が医学的入力値を送ってきた場合は記録して無視する
    if not med and (work_class or action in ("approve", "unapprove", "exclude")):
        H.log("master", "医学的判断の入力を無視", "blocked", target="産業医面談",
              detail=f"role={acc['role']}／sub_role={H.sub_role(acc) or 'なし'}"
                     f"／就業区分・承認は産業医のみ")
        work_class = ""
        if action in ("approve", "unapprove", "exclude"):
            action = "save"
    if hr_class and not H.feature_allowed("oh.hr_class", acc):
        # 対応区分は人事（運用事務）の項目。使えないロールから送信された場合は無視する。
        H.log("master", "対応区分の入力を無視", "blocked", target="産業医面談",
              detail=f"role={H.role_key(acc)}／機能=oh.hr_class")
        hr_class = ""

    n_app = n_unapp = n_work = n_hr = n_memo = n_out = n_date = 0
    for mid in ids:
        c = ensure_candidate(mid, fy)
        sets, params = [], []
        if action == "approve":
            sets += ["status=?", "approved_by=?", "approved_at=?"]
            params += [ST_APPROVED, actor(), H.now()]
            n_app += 1
        elif action == "unapprove":
            sets += ["status=?", "approved_by=NULL", "approved_at=NULL"]
            params += [ST_WAIT]
            n_unapp += 1
        elif action == "exclude":
            sets += ["status=?", "exclude_reason=?"]
            params += [ST_OUT, ex_reason or "産業医の判断により面談不要"]
            n_out += 1
        if work_class in WORK_CLASSES:
            sets.append("work_class=?")
            params.append(work_class)
            n_work += 1
            # 面談の要否は就業区分で決まる（産業医の判定）
            #   通常勤務 → 面談不要（対象外）。就業制限・要休業 → 面談対象（承認済）
            # 案内済み・予約済み・面談完了まで進んでいる方の進み具合は変えない
            if action == "save":
                if work_class == "通常勤務" and c["status"] in (ST_WAIT, ST_APPROVED):
                    sets += ["status=?", "exclude_reason=?"]
                    params += [ST_OUT, "就業区分「通常勤務」の判定により面談不要"]
                    n_out += 1
                elif work_class != "通常勤務" and c["status"] in (ST_WAIT, ST_OUT):
                    sets += ["status=?", "exclude_reason=NULL", "approved_by=?", "approved_at=?"]
                    params += [ST_APPROVED, actor(), H.now()]
                    n_app += 1
        if hr_class in HR_CLASSES:
            sets.append("hr_class=?")
            params.append(hr_class)
            n_hr += 1
        # 対応期限・次回フォロー予定日・対応完了日（人事・産業医のどちらも記録できます）
        hit = False
        for col, val in (("due_on", due_on), ("follow_on", follow_on),
                         ("done_on", done_on)):
            if val:
                sets.append(f"{col}=?")
                params.append(val)
                hit = True
        if hit:
            n_date += 1
        # 産業医の入力を人事のステータスに反映します。
        #   就業区分を判定 → その区分（通常勤務・就業制限・要休業）
        #   面談対象として承認 → 保健師対応中（保健師のフォローへ進みます）
        new_hr = ""
        if med and work_class in WORK_CLASSES:
            new_hr = work_class
        if med and action == "approve":
            new_hr = HR_NURSE
        if new_hr:
            sets.append("hr_class=?")
            params.append(new_hr)
            n_hr += 1
        if sets:
            sets.append("updated_at=?")
            params.append(H.now())
            db.execute(f"UPDATE oh_candidate SET {','.join(sets)} WHERE id=?",
                       params + [c["id"]])
        if memo:
            db.execute("INSERT INTO oh_memo (member_id, fiscal_year, body, author, role)"
                       " VALUES (?,?,?,?,?)", (mid, fy, memo, actor(), role_label()))
            n_memo += 1
    db.commit()

    detail = "／".join(x for x in [
        f"承認{n_app}件" if n_app else "",
        f"承認取消{n_unapp}件" if n_unapp else "",
        f"就業区分{n_work}件" if n_work else "",
        f"対応区分{n_hr}件" if n_hr else "", f"日付{n_date}件" if n_date else "",
        f"メモ{n_memo}件" if n_memo else "",
        f"対象外{n_out}件" if n_out else ""] if x) or "変更なし"
    H.log("master", "産業医面談の確認・処理", "success",
          target=f"{len(ids)}名（{fy}年度）", detail=detail)
    flash(f"{len(ids)}名に反映しました（{detail}）。", "ok")
    return redirect(request.form.get("back") or url_for("oh.oh_list"))


@bp.route("/member/<int:mid>")
@need("oh.list")
def oh_member(mid):
    """対象者の詳細（確認・処理モーダルの中身とメモ履歴・健診結果票）"""
    acc = H.current_account()
    m = member_row(acc, mid)
    if not m:
        return render_template("denied.html", path=request.path, notfound=True), 404
    fy = (request.args.get("fy") or current_fy()).strip()
    db = H.get_db()
    ken = db.execute("SELECT * FROM oh_kenshin WHERE member_id=? AND fiscal_year=?"
                     " AND kind='定期'", (mid, fy)).fetchone()
    prev = db.execute("SELECT * FROM oh_kenshin WHERE member_id=? AND fiscal_year=?"
                      " AND kind='定期'", (mid, str(int(fy) - 1))).fetchone()
    items = db.execute("SELECT * FROM oh_kenshin_item WHERE kenshin_id=?"
                       " ORDER BY sort, id", (ken["id"],)).fetchall() if ken else []
    prev_items = {r["name"]: r for r in db.execute(
        "SELECT * FROM oh_kenshin_item WHERE kenshin_id=?", (prev["id"],))} if prev else {}
    st = db.execute("SELECT * FROM oh_stress WHERE member_id=? AND fiscal_year=?",
                    (mid, fy)).fetchone()
    a, b = fy_span(fy)
    ots = db.execute("SELECT * FROM oh_overtime WHERE member_id=? AND ym BETWEEN ? AND ?"
                     " ORDER BY ym", (mid, a, b)).fetchall()
    memos = db.execute("SELECT * FROM oh_memo WHERE member_id=? ORDER BY id DESC",
                       (mid,)).fetchall()
    cand = db.execute("SELECT * FROM oh_candidate WHERE member_id=? AND fiscal_year=?",
                      (mid, fy)).fetchone()
    iv = db.execute("SELECT * FROM oh_interview WHERE member_id=? AND fiscal_year=?",
                    (mid, fy)).fetchone()
    otmax = max([dict(ym=r["ym"], hours=r["hours"] or 0) for r in ots],
                key=lambda x: x["hours"], default=None)
    judge = norm_judge(ken["judge"]) if ken else None
    return render_template("sanmen_member.html", m=m, ken=ken, items=items,
                           prev=prev, prev_items=prev_items, st=st, ots=ots,
                           memos=memos, cand=cand, iv=iv, judge=judge,
                           reasons=reasons_of(judge, otmax, st, items),
                           age=age_of(m["birth"]),
                           **common(fy))


# ---------------------------------------------------------------- B-16 CSV出力
KENSHIN_CSV_HEADER = ["社員ID", "氏名", "企業", "事業所", "部署", "年齢", "性別",
                      "受診年度", "健診区分", "受診日", "総合判定", "判定の意味",
                      "抽出理由", "時間外（最大）", "高ストレス", "就業区分",
                      "対応区分", "ステータス", "産業医所見", "最新メモ"]


def csv_rows_for(acc, fy, ids=None, kind="定期"):
    rows = build_rows(acc, fy, None, kind)
    if ids:
        keep = set(ids)
        rows = [r for r in rows if r["id"] in keep]
    out = []
    for r in rows:
        m = r["m"]
        out.append((
            m["employee_code"] or "", m["name"], m["company_name"] or "",
            m["office_name"] or "", m["dept_name"] or "", r["age"] if r["age"] is not None else "",
            m["sex"] or "", fy, kind, r["exam_date"] or "",
            r["judge"] or "", JUDGE_LABEL.get(r["judge"] or "", ""),
            r["reason_text"], f"{r['overtime']['hours']:.0f}" if r["overtime"] else "",
            "該当" if (r["stress"] and r["stress"]["high"]) else "",
            r["work_class"] or "", r["hr_class"] or "", r["status"],
            (r["kenshin"]["findings"] if r["kenshin"] else "") or "",
            (r["memo"]["body"] if r["memo"] else "") or ""))
    return out


@bp.route("/export/csv", methods=["GET", "POST"])
@need("oh.list")
def oh_export_csv():
    """健診結果・面談状況の一括CSV出力（B-16）"""
    acc = H.current_account()
    src = request.form if request.method == "POST" else request.args
    fy = (src.get("fy") or current_fy()).strip()
    kind = src.get("kind") if src.get("kind") in KINDS else "定期"
    ids = [int(x) for x in src.getlist("member_ids") if str(x).isdigit()]
    ids = [i for i in ids if owns_member(acc, i)]
    rows = csv_rows_for(acc, fy, ids, kind)
    return H.export_csv(f"sanmen_{kind}_{fy}.csv", KENSHIN_CSV_HEADER, rows,
                        f"産業医面談 健診結果一覧（{fy}年度・{kind}）") \
        or redirect(url_for("oh.oh_list", fy=fy))


# ---------------------------------------------------------------- B-17 PDF（印刷）出力
@bp.route("/export/pdf", methods=["GET", "POST"])
@need("oh.list")
def oh_export_pdf():
    """健診結果票の一括PDF出力（B-17）。印刷用の画面を開き、PDFとして保存する。"""
    acc = H.current_account()
    src = request.form if request.method == "POST" else request.args
    fy = (src.get("fy") or current_fy()).strip()
    ids = [int(x) for x in src.getlist("member_ids") if str(x).isdigit()]
    ids = [i for i in ids if owns_member(acc, i)]
    rows = build_rows(acc, fy, None, "定期")
    if ids:
        keep = set(ids)
        rows = [r for r in rows if r["id"] in keep]
    rows = rows[:100]
    db = H.get_db()
    for r in rows:
        r["ken_items"] = db.execute(
            "SELECT * FROM oh_kenshin_item WHERE kenshin_id=? ORDER BY sort, id",
            (r["kenshin"]["id"],)).fetchall() if r["kenshin"] else []
        prev = db.execute("SELECT id FROM oh_kenshin WHERE member_id=? AND fiscal_year=?"
                          " AND kind='定期'", (r["id"], str(int(fy) - 1))).fetchone()
        r["prev_items"] = {x["name"]: x for x in db.execute(
            "SELECT * FROM oh_kenshin_item WHERE kenshin_id=?", (prev["id"],))} if prev else {}
    H.log("download", "健診結果票の印刷・PDF出力", "success",
          target=f"{len(rows)}名（{fy}年度）")
    return render_template("sanmen_kenshin_print.html", rows=rows, **common(fy))


# ================================================================ C．健診受診管理
@bp.route("/kenshin")
@need("oh.kenshin")
def oh_kenshin():
    """定期健診・深夜健診の受診一覧（C-01〜C-06）"""
    acc = H.current_account()
    f = form_filter()
    kind = request.args.get("kind") if request.args.get("kind") in KINDS else "定期"
    rows = build_rows(acc, f["fy"], f, kind)
    if kind == "深夜":
        # 深夜業従事区分が立っている加入者だけが対象（C-02・C-03）
        rows = [r for r in rows if r["m"]["night_work"]]
    rows = apply_row_filter(rows, f)
    total = len(rows)
    done = sum(1 for r in rows if r["exam_date"])
    finding = sum(1 for r in rows if r["judge"] in FINDING_JUDGES)
    return render_template("sanmen_kenshin.html", rows=rows, f=f, kind=kind,
                           total=total, done=done, notdone=total - done,
                           finding=finding,
                           rate=(done / total * 100 if total else 0),
                           **common(f["fy"]))


@bp.route("/night", methods=["POST"])
@need("oh.kenshin")
def oh_night():
    """深夜業従事区分の設定（C-03）"""
    acc, db = H.current_account(), H.get_db()
    mid = request.form.get("member_id", type=int)
    if not mid or not owns_member(acc, mid):
        flash("対象者を特定できませんでした。", "error")
        return redirect(url_for("oh.oh_kenshin", kind="深夜"))
    v = 1 if request.form.get("night_work") else 0
    db.execute("UPDATE member SET night_work=?, updated_at=? WHERE id=?", (v, H.now(), mid))
    db.commit()
    H.log("master", "深夜業従事区分を変更", "success", target=f"member:{mid}",
          detail="対象" if v else "対象外")
    flash("深夜業従事区分を変更しました。", "ok")
    return redirect(request.form.get("back") or url_for("oh.oh_kenshin", kind="深夜"))


# ================================================================ E．面談結果入力
def _iv_not_done(r):
    """面談結果がまだ入力されていない方か"""
    return not (r["iv"] and r["iv"]["met_on"])


def interview_rows(acc, fy):
    """面談結果入力の対象者。面接指導の対象者（抽出理由あり）・承認済以降・
    すでに面談記録があるものが対象。該当がないときは、記録を入れられるように
    担当範囲の対象者をそのまま使う。
    保健師は、産業医が 就業制限・要休業 と判定した方（保健指導の対象）・保健師対応中の方と、記録済みの方。"""
    rows = build_rows(acc, fy)
    if is_nurse(acc):
        picked = [r for r in rows if r["hr_class"] in NURSE_HR_CLASSES or r["iv"]]
        return rows, picked
    picked = [r for r in rows
              if r["reasons"] or r["iv"]
              or r["status"] in (ST_APPROVED, ST_MAILED, ST_BOOKED, ST_DONE)]
    return rows, (picked or rows)


def next_waiting(rows, cur_id):
    """「次の未入力の方」＝いまの方より後ろで未入力の方（無ければ先頭から探す）"""
    i = next((k for k, r in enumerate(rows) if r["id"] == cur_id), -1)
    order = rows[i + 1:] + rows[:i]
    return next((r for r in order if _iv_not_done(r)), None)


@bp.route("/interview")
@need("oh.interview.view")
def oh_interview():
    """面談結果入力（E-01・E-02）。1人ずつ入力し、「確定して次の未入力の方へ」で進めます。"""
    acc = H.current_account()
    fy = (request.args.get("fy") or current_fy()).strip()
    all_rows, rows = interview_rows(acc, fy)
    flow_c = flow_counts(all_rows)
    not_done = _iv_not_done

    # 入力する対象者（未指定のときは、まだ入力していない先頭の方）
    sid = request.args.get("sid", type=int)
    sel = next((r for r in rows if r["id"] == sid), None)
    if sel is None:
        sel = next((r for r in rows if not_done(r)), None) or (rows[0] if rows else None)
    nxt = next_waiting(rows, sel["id"]) if sel else None
    # 「前の方に戻る」＝一覧の並びでひとつ前の方（先頭なら無し → 一覧に戻る）
    prv = None
    if sel:
        i = next((k for k, r in enumerate(rows) if r["id"] == sel["id"]), -1)
        prv = rows[i - 1] if i > 0 else None
    waiting = [r for r in rows if not_done(r)]
    return render_template("sanmen_interview.html", rows=rows, flow_c=flow_c,
                           sel=sel, nxt=nxt, prv=prv, n_wait=len(waiting),
                           done=len(rows) - len(waiting), **common(fy))


@bp.route("/interview/save", methods=["POST"])
@need("oh.interview")
def oh_interview_save():
    """面談記録の保存（E-01）。保存すると面談実施済として集計・報告へ連携する（E-02）。"""
    acc, db = H.current_account(), H.get_db()
    mid = request.form.get("member_id", type=int)
    fy = (request.form.get("fy") or current_fy()).strip()
    if not mid or not owns_member(acc, mid):
        flash("対象者を特定できませんでした。", "error")
        return redirect(url_for("oh.oh_interview", fy=fy))
    v = {k: (request.form.get(k) or "").strip()
         for k in ("met_on", "method", "purpose", "work_class", "measure",
                   "findings", "next_plan")}
    met_on = H.norm_date(v["met_on"]) if v["met_on"] else ""
    if met_on is None:
        flash("面談日の形式が正しくありません（例 2026-08-20）。", "error")
        return redirect(url_for("oh.oh_interview", fy=fy))
    # 次回フォロー予定日も日付として保存します（入力は日付の欄です）
    if v["next_plan"]:
        nxt = H.norm_date(v["next_plan"])
        if nxt is None:
            flash("次回フォロー予定日の形式が正しくありません（例 2026-08-20）。", "error")
            return redirect(url_for("oh.oh_interview", fy=fy, sid=mid))
        v["next_plan"] = nxt
    ot = request.form.get("overtime", type=float)
    # 就業区分は産業医の判定。保健師の記録では変えない
    work = v["work_class"] if (v["work_class"] in WORK_CLASSES and is_doctor(acc)) else None
    db.execute(
        "INSERT INTO oh_interview (member_id, fiscal_year, met_on, method, purpose,"
        " work_class, measure, findings, next_plan, overtime, doctor, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(member_id, fiscal_year) DO UPDATE SET"
        " met_on=excluded.met_on, method=excluded.method, purpose=excluded.purpose,"
        " work_class=excluded.work_class, measure=excluded.measure,"
        " findings=excluded.findings, next_plan=excluded.next_plan,"
        " overtime=excluded.overtime, doctor=excluded.doctor, updated_at=excluded.updated_at",
        (mid, fy, met_on, v["method"], v["purpose"], work, v["measure"], v["findings"],
         v["next_plan"], ot, actor(), H.now()))
    c = ensure_candidate(mid, fy)
    # 保存＝確定。面談実施済（面談完了）にして集計・報告へ反映します（下書きは置きません）
    sets = ["status=?", "updated_at=?"]
    params = [ST_DONE, H.now()]
    if work:
        sets.append("work_class=?")
        params.append(work)
    if c["hr_class"] == "未判定":
        sets.append("hr_class=?")
        params.append("産業医判定済")
    # 保健師が面談（保健指導）の結果を記録したら、ステータスを対応済みにする
    nurse = is_nurse(acc)
    if nurse:
        sets.append("hr_class=?")
        params.append(HR_DONE)
    db.execute(f"UPDATE oh_candidate SET {','.join(sets)} WHERE id=?", params + [c["id"]])
    db.commit()
    H.log("master", "面談記録を登録", "success", target=f"member:{mid}（{fy}年度）",
          detail=f"面談日{met_on}／{v['method']}／就業区分{work or '—'}"
                 + (f"／ステータス→{HR_DONE}" if nurse else ""))
    flash(f"面談記録を確定しました。ステータスを「{HR_DONE}」（対応済み）にしました。" if nurse
          else "面談記録を確定しました。面談実施済として報告に反映します。", "ok")
    # 「確定して次の未入力の方へ」：確定したうえで、次にまだ入力していない方の画面へ進む
    if request.form.get("go_next"):
        _, rows = interview_rows(acc, fy)
        nxt = next_waiting(rows, mid)
        if nxt:
            return redirect(url_for("oh.oh_interview", fy=fy, sid=nxt["id"]))
        # 次の未入力の方がいなければ、確定して一覧に戻る（保健師は加入者健康一覧）
        flash("この年度の面談対象は、入力がすべて終わりました。", "ok")
        return redirect(url_for("hm.hm_list", fy=fy) if nurse else url_for("oh.oh_list", fy=fy))
    return redirect(url_for("oh.oh_interview", fy=fy, sid=mid))


# ================================================================ F．労基署報告
def report_summary(acc, fy, oid=None):
    """報告書に載せる集計。定期健康診断結果報告書（様式第6号）の項目に合わせる。
    事業所（oid）を指定すると、その事業所の加入者だけで集計します。"""
    rows = build_rows(acc, fy, None, "定期")
    if oid:
        rows = [r for r in rows if str(r["m"]["office_id"] or "") == str(oid)]
    night = [r for r in rows if r["m"]["night_work"]]
    nrows = build_rows(acc, fy, None, "深夜") if night else []
    if oid:
        nrows = [r for r in nrows if str(r["m"]["office_id"] or "") == str(oid)]
    ndone = sum(1 for r in nrows if r["m"]["night_work"] and r["exam_date"])
    judges = {j: 0 for j in JUDGES}
    for r in rows:
        if r["judge"]:
            judges[r["judge"]] += 1
    done = sum(1 for r in rows if r["exam_date"])
    finding = sum(1 for r in rows if r["judge"] in FINDING_JUDGES)
    iv_target = sum(1 for r in rows if r["reasons"])
    iv_done = sum(1 for r in rows if r["iv"] and r["iv"]["met_on"])
    # 就業上の措置を記録した件数（労基署報告の「就業上の措置 実施数」）
    iv_measure = sum(1 for r in rows
                     if r["iv"] and (r["iv"]["measure"] or "").strip())
    work = {w: sum(1 for r in rows if r["work_class"] == w) for w in WORK_CLASSES}
    st = [r for r in rows if r["stress"]]
    st_high = sum(1 for r in st if r["stress"]["high"])
    st_apply = sum(1 for r in st if r["stress"]["applied"])
    st_iv = sum(1 for r in rows if r["stress"] and r["stress"]["high"]
                and r["iv"] and r["iv"]["met_on"])
    st_apply_iv = sum(1 for r in rows if r["stress"] and r["stress"]["applied"]
                      and r["iv"] and r["iv"]["met_on"])
    camp = H.get_db().execute("SELECT * FROM oh_sc_campaign WHERE fiscal_year=?"
                              " ORDER BY id DESC LIMIT 1", (fy,)).fetchone()
    return {
        "total": len(rows), "done": done, "notdone": len(rows) - done,
        "rate": (done / len(rows) * 100 if rows else 0),
        "finding": finding, "judges": judges,
        "night_total": len(night), "night_done": ndone,
        "iv_target": iv_target, "iv_done": iv_done, "iv_measure": iv_measure,
        "iv_rate": (iv_done / iv_target * 100 if iv_target else 0),
        "iv_rest": iv_target - iv_done, "work": work,
        "st_total": len(rows), "st_done": len(st), "st_high": st_high,
        "st_apply": st_apply, "st_iv": st_iv, "st_apply_iv": st_apply_iv,
        "st_wait": st_high - st_apply, "camp": camp,
        "kenpo": (H.get_db().execute("SELECT * FROM kenpo WHERE id=?",
                                     (acc["kenpo_id"],)).fetchone()
                  if acc["kenpo_id"] else None),
    }


# ---------------------------------------------------------------- 様式第6号の報告項目
# eGov「定期健康診断結果報告書 仕様書」の項目に合わせています。
# 人数は健診結果から集計し、集計できない情報（事業場・実施機関・産業医など）は登録します。
FORM6_TEXT_FIELDS = [
    ("labor_insurance_no", "労働保険番号", "例：13101-123456-000"),
    ("industry_type", "事業の種類", "例：情報通信業"),
    ("workplace_name", "事業場の名称", "例：株式会社　光通信　本社"),
    ("workplace_zip", "事業場の郵便番号", "例：1710014"),
    ("workplace_address", "事業場の所在地", "例：東京都豊島区池袋2-43-1"),
    ("workplace_tel", "事業場の電話番号", "例：03-6416-3872"),
    ("examination_date", "健診年月日", "例：2026-06-12"),
    ("institution_name", "健康診断実施機関名", "例：ひかり健診クリニック"),
    ("institution_address", "健康診断実施機関所在地", "例：東京都新宿区西新宿1-1-1"),
    ("physician_name", "産業医氏名", "例：山田 太郎"),
    ("physician_address", "産業医所在地", "例：東京都港区芝浦4-16-25"),
    ("employer_name_title", "事業者職氏名", "例：代表取締役　佐藤 一郎"),
    ("report_count", "報告回目", "例：1"),
]
# 様式第6号（用紙）の「健康診断項目」欄の並び。左列7行・右列6行で、
# それぞれ受診労働者数と有所見者数を書きます（項目名の一部で数えます）
FORM6_PRINT_ROWS = [
    ("hearing1000", "聴力検査（オージオメーターによる検査）（1000Hz）",
     ("聴力(右:1000Hz)", "聴力(左:1000Hz)")),
    ("hearing4000", "聴力検査（オージオメーターによる検査）（4000Hz）",
     ("聴力(右:4000Hz)", "聴力(左:4000Hz)")),
    ("hearing_other", "聴力検査（その他の方法による検査）",
     ("聴力(検査方法)", "聴力(その他の所見)")),
    ("chest_xray", "胸部エックス線検査", ("胸部X線", "胸部エックス線", "胸部CT")),
    ("sputum", "喀痰検査", ("喀痰",)),
    ("bp", "血圧", ("血圧",)),
    ("anemia", "貧血検査", ("赤血球", "血色素", "ヘモグロビン", "ヘマトクリット")),
    ("liver", "肝機能検査", ("AST", "GOT", "ALT", "GPT", "γ-GT", "GTP", "ALP",
                          "ビリルビン", "総蛋白", "アルブミン")),
    ("lipid", "血中脂質検査", ("コレステロール", "中性脂肪", "トリグリセリド")),
    ("glucose", "血糖検査", ("血糖", "HbA1c")),
    ("urine_sugar", "尿検査（糖）", ("尿糖",)),
    ("urine_protein", "尿検査（蛋白）", ("尿蛋白",)),
    ("ecg", "心電図検査", ("心電図",)),
]
# 年齢階層別の受診労働者数（様式第6号の（※）欄。左から順に書きます）
FORM6_AGE_BANDS = [("15歳以下", 0, 15), ("16〜19歳", 16, 19), ("20〜24歳", 20, 24),
                   ("25〜29歳", 25, 29), ("30〜34歳", 30, 34), ("35〜39歳", 35, 39),
                   ("40〜44歳", 40, 44), ("45〜49歳", 45, 49), ("50〜54歳", 50, 54),
                   ("55〜59歳", 55, 59), ("60〜64歳", 60, 64), ("65〜69歳", 65, 69),
                   ("70歳以上", 70, 200)]

# 健診項目ごとの受診人数（eGov仕様書の項目）。項目名の一部で数えます
FORM6_ITEM_GROUPS = [
    ("basic_measurement_count", "身長・体重・腹囲・視力・聴力",
     ("身長", "体重", "腹囲", "視力", "聴力", "BMI")),
    ("chest_xray_count", "胸部エックス線検査", ("胸部X線", "胸部エックス線", "胸部CT")),
    ("sputum_count", "喀痰検査", ("喀痰",)),
    ("blood_pressure_count", "血圧測定", ("血圧",)),
    ("anemia_count", "貧血検査", ("赤血球", "血色素", "ヘモグロビン", "ヘマトクリット", "貧血")),
    ("liver_function_count", "肝機能検査", ("AST", "GOT", "ALT", "GPT", "γ-GT", "GTP",
                                        "ALP", "ビリルビン", "総蛋白", "アルブミン")),
    ("lipid_count", "血中脂質検査", ("コレステロール", "中性脂肪", "トリグリセリド")),
    ("glucose_count", "血糖検査", ("血糖", "HbA1c")),
    ("urinalysis_count", "尿検査", ("尿糖", "尿蛋白", "尿潜血", "尿比重", "尿沈渣")),
    ("ecg_count", "心電図検査", ("心電図",)),
]


def form6_empty(year):
    """健保が決まっていないとき（当社スタッフなど）に使う空の内容"""
    d = {k: "" for k, _l, _p in FORM6_TEXT_FIELDS}
    d.update(id=None, target_year=year, employees_count=None, updated_at=None)
    return d


def form6_row(db, acc, year, cid=None):
    """様式第6号の登録内容（無ければ空の行を作って返す）"""
    if not acc or not acc["kenpo_id"]:
        # 健保に属さないアカウント（当社スタッフ）は登録先が決まらないため空で返す
        return form6_empty(year)
    row = db.execute("SELECT * FROM form6_report WHERE kenpo_id=? AND company_id IS ?"
                     " AND target_year=?", (acc["kenpo_id"], cid, year)).fetchone()
    if row:
        return row
    db.execute("INSERT INTO form6_report (kenpo_id, company_id, target_year, updated_at)"
               " VALUES (?,?,?,?)", (acc["kenpo_id"], cid, year, H.now()))
    db.commit()
    return db.execute("SELECT * FROM form6_report WHERE kenpo_id=? AND company_id IS ?"
                      " AND target_year=?", (acc["kenpo_id"], cid, year)).fetchone()


def office_choices(acc):
    """報告書をしぼり込む事業所の一覧（担当範囲のなかだけ）"""
    db = H.get_db()
    where, params = H.member_where(acc)
    return db.execute(
        "SELECT o.id, o.name, o.zip, o.tel, o.address, c.name AS company_name,"
        " c.zip AS c_zip, c.tel AS c_tel, c.address AS c_address"
        " FROM office o JOIN company c ON c.id=o.company_id"
        f" WHERE EXISTS (SELECT 1 FROM member m WHERE m.office_id=o.id AND {where})"
        " ORDER BY c.code, o.code", list(params)).fetchall()


def office_row(acc, oid):
    """選んだ事業所（事業所マスタの内容）。選んでいなければ None"""
    if not oid:
        return None
    for o in office_choices(acc):
        if str(o["id"]) == str(oid):
            return o
    return None


def form6_from_office(office, f6, year):
    """様式第6号に載せる内容を、事業所マスタ（と登録済みの内容）から作る"""
    d = dict(f6) if f6 is not None else {}
    d.setdefault("target_year", year)
    # 事業場の名称・郵便番号・所在地・電話は、事業所マスタの内容だけを使います
    # （この画面では入力しません。直すときは事業所マスタで登録し直します）
    if office is not None:
        d["workplace_name"] = (office["company_name"] or "") + (
            "　" + office["name"] if office["name"] else "")
        d["workplace_zip"] = office["zip"] or office["c_zip"] or ""
        d["workplace_address"] = office["address"] or office["c_address"] or ""
        d["workplace_tel"] = office["tel"] or office["c_tel"] or ""
    else:
        for k in ("workplace_name", "workplace_zip", "workplace_address",
                  "workplace_tel"):
            d[k] = ""
    return d


def form6_counts(acc, fy, oid=None):
    """様式第6号に載せる人数を健診結果から集計する（事業所でしぼれます）"""
    db = H.get_db()
    where, params = H.member_where(acc)
    if oid:
        where += " AND m.office_id=?"
        params = list(params) + [oid]
    total = db.execute(f"SELECT COUNT(*) c FROM member m WHERE {where}",
                       list(params)).fetchone()["c"]
    rows = db.execute(
        "SELECT k.id, k.judge FROM oh_kenshin k JOIN member m ON m.id=k.member_id"
        f" WHERE {where} AND k.fiscal_year=? AND k.kind='定期'",
        list(params) + [fy]).fetchall()
    ids = [r["id"] for r in rows]
    items = []
    if ids:
        q = ",".join("?" * len(ids))
        items = db.execute(f"SELECT kenshin_id, name, judge FROM oh_kenshin_item"
                           f" WHERE kenshin_id IN ({q})", ids).fetchall()

    def hit_of(words):
        """その検査を受けた人（受診労働者数）と、有所見だった人（有所見者数）"""
        got, ng = set(), set()
        for r in items:
            nm = (r["name"] or "").lower()
            if not any(w.lower() in nm for w in words):
                continue
            got.add(r["kenshin_id"])
            if norm_judge(r["judge"]) in FINDING_JUDGES:
                ng.add(r["kenshin_id"])
        return len(got), len(ng)

    per = {}
    for key, _label, words in FORM6_ITEM_GROUPS:
        per[key] = hit_of(words)[0]
    print_rows = []
    for key, label, words in FORM6_PRINT_ROWS:
        n, ng = hit_of(words)
        print_rows.append({"key": key, "label": label, "n": n, "ng": ng})
    # 年齢階層別の受診労働者数
    ages = db.execute(
        "SELECT m.birth FROM oh_kenshin k JOIN member m ON m.id=k.member_id"
        f" WHERE {where} AND k.fiscal_year=? AND k.kind='定期'",
        list(params) + [fy]).fetchall()
    bands = []
    for label, lo, hi in FORM6_AGE_BANDS:
        n = 0
        for a in ages:
            age = age_of(a["birth"])
            if age is not None and lo <= age <= hi:
                n += 1
        bands.append({"label": label, "n": n})
    finding = sum(1 for r in rows if norm_judge(r["judge"]) in FINDING_JUDGES)
    ivs = db.execute(
        "SELECT COUNT(*) c FROM oh_interview i JOIN member m ON m.id=i.member_id"
        f" WHERE {where} AND i.fiscal_year=? AND IFNULL(i.measure,'') <> ''",
        list(params) + [fy]).fetchone()["c"]
    return {"employees": total, "examinees": len(rows), "items": per,
            "print_rows": print_rows, "bands": bands,
            "bands_total": sum(b["n"] for b in bands),
            "abnormal": finding, "instruction": ivs}


@bp.route("/report/form6", methods=["POST"])
@need("oh.report")
def oh_report_form6():
    """様式第6号の報告項目を登録する（eGov仕様書の項目）"""
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    year = (request.form.get("target_year") or fy).strip()[:4]
    if not acc["kenpo_id"]:
        flash("この報告書は健康保険組合ごとに登録します。"
              "健保に属するアカウントで操作してください。", "error")
        return redirect(url_for("oh.oh_report", fy=fy))
    row = form6_row(db, acc, year)
    vals, errs = {}, []
    for key, label, _ph in FORM6_TEXT_FIELDS:
        v = (request.form.get(key) or "").strip()
        if key == "examination_date" and v:
            v = H.norm_date(v) or v
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                errs.append("健診年月日は 2026-06-12 のように入力してください。")
        vals[key] = v
    emp = (request.form.get("employees_count") or "").strip()
    if emp and not emp.isdigit():
        errs.append("在籍労働者数は数字で入力してください。")
    if errs:
        for e in errs:
            flash(e, "error")
        return redirect(url_for("oh.oh_report", fy=fy))
    keys = list(vals.keys())
    db.execute("UPDATE form6_report SET " + ", ".join(f"{k}=?" for k in keys)
               + ", employees_count=?, updated_at=? WHERE id=?",
               [vals[k] for k in keys] + [int(emp) if emp else None, H.now(), row["id"]])
    db.commit()
    H.log("master", "定期健康診断結果報告書の報告項目を登録", "success",
          target=f"{year}年", detail=vals.get("workplace_name") or "事業場名未入力")
    flash(f"定期健康診断結果報告書（{year}年）の報告項目を保存しました。", "ok")
    return redirect(url_for("oh.oh_report", fy=fy))


@bp.route("/report")
@need("oh.report")
def oh_report():
    """労基署報告画面（F-01・F-05）。社内管理用サマリーと提出書類2種を表示する。"""
    acc = H.current_account()
    fy = (request.args.get("fy") or current_fy()).strip()
    db = H.get_db()
    signs = {r["kind"]: r for r in db.execute(
        "SELECT * FROM oh_sign WHERE fiscal_year=?", (fy,))}
    oid = (request.args.get("office") or "").strip()
    offices = office_choices(acc)
    office = office_row(acc, oid)
    if office is None:
        oid = ""
    f6 = form6_from_office(office, form6_row(db, acc, fy), fy)
    return render_template("sanmen_report.html",
                           s=report_summary(acc, fy, oid),
                           signs=signs, f6=f6, office=office,
                           offices=offices, oid=oid,
                           f6c=form6_counts(acc, fy, oid),
                           F6_FIELDS=FORM6_TEXT_FIELDS, F6_ITEMS=FORM6_ITEM_GROUPS,
                           **common(fy))


@bp.route("/report/sign", methods=["POST"])
@need("oh.sign")
def oh_report_sign():
    """産業医の記名・サイン（F-04）と署名情報の登録（I-04）"""
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    kind = request.form.get("kind") if request.form.get("kind") in ("kenshin", "stress") \
        else "kenshin"
    name = (request.form.get("name") or "").strip()
    title = (request.form.get("title") or "").strip()
    if not name:
        flash("記名する産業医の氏名を入力してください。", "error")
        return redirect(url_for("oh.oh_report", fy=fy))
    db.execute("INSERT INTO oh_sign (fiscal_year, kind, name, title, signed_at, account_id)"
               " VALUES (?,?,?,?,?,?)"
               " ON CONFLICT(fiscal_year, kind) DO UPDATE SET name=excluded.name,"
               " title=excluded.title, signed_at=excluded.signed_at,"
               " account_id=excluded.account_id",
               (fy, kind, name, title, H.now(), acc["id"]))
    # 次回以降の初期値として保持する（I-04 署名情報の登録）
    for k, v in (("oh_sign_name", name), ("oh_sign_title", title)):
        db.execute("INSERT INTO setting (key, value) VALUES (?,?)"
                   " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    db.commit()
    H.log("master", "報告書へ産業医が記名", "success",
          target=f"{fy}年度／{'様式第6号' if kind == 'kenshin' else 'ストレスチェック'}",
          detail=name)
    flash("記名・サインを登録しました。報告書に反映します。", "ok")
    return redirect(url_for("oh.oh_report", fy=fy))


@bp.route("/report/unsign", methods=["POST"])
@need("oh.sign")
def oh_report_unsign():
    """産業医の記名を取り消す（記名し直すとき）"""
    db = H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    kind = request.form.get("kind") if request.form.get("kind") in ("kenshin", "stress") \
        else "kenshin"
    db.execute("DELETE FROM oh_sign WHERE fiscal_year=? AND kind=?", (fy, kind))
    db.commit()
    H.log("master", "報告書の記名を取消", "success",
          target=f"{fy}年度／{'様式第6号' if kind == 'kenshin' else 'ストレスチェック'}")
    flash("記名を取り消しました。", "ok")
    return redirect(url_for("oh.oh_report", fy=fy))


@bp.route("/opinion/<int:mid>")
@need("oh.interview.view")
def oh_opinion(mid):
    """産業医意見書（1名分の帳票）。面談結果入力の記録から作り、印刷・PDF保存する。
    ロール別業務フローの「産業医：意見書を出力」に対応する。"""
    acc = H.current_account()
    fy = (request.args.get("fy") or current_fy()).strip()
    if not owns_member(acc, mid):
        return render_template("denied.html", path=request.path, notfound=True), 404
    rows = build_rows(acc, fy)
    r = next((x for x in rows if x["id"] == mid), None)
    if r is None:
        return render_template("denied.html", path=request.path, notfound=True), 404
    db = H.get_db()
    memos = db.execute(
        "SELECT * FROM oh_memo WHERE member_id=? AND fiscal_year=? AND kind IN ('医学的意見','就業上の措置')"
        " ORDER BY created_at DESC LIMIT 5", (mid, fy)).fetchall()
    H.log("download", "産業医意見書を出力", "success", target=f"member:{mid}（{fy}年度）")
    return render_template("sanmen_opinion.html", r=r, m=r["m"], iv=r["iv"], cand=r["cand"],
                           memos=memos, **common(fy))


@bp.route("/report/print/<kind>")
@need("oh.report")
def oh_report_print(kind):
    """報告書の印刷・PDF出力（F-02・F-03）"""
    if kind not in ("kenshin", "stress"):
        return render_template("denied.html", path=request.path, notfound=True), 404
    acc = H.current_account()
    fy = (request.args.get("fy") or current_fy()).strip()
    db = H.get_db()
    sign = db.execute("SELECT * FROM oh_sign WHERE fiscal_year=? AND kind=?",
                      (fy, kind)).fetchone()
    H.log("download", "報告書を出力", "success",
          target=f"{'定期健康診断結果報告書' if kind == 'kenshin' else 'ストレスチェック結果等報告書'}"
                 f"（{fy}年度）")
    oid = (request.args.get("office") or "").strip()
    office = office_row(acc, oid)
    if office is None:
        oid = ""
    return render_template("sanmen_report_print.html", kind=kind, sign=sign,
                           s=report_summary(acc, fy, oid),
                           f6=form6_from_office(office, form6_row(db, acc, fy), fy),
                           f6c=form6_counts(acc, fy, oid), office=office,
                           F6_ITEMS=FORM6_ITEM_GROUPS,
                           F6_ROWS=FORM6_PRINT_ROWS, **common(fy))


@bp.route("/report/csv")
@need("oh.report")
def oh_report_csv():
    """報告用集計のCSV出力（F-06）"""
    acc = H.current_account()
    fy = (request.args.get("fy") or current_fy()).strip()
    oid = (request.args.get("office") or "").strip()
    if office_row(acc, oid) is None:
        oid = ""
    s = report_summary(acc, fy, oid)
    rows = [
        ("在籍労働者数", s["total"]), ("受診労働者数", s["done"]),
        ("未受診者数", s["notdone"]), ("受診率（％）", f"{s['rate']:.1f}"),
        ("有所見者数（C判定以上）", s["finding"]),
    ]
    rows += [(f"判定区分 {j}（{JUDGE_LABEL[j]}）", s["judges"][j]) for j in JUDGES]
    rows += [
        ("深夜健診 対象者数", s["night_total"]), ("深夜健診 受診者数", s["night_done"]),
        ("面接指導 対象者数", s["iv_target"]), ("面接指導 実施数", s["iv_done"]),
        ("面接指導 未実施数", s["iv_rest"]),
    ]
    rows += [(f"就業区分 {w}", s["work"][w]) for w in WORK_CLASSES]
    rows += [
        ("ストレスチェック 対象者数", s["st_total"]), ("ストレスチェック 受検者数", s["st_done"]),
        ("高ストレス者数", s["st_high"]), ("面接指導の申出", s["st_apply"]),
        ("高ストレス者のうち面接指導実施", s["st_iv"]),
    ]
    return H.export_csv(f"sanmen_report_{fy}.csv", ["項目", "値"], rows,
                        f"労基署報告 集計（{fy}年度）") or redirect(url_for("oh.oh_report", fy=fy))


# ================================================================ G．データ取込
OT_COLUMNS = ["社員番号", "対象年月", "時間外労働時間", "休日労働時間"]
OT_SAMPLE = ["E0001", "2026-07", "92.5", "8.0"]
SC_COLUMNS = ["社員番号", "実施年度", "受検日", "合計点", "高ストレス者", "面接指導の申出"]
SC_SAMPLE = ["E0001", "2026", "2026-09-10", "78", "1", "1"]
# 健診結果CSV。検査値の列を入れると、取込のときに判定マスタの条件で
# 項目ごとの判定と総合判定を自動で付けます（総合判定の列は空欄でかまいません）
KEN_VALUE_COLUMNS = ["身長", "体重", "BMI", "収縮期血圧", "拡張期血圧",
                     "空腹時血糖", "HbA1c", "LDLコレステロール", "HDLコレステロール",
                     "中性脂肪", "AST(GOT)", "ALT(GPT)", "γ-GT(γ-GTP)", "eGFR",
                     "尿糖", "尿蛋白"]
KEN_COLUMNS = ["社員番号", "受診年度", "健診区分", "受診日", "総合判定",
               "産業医所見"] + KEN_VALUE_COLUMNS
KEN_SAMPLE = ["E0001", "2026", "定期", "2026-06-12", "", "血圧・脂質で再検査が必要",
              "170.2", "72.5", "25.0", "138", "84", "98", "5.8", "128", "58",
              "171", "25", "22", "90", "78", "", ""]

TEMPLATES = {
    "overtime": ("overtime_format.csv", OT_COLUMNS, OT_SAMPLE, "労働時間"),
    "stress": ("stresscheck_format.csv", SC_COLUMNS, SC_SAMPLE, "ストレスチェック"),
    "kenshin": ("kenshin_result_format.csv", KEN_COLUMNS, KEN_SAMPLE, "健診結果"),
}


@bp.route("/upload")
@need("oh.upload")
def oh_upload():
    """データ取込のハブ（G-01）"""
    acc, db = H.current_account(), H.get_db()
    fy = (request.args.get("fy") or current_fy()).strip()
    a, b = fy_span(fy)
    where, params = H.member_where(acc)
    n_ot = db.execute("SELECT COUNT(*) c FROM oh_overtime t JOIN member m ON m.id=t.member_id"
                      f" WHERE {where} AND t.ym BETWEEN ? AND ?",
                      list(params) + [a, b]).fetchone()["c"]
    n_st = db.execute("SELECT COUNT(*) c FROM oh_stress t JOIN member m ON m.id=t.member_id"
                      f" WHERE {where} AND t.fiscal_year=?",
                      list(params) + [fy]).fetchone()["c"]
    n_ken = db.execute("SELECT COUNT(*) c FROM oh_kenshin t JOIN member m ON m.id=t.member_id"
                       f" WHERE {where} AND t.fiscal_year=?",
                       list(params) + [fy]).fetchone()["c"]
    n_link = db.execute("SELECT COUNT(*) c FROM kenshin_result r JOIN member m ON m.id=r.member_id"
                        f" WHERE {where}", list(params)).fetchone()["c"]
    qs = db.execute("SELECT * FROM oh_sc_question ORDER BY category, no, id").fetchall()
    # only=kenshin / overtime / stress を付けると、その取込だけを表示します
    # （HIA健保管理の「インポート」からは1種類ずつ開きます）
    only = request.args.get("only") or ""
    if only not in ("kenshin", "overtime", "stress"):
        only = ""
    return render_template("sanmen_upload.html", n_ot=n_ot, n_st=n_st, n_ken=n_ken,
                           n_link=n_link, questions=qs, only=only,
                           ksum=kenshin_summary(acc, fy), **common(fy))


@bp.route("/template/<kind>.csv")
@need("oh.upload")
def oh_template(kind):
    """取込テンプレートの提供（G-06・H-04）"""
    if kind not in TEMPLATES:
        return render_template("denied.html", path=request.path, notfound=True), 404
    fn, cols, sample, label = TEMPLATES[kind]
    H.log("download", "取込フォーマットを出力", "success", target=f"産業医面談／{label}")
    return H.csv_response(fn, cols, [sample])


def find_member(acc, key):
    """社員番号・被保険者証番号・氏名で加入者を突合する（G-07 加入者名寄せ）"""
    key = (key or "").strip()
    if not key:
        return None
    where, params = H.member_where(acc)
    db = H.get_db()
    for sql in ("IFNULL(m.employee_code,'')=?", "m.member_no=?", "m.name=?",
                "IFNULL(m.subscriber_id,'')=?"):
        r = db.execute(f"SELECT m.id FROM member m WHERE {sql} AND {where}",
                       [key] + list(params)).fetchall()
        if len(r) == 1:
            return r[0]["id"]
        if len(r) > 1:
            return None
    return None


def judge_from_values(db, mid, fy, vals):
    """検査値を判定マスタの条件で判定する（企業ごとの基準 → 健保共通の基準）。

    健保共通の基準が無い項目は、HIA総合管理の判定マスタの内容を健保共通として
    反映してから使います（H.reflect_judge_defaults）。
    戻り値は [(検査項目名, 値, 単位, 判定), ...]。
    """
    m = db.execute("SELECT kenpo_id, company_id, sex FROM member WHERE id=?",
                   (mid,)).fetchone()
    if not m:
        return []
    kid, cid = m["kenpo_id"], m["company_id"]
    items, need = [], []
    for name, value in vals:
        it = H.judge_item_by_name(db, name)
        items.append((name, (value or "").strip(), it))
        if it:
            need.append(it["id"])
    # HIA健保管理に設定が無い項目は、HIA総合管理の判定マスタを反映する
    H.reflect_judge_defaults(db, kid, fy, need)
    rules = H.criteria_map(db, kid, cid, fy)
    out = []
    for name, value, it in items:
        if not it:
            out.append((name, value, "", None))
            continue
        j = H.judge_by_rules(rules.get(it["id"], []), value, m["sex"])
        out.append((it["name"], value, it["unit"] or "", j))
    return out


def kenshin_summary(acc, fy, kind="定期"):
    """取り込んだ健診結果の集計（判定マスタの条件で付けた判定で数えます）"""
    db = H.get_db()
    where, params = H.member_where(acc)
    rows = db.execute(
        "SELECT k.id, k.judge FROM oh_kenshin k JOIN member m ON m.id=k.member_id"
        f" WHERE {where} AND k.fiscal_year=? AND k.kind=?",
        list(params) + [fy, kind]).fetchall()
    ids = [r["id"] for r in rows]
    judges = {j: 0 for j in JUDGES}
    for r in rows:
        j = norm_judge(r["judge"])
        if j:
            judges[j] += 1
    per = {}
    if ids:
        q = ",".join("?" * len(ids))
        for r in db.execute(
                f"SELECT name, judge, COUNT(*) n FROM oh_kenshin_item"
                f" WHERE kenshin_id IN ({q}) GROUP BY name, judge", ids):
            d = per.setdefault(r["name"], {"n": 0, "finding": 0, "judges": {}})
            j = norm_judge(r["judge"])
            d["n"] += r["n"]
            d["judges"][j or "—"] = d["judges"].get(j or "—", 0) + r["n"]
            if j in FINDING_JUDGES:
                d["finding"] += r["n"]
    return {"n": len(rows), "judges": judges,
            "finding": sum(judges[j] for j in FINDING_JUDGES if j in judges),
            "per_item": sorted(per.items(), key=lambda kv: -kv[1]["finding"])}


def import_csv(kind):
    """取込の共通処理。1行ずつ突合して登録し、未マッチは要確認として返す。"""
    acc, db = H.current_account(), H.get_db()
    fs = request.files.get("file")
    if not fs or not fs.filename:
        return None, ["ファイルが選択されていません。"], []
    try:
        table = H.read_table(fs)
    except ValueError as e:
        return None, [str(e)], []
    ok, errs, unmatched = 0, [], []
    for i, row in enumerate(table, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        # 旧フォーマットの「社員コード」も受け付けます
        key = (row.get("社員番号") or row.get("社員コード")
               or row.get("被保険者証番号") or row.get("氏名"))
        mid = find_member(acc, key)
        if not mid:
            unmatched.append({"line": i, "key": key or "（空欄）",
                              "detail": "加入者マスタに一致する行がありません（または複数該当）"})
            continue
        try:
            if kind == "overtime":
                ym = (row.get("対象年月") or "").replace("/", "-")[:7]
                if not re.fullmatch(r"\d{4}-\d{2}", ym):
                    raise ValueError("対象年月の形式が正しくありません（例 2026-07）")
                db.execute("INSERT INTO oh_overtime (member_id, ym, hours, holiday_hours)"
                           " VALUES (?,?,?,?) ON CONFLICT(member_id, ym) DO UPDATE SET"
                           " hours=excluded.hours, holiday_hours=excluded.holiday_hours",
                           (mid, ym, float(row.get("時間外労働時間") or 0),
                            float(row.get("休日労働時間") or 0)))
            elif kind == "stress":
                fy = (row.get("実施年度") or current_fy()).strip()[:4]
                d = H.norm_date(row.get("受検日")) or ""
                high = 1 if (row.get("高ストレス者") or "").strip() in ("1", "該当", "はい",
                                                                  "有", "Y", "y") else 0
                ap = 1 if (row.get("面接指導の申出") or "").strip() in ("1", "有", "はい",
                                                                 "申出あり", "Y", "y") else 0
                score = row.get("合計点")
                db.execute("INSERT INTO oh_stress (member_id, fiscal_year, exam_date,"
                           " score, high, applied) VALUES (?,?,?,?,?,?)"
                           " ON CONFLICT(member_id, fiscal_year) DO UPDATE SET"
                           " exam_date=excluded.exam_date, score=excluded.score,"
                           " high=excluded.high, applied=excluded.applied",
                           (mid, fy, d, int(score) if (score or "").isdigit() else None,
                            high, ap))
            else:
                fy = (row.get("受診年度") or current_fy()).strip()[:4]
                k = row.get("健診区分") if row.get("健診区分") in KINDS else "定期"
                d = H.norm_date(row.get("受診日")) or ""
                # 検査値があれば、判定マスタの条件で項目ごとの判定と総合判定を作る
                vals = [(c, row.get(c)) for c in KEN_VALUE_COLUMNS if (row.get(c) or "").strip()]
                judged = judge_from_values(db, mid, fy, vals) if vals else []
                j = norm_judge(row.get("総合判定")) or worst_judge([x[3] for x in judged])
                if not j:
                    raise ValueError("総合判定を判別できません"
                                     "（A〜E で指定するか、検査値の列を入れてください）")
                db.execute("INSERT INTO oh_kenshin (member_id, fiscal_year, kind,"
                           " exam_date, judge, findings, source, updated_at)"
                           " VALUES (?,?,?,?,?,?, 'csv', ?)"
                           " ON CONFLICT(member_id, fiscal_year, kind) DO UPDATE SET"
                           " exam_date=excluded.exam_date, judge=excluded.judge,"
                           " findings=excluded.findings, updated_at=excluded.updated_at",
                           (mid, fy, k, d, j, row.get("産業医所見") or "", H.now()))
                if judged:
                    kid = db.execute("SELECT id FROM oh_kenshin WHERE member_id=?"
                                     " AND fiscal_year=? AND kind=?",
                                     (mid, fy, k)).fetchone()["id"]
                    db.execute("DELETE FROM oh_kenshin_item WHERE kenshin_id=?", (kid,))
                    for n, (name, value, unit, ij) in enumerate(judged, start=1):
                        db.execute("INSERT INTO oh_kenshin_item (kenshin_id, name, value,"
                                   " unit, judge, sort) VALUES (?,?,?,?,?,?)",
                                   (kid, name, value, unit, ij, n * 10))
            ok += 1
        except (ValueError, TypeError) as e:
            errs.append(f"{i}行目：{e}")
    db.commit()
    return ok, errs, unmatched


@bp.route("/upload/<kind>", methods=["POST"])
@need("oh.upload")
def oh_upload_do(kind):
    """労働時間CSV（G-03）・ストレスチェックCSV（G-04）・健診結果CSV（G-02）の取込"""
    if kind not in TEMPLATES:
        return render_template("denied.html", path=request.path, notfound=True), 404
    fy = (request.form.get("fy") or current_fy()).strip()
    ok, errs, unmatched = import_csv(kind)
    label = TEMPLATES[kind][3]
    if ok is None:
        for e in errs:
            flash(e, "error")
        H.log("import", f"{label}の取込に失敗", "failure", target="産業医面談",
              detail="／".join(errs)[:200])
        return redirect(url_for("oh.oh_upload", fy=fy))
    for e in errs[:10]:
        flash(e, "error")
    if unmatched:
        flash(f"{len(unmatched)}行は加入者と突合できませんでした（"
              + "／".join(f"{u['line']}行目：{u['key']}" for u in unmatched[:5])
              + ("…" if len(unmatched) > 5 else "") + "）。加入者マスタをご確認ください。", "error")
    flash(f"{label}を{ok}件取込みました。", "ok")
    detail = f"{ok}件／未突合{len(unmatched)}件／エラー{len(errs)}件"
    if kind == "kenshin":
        # 判定マスタの条件で付けた判定を集計してお知らせする
        sm = kenshin_summary(H.current_account(), fy)
        jt = "／".join(f"{j} {sm['judges'][j]}名" for j in JUDGES if sm["judges"][j])
        flash(f"判定マスタの条件で判定しました（{fy}年度・定期）："
              f"受診{sm['n']}名／有所見（C以上）{sm['finding']}名"
              + (f"　{jt}" if jt else "")
              + "。下の「取込結果の集計」からCSVで出力できます。", "ok")
        detail += f"／受診{sm['n']}名・有所見{sm['finding']}名・{jt}"
    H.log("import", f"{label}を取込", "success", target="産業医面談", detail=detail)
    return redirect(url_for("oh.oh_upload", fy=fy))


@bp.route("/upload/summary.csv")
@need("oh.upload")
def oh_upload_summary():
    """取込結果の集計をCSVで出力する（判定マスタの条件で付けた判定で数えます）"""
    acc = H.current_account()
    fy = (request.args.get("fy") or current_fy()).strip()
    sm = kenshin_summary(acc, fy)
    rows = [["総合判定", "—", "受診者数", sm["n"]],
            ["総合判定", "—", "有所見（C以上）", sm["finding"]]]
    for j in JUDGES:
        rows.append(["総合判定", j, JUDGE_LABEL[j], sm["judges"][j]])
    for name, d in sm["per_item"]:
        rows.append(["検査項目", name, "判定した件数", d["n"]])
        rows.append(["検査項目", name, "有所見（C以上）", d["finding"]])
        for j in JUDGES:
            if d["judges"].get(j):
                rows.append(["検査項目", name, f"{j}（{JUDGE_LABEL[j]}）", d["judges"][j]])
    return H.export_csv(f"kenshin_summary_{fy}.csv",
                        ["区分", "対象", "内容", "件数"], rows,
                        f"健診結果の集計（{fy}年度・定期）") \
        or redirect(url_for("oh.oh_upload", fy=fy))


@bp.route("/upload/reflect", methods=["POST"])
@need("oh.upload")
def oh_upload_reflect():
    """HIA総合管理の判定マスタを、HIA健保管理の健保共通の基準として反映する"""
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    n_item, n_row = H.reflect_judge_defaults(db, acc["kenpo_id"], fy)
    if n_item:
        flash(f"HIA総合管理の判定マスタから{n_item}項目・{n_row}件を"
              f"健保共通の基準として反映しました（{fy}年度）。", "ok")
    else:
        flash("反映する項目はありませんでした（すでに健保共通の基準があります）。", "ok")
    return redirect(url_for("oh.oh_upload", fy=fy))


@bp.route("/upload/link", methods=["POST"])
@need("oh.upload")
def oh_upload_link():
    """本システムの健診結果連携（疾患予測で取込んだ個人の判定）から総合判定を作る。

    既存の kenshin_result は検査項目ごとの判定（基準内・要注意・要医療）なので、
    いちばん重い判定を学会区分（A〜E）へ読み替えて総合判定として登録する。
    """
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    where, params = H.member_where(acc)
    rows = db.execute(
        "SELECT r.member_id, r.exam_date, r.item, r.judge FROM kenshin_result r"
        f" JOIN member m ON m.id=r.member_id WHERE {where}", list(params)).fetchall()
    by = {}
    for r in rows:
        d = by.setdefault(r["member_id"], {"judges": [], "date": None, "items": []})
        d["judges"].append(r["judge"])
        d["items"].append(r)
        if r["exam_date"] and (not d["date"] or r["exam_date"] > d["date"]):
            d["date"] = r["exam_date"]
    n = 0
    for mid, d in by.items():
        j = worst_judge(d["judges"])
        if not j:
            continue
        exam = d["date"] or ""
        y = fy_of(exam) or fy
        db.execute("INSERT INTO oh_kenshin (member_id, fiscal_year, kind, exam_date,"
                   " judge, source, updated_at) VALUES (?,?, '定期', ?,?, 'kenshin_result', ?)"
                   " ON CONFLICT(member_id, fiscal_year, kind) DO UPDATE SET"
                   " exam_date=excluded.exam_date, judge=excluded.judge,"
                   " source=excluded.source, updated_at=excluded.updated_at",
                   (mid, y, exam, j, H.now()))
        kid = db.execute("SELECT id FROM oh_kenshin WHERE member_id=? AND fiscal_year=?"
                         " AND kind='定期'", (mid, y)).fetchone()["id"]
        db.execute("DELETE FROM oh_kenshin_item WHERE kenshin_id=?", (kid,))
        for i, it in enumerate(d["items"]):
            db.execute("INSERT INTO oh_kenshin_item (kenshin_id, name, value, judge, sort)"
                       " VALUES (?,?,?,?,?)",
                       (kid, it["item"], "", norm_judge(it["judge"]), i))
        n += 1
    db.commit()
    H.log("import", "健診結果の連携から総合判定を生成", "success", target="産業医面談",
          detail=f"{n}名")
    flash(f"本システムの健診結果から{n}名の総合判定を作成しました。", "ok")
    return redirect(url_for("oh.oh_upload", fy=fy))


@bp.route("/questions")
@need("oh.upload")
def oh_questions():
    """ストレスチェック設問（マスタ管理から開く画面）"""
    db = H.get_db()
    fy = (request.args.get("fy") or current_fy()).strip()
    qs = db.execute("SELECT * FROM oh_sc_question ORDER BY no, category, id").fetchall()
    return render_template("sanmen_questions.html", questions=qs, **common(fy))


@bp.route("/question", methods=["POST"])
@need("oh.upload")
def oh_question():
    """ストレスチェック設問の追加・削除（H-03）"""
    db = H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    if request.form.get("delete", type=int):
        db.execute("DELETE FROM oh_sc_question WHERE id=?",
                   (request.form.get("delete", type=int),))
        db.commit()
        flash("設問を削除しました。", "ok")
        return redirect(url_for("oh.oh_questions", fy=fy))
    cat = (request.form.get("category") or "").strip()
    body = (request.form.get("body") or "").strip()
    no = request.form.get("no", type=int) or 0
    if not cat or not body:
        flash("カテゴリと設問内容を入力してください。", "error")
    else:
        db.execute("INSERT INTO oh_sc_question (category, no, body) VALUES (?,?,?)",
                   (cat, no, body))
        db.commit()
        H.log("master", "ストレスチェック設問を追加", "success", target=cat)
        flash("設問を追加しました。", "ok")
    return redirect(url_for("oh.oh_questions", fy=fy))


# ================================================================ D．メール配信
DEFAULT_TEMPLATES = [
    ("面談受診勧奨", "産業医面談のご案内",
     "産業医面談のご案内（{{氏名}} 様）",
     "{{氏名}} 様\n\n"
     "健康診断の結果等にもとづき、産業医面談の対象となりました。\n"
     "下記より面談のご予約をお願いいたします。\n\n"
     "　ご予約：{{予約URL}}\n"
     "　回答期限：{{回答期限}}\n\n"
     "本メールは健康管理の一環としてお送りしています。内容は産業医と人事担当者のみが確認します。\n"
     "※ 面談内容にもとづく就業上の措置は、産業医の判断により決定します。\n"),
    ("ストレスチェック案内", "ストレスチェック受検のご案内",
     "ストレスチェック受検のご案内（{{氏名}} 様）",
     "{{氏名}} 様\n\n"
     "今年度のストレスチェックを実施します。下記より受検をお願いいたします。\n\n"
     "　受検：{{予約URL}}\n"
     "　回答期限：{{回答期限}}\n\n"
     "回答内容は法令にもとづき保護され、人事評価には一切利用されません。\n"),
]

# 案内を送ったあと返事がない方への「リマインド」の既定テンプレート
# （テンプレート名に「リマインド」を含めると、リマインドのカードで選べます）
REMIND_WORD = "リマインド"
REMIND_TEMPLATES = [
    ("面談受診勧奨", "産業医面談のご案内（リマインド）",
     "【再度のご案内】産業医面談のご予約について（{{氏名}} 様）",
     "{{氏名}} 様\n\n"
     "先日ご案内した産業医面談について、まだご予約の確認ができておりません。\n"
     "お忙しいところ恐れ入りますが、下記よりご予約をお願いいたします。\n\n"
     "　ご予約：{{予約URL}}\n"
     "　回答期限：{{回答期限}}\n\n"
     "すでにご予約いただいている場合は、本メールはご容赦ください。\n"
     "本メールは健康管理の一環としてお送りしています。内容は産業医と人事担当者のみが確認します。\n"),
    ("健康管理のお知らせ", "再検査のご案内（リマインド）",
     "【再度のご案内】再検査の受診・結果のご提出について（{{氏名}} 様）",
     "{{氏名}} 様\n\n"
     "先日ご案内した再検査について、まだ結果のご提出が確認できておりません。\n"
     "受診がお済みの方は、マイページから結果の写真をアップロードしてください。\n"
     "まだの方は、期限までの受診をお願いいたします。\n\n"
     "　マイページ：{{URL}}\n"
     "　期限：{{期限}}\n\n"
     "ご不明な点は {{担当窓口}} までお問い合わせください。\n"
     "すでにご提出いただいている場合は、本メールはご容赦ください。\n"),
]


def ensure_templates():
    db = H.get_db()
    if not db.execute("SELECT COUNT(*) c FROM oh_mail_template").fetchone()["c"]:
        for kind, name, subject, body in DEFAULT_TEMPLATES:
            db.execute("INSERT INTO oh_mail_template (kind, name, subject, body)"
                       " VALUES (?,?,?,?)", (kind, name, subject, body))
    # リマインドのテンプレートは、種別ごとに1つも無ければ既定のものを足します
    for kind, name, subject, body in REMIND_TEMPLATES:
        has = db.execute("SELECT COUNT(*) c FROM oh_mail_template WHERE kind=?"
                         " AND name LIKE ?", (kind, f"%{REMIND_WORD}%")).fetchone()["c"]
        if not has:
            db.execute("INSERT INTO oh_mail_template (kind, name, subject, body)"
                       " VALUES (?,?,?,?)", (kind, name, subject, body))
    db.commit()


REMIND_DAYS = [3, 7, 14, 30]


def reminder_targets(acc, fy, days):
    """案内を送ったあと、返事（予約・結果の提出）がないまま日数がたった方を集める。

    ・面談受診勧奨　　：勧奨メール送信済のまま予約・面談に進んでいない方
    ・健康管理のお知らせ：案内を送って「保健師対応中」のまま結果の提出がない方
    最後に送ってから days 日以上たった方だけを出します（送った回数も添えます）。
    """
    db = H.get_db()
    members = {m["id"]: m for m in scoped_members(acc)}
    if not members:
        return []
    ids = list(members)
    cands = fetch_map("SELECT * FROM oh_candidate WHERE member_id IN ({IN})"
                      " AND fiscal_year=?", ids, params_tail=(fy,))
    # 種別ごとの最終送信日時と送信回数（成功したものだけ）
    last = {}
    for r in fetch_rows("SELECT member_id, kind, MAX(sent_at) AS last_at, COUNT(*) AS n"
                        " FROM oh_mail_log WHERE member_id IN ({IN}) AND result='success'"
                        " AND direction='out' AND kind IN ('面談受診勧奨','健康管理のお知らせ')"
                        " GROUP BY member_id, kind", ids):
        last[(r["member_id"], r["kind"])] = r
    today = date.today()
    out = []
    for mid, m in members.items():
        c = cands.get(mid)
        if not c:
            continue
        wants = []
        if c["status"] == ST_MAILED:
            wants.append("面談受診勧奨")
        if c["hr_class"] == HR_NURSE:
            wants.append("健康管理のお知らせ")
        for kind in wants:
            lg = last.get((mid, kind))
            if not lg:
                continue
            try:
                d0 = date.fromisoformat(lg["last_at"][:10])
            except ValueError:
                continue
            passed = (today - d0).days
            if passed < days:
                continue
            out.append({"id": mid, "m": m, "kind": kind, "last_at": lg["last_at"][:10],
                        "n": lg["n"], "days": passed,
                        "state": c["status"] if kind == "面談受診勧奨" else c["hr_class"],
                        "due": c["due_on"] or ""})
    out.sort(key=lambda r: (-r["days"], r["m"]["name"] or ""))
    return out


def fill_template(text, m, deadline, url, fy="", exam_date="", desk=""):
    """差し込み項目を反映する（D-09）

    「健康管理のお知らせ」のテンプレートで使う項目（年度・受診日・期限・URL・担当窓口）も
    ここでまとめて置き換えます。期限は回答締切、URLは予約URL／マイページURLの入力を使います。
    """
    return ((text or "")
            .replace("{{氏名}}", m["name"] or "")
            .replace("{{予約URL}}", url or "（URLは配信設定で指定します）")
            .replace("{{URL}}", url or "（URLは配信設定で指定します）")
            .replace("{{回答期限}}", deadline or "（未設定）")
            .replace("{{期限}}", deadline or "（未設定）")
            .replace("{{年度}}", str(fy or ""))
            .replace("{{受診日}}", exam_date or "（未受診）")
            .replace("{{担当窓口}}", desk or "健康管理のご担当窓口"))


@bp.route("/mail")
@need("oh.mail")
def oh_mail():
    """メール配信（統合画面。D-01〜D-10）"""
    acc = H.current_account()
    ensure_templates()
    f = form_filter()
    kind = request.args.get("kind") if request.args.get("kind") in MAIL_KINDS \
        else MAIL_KINDS[0]
    rows = build_rows(acc, f["fy"], f)
    flow_c = flow_counts(rows)
    db = H.get_db()
    # テンプレートと配信履歴は種別を問わずこの画面にまとめます
    # （健康管理のお知らせの送信そのものは加入者健康一覧から行います）
    tpls = db.execute("SELECT * FROM oh_mail_template ORDER BY kind, id").fetchall()
    logs = db.execute(
        "SELECT l.*, m.name FROM oh_mail_log l LEFT JOIN member m ON m.id=l.member_id"
        " ORDER BY l.id DESC LIMIT 50").fetchall()
    camps = db.execute("SELECT * FROM oh_sc_campaign ORDER BY id DESC LIMIT 10").fetchall()
    sent_map = {r["member_id"]: r["c"] for r in db.execute(
        "SELECT member_id, COUNT(*) c FROM oh_mail_log WHERE kind=? AND result='success'"
        " GROUP BY member_id", (kind,))}
    # リマインド：案内を送ったあと返事がない方（経過日数でしぼる）
    rdays = request.args.get("rdays", type=int)
    if rdays not in REMIND_DAYS:
        rdays = 7
    remind = reminder_targets(acc, f["fy"], rdays)
    remind_tpls = [t for t in tpls if REMIND_WORD in (t["name"] or "")]
    return render_template("sanmen_mail.html", rows=rows, f=f, kind=kind, flow_c=flow_c,
                           tpls=tpls, logs=logs, camps=camps, sent_map=sent_map,
                           remind=remind, remind_tpls=remind_tpls, rdays=rdays,
                           REMIND_DAYS=REMIND_DAYS,
                           MAIL_KINDS=MAIL_KINDS, mail_on=H.mail_enabled(),
                           **common(f["fy"]))


@bp.route("/mail/remind", methods=["POST"])
@need("oh.mail")
def oh_mail_remind():
    """リマインドの送信。案内を送ったあと返事がない方へ、リマインドのテンプレートで再送する。

    ステータス（勧奨メール送信済／保健師対応中）は変えず、送った回数だけ増やします。
    """
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    rdays = request.form.get("rdays", type=int) or 7
    back = url_for("oh.oh_mail", fy=fy, rdays=rdays)
    deadline = (request.form.get("deadline") or "").strip()
    url = (request.form.get("book_url") or "").strip()
    desk = (request.form.get("desk") or "").strip()
    # 選んだ行は「加入者ID:種別」の形で受け取ります
    picks = []
    for v in request.form.getlist("targets"):
        mid, _, kind = str(v).partition(":")
        if mid.isdigit() and kind in ("面談受診勧奨", "健康管理のお知らせ"):
            picks.append((int(mid), kind))
    picks = [(m, k) for m, k in dict.fromkeys(picks) if owns_member(acc, m)]
    if not picks:
        flash("リマインドを送る方を1名以上選んでください。", "error")
        return redirect(back)
    tpls = {t["kind"]: t for t in db.execute(
        "SELECT * FROM oh_mail_template WHERE name LIKE ? ORDER BY id", (f"%{REMIND_WORD}%",))}
    # 画面で選んだテンプレート（種別ごと）があれば優先します
    for kind in ("面談受診勧奨", "健康管理のお知らせ"):
        tid = request.form.get(f"tpl_{kind}", type=int)
        if tid:
            t = db.execute("SELECT * FROM oh_mail_template WHERE id=?", (tid,)).fetchone()
            if t:
                tpls[kind] = t
    sent = failed = 0
    for mid, kind in picks:
        m = member_row(acc, mid)
        tpl = tpls.get(kind)
        if not m or not tpl:
            failed += 1
            continue
        k = db.execute("SELECT exam_date FROM oh_kenshin WHERE member_id=?"
                       " AND fiscal_year=? AND kind='定期'", (mid, fy)).fetchone()
        exam = (k["exam_date"] if k else "") or ""
        subject = fill_template(tpl["subject"], m, deadline, url, fy, exam, desk)
        body = fill_template(tpl["body"], m, deadline, url, fy, exam, desk)
        n_before = db.execute("SELECT COUNT(*) c FROM oh_mail_log WHERE member_id=? AND kind=?"
                              " AND result='success' AND direction='out'",
                              (mid, kind)).fetchone()["c"]
        ok, note = False, "メールアドレスが未登録"
        if m["email"]:
            try:
                res = bool(H.send_mail(m["email"], subject, body))
            except Exception:
                res = False
            if res:
                ok, note = True, f"{REMIND_WORD}（{n_before}回目）"
            elif not H.mail_enabled():
                ok, note = True, f"{REMIND_WORD}（{n_before}回目）／SMTP未設定のため outbox に控えを保存"
            else:
                note = "送信できませんでした（送信設定をご確認ください）"
        db.execute("INSERT INTO oh_mail_log (kind, template_id, member_id, subject,"
                   " actor, result, detail) VALUES (?,?,?,?,?,?,?)",
                   (kind, tpl["id"], mid, subject, actor(),
                    "success" if ok else "failure", note))
        if ok:
            sent += 1
            if kind == "面談受診勧奨":
                cur = ensure_candidate(mid, fy)
                db.execute("UPDATE oh_candidate SET mail_count=mail_count+1, last_mail_at=?,"
                           " updated_at=? WHERE id=?", (H.now(), H.now(), cur["id"]))
        else:
            failed += 1
    db.commit()
    H.log("master", f"メールの{REMIND_WORD}", "success", target=f"{fy}年度",
          detail=f"送信{sent}件／失敗{failed}件")
    msg = f"{REMIND_WORD}を{sent}件送信しました。"
    if failed:
        msg += f"（{failed}件は送信できませんでした。メールアドレス・送信設定をご確認ください）"
    if not H.mail_enabled() and sent:
        msg += "（送信設定が未登録のため、控えを outbox に保存しました）"
    flash(msg, "ok" if sent else "error")
    return redirect(back)


@bp.route("/mail/template", methods=["POST"])
@need("oh.mail")
def oh_mail_template():
    """配信テンプレートの編集（D-08・D-09）"""
    db = H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    tid = request.form.get("id", type=int)
    name = (request.form.get("name") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    body = (request.form.get("body") or "").strip()
    kind = request.form.get("kind") if request.form.get("kind") in MAIL_KINDS \
        else MAIL_KINDS[0]
    if not (name and subject and body):
        flash("テンプレート名・件名・本文を入力してください。", "error")
    elif tid:
        db.execute("UPDATE oh_mail_template SET name=?, subject=?, body=?, kind=?,"
                   " updated_at=? WHERE id=?", (name, subject, body, kind, H.now(), tid))
        db.commit()
        H.log("master", "配信テンプレートを更新", "success", target=name)
        flash("テンプレートを更新しました。", "ok")
    else:
        db.execute("INSERT INTO oh_mail_template (kind, name, subject, body)"
                   " VALUES (?,?,?,?)", (kind, name, subject, body))
        db.commit()
        H.log("master", "配信テンプレートを追加", "success", target=name)
        flash("テンプレートを追加しました。", "ok")
    return redirect(url_for("oh.oh_mail", fy=fy, kind=kind))


@bp.route("/mail/send", methods=["POST"])
@need("oh.mail")
def oh_mail_send():
    """メール送信・リマインド（D-06・D-07・D-10）

    面談受診勧奨は面談対象（承認済以降）のみ送信し、非対象はスキップする。
    ストレスチェック案内は全加入者へ送信する。
    """
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    kind = request.form.get("kind") if request.form.get("kind") in MAIL_KINDS \
        else MAIL_KINDS[0]
    tid = request.form.get("template_id", type=int)
    deadline = (request.form.get("deadline") or "").strip()
    url = (request.form.get("book_url") or "").strip()
    ids = [int(x) for x in request.form.getlist("member_ids") if str(x).isdigit()]
    ids = [i for i in dict.fromkeys(ids) if owns_member(acc, i)]
    tpl = db.execute("SELECT * FROM oh_mail_template WHERE id=?", (tid,)).fetchone()
    if not tpl:
        flash("配信テンプレートを選択してください。", "error")
        return redirect(url_for("oh.oh_mail", fy=fy, kind=kind))
    if not ids:
        flash("送信対象を1名以上選択してください。", "error")
        return redirect(url_for("oh.oh_mail", fy=fy, kind=kind))

    sent = skipped = failed = 0
    for mid in ids:
        m = member_row(acc, mid)
        if not m:
            continue
        c = db.execute("SELECT * FROM oh_candidate WHERE member_id=? AND fiscal_year=?",
                       (mid, fy)).fetchone()
        if kind == "面談受診勧奨" and (
                not c or c["status"] not in (ST_APPROVED, ST_MAILED, ST_BOOKED)):
            db.execute("INSERT INTO oh_mail_log (kind, template_id, member_id, subject,"
                       " actor, result, detail) VALUES (?,?,?,?,?, 'skipped', ?)",
                       (kind, tpl["id"], mid, tpl["subject"], actor(),
                        "面談対象ではないためスキップ"))
            skipped += 1
            continue
        k = db.execute("SELECT exam_date FROM oh_kenshin WHERE member_id=?"
                       " AND fiscal_year=? AND kind='定期'", (mid, fy)).fetchone()
        exam = (k["exam_date"] if k else "") or ""
        desk = (request.form.get("desk") or "").strip()
        subject = fill_template(tpl["subject"], m, deadline, url, fy, exam, desk)
        body = fill_template(tpl["body"], m, deadline, url, fy, exam, desk)
        ok, note = False, "メールアドレスが未登録"
        if m["email"]:
            try:
                res = bool(H.send_mail(m["email"], subject, body))
            except Exception:
                res = False
            if res:
                ok, note = True, ""
            elif not H.mail_enabled():
                # SMTPが未設定のときは outbox に控えを書き出す（本体と同じ扱い）
                ok, note = True, "SMTP未設定のため outbox に控えを保存"
            else:
                note = "送信できませんでした（送信設定をご確認ください）"
        db.execute("INSERT INTO oh_mail_log (kind, template_id, member_id, subject,"
                   " actor, result, detail) VALUES (?,?,?,?,?,?,?)",
                   (kind, tpl["id"], mid, subject, actor(),
                    "success" if ok else "failure", note))
        if ok:
            sent += 1
            if kind == "面談受診勧奨":
                cur = ensure_candidate(mid, fy)
                db.execute("UPDATE oh_candidate SET status=?, mail_count=mail_count+1,"
                           " last_mail_at=?, updated_at=? WHERE id=?",
                           (ST_MAILED, H.now(), H.now(), cur["id"]))
        else:
            failed += 1
    if kind == "ストレスチェック案内":
        db.execute("INSERT INTO oh_sc_campaign (fiscal_year, period_from, period_to,"
                   " deadline, sent, actor) VALUES (?,?,?,?,?,?)",
                   (fy, (request.form.get("period_from") or "").strip(),
                    (request.form.get("period_to") or "").strip(), deadline, sent, actor()))
    db.commit()
    H.log("master", "産業医面談のメール配信", "success", target=f"{kind}（{fy}年度）",
          detail=f"送信{sent}件／スキップ{skipped}件／失敗{failed}件")
    msg = f"{kind}を{sent}件送信しました。"
    if skipped:
        msg += f"（面談対象外{skipped}件はスキップしました）"
    if failed:
        msg += f"（{failed}件は送信できませんでした。メールアドレス・送信設定をご確認ください）"
    flash(msg, "ok" if sent else "error")
    return redirect(url_for("oh.oh_mail", fy=fy, kind=kind))
