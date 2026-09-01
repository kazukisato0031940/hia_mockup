# -*- coding: utf-8 -*-
"""マスタに登録されている加入者ぶんの健診結果XML（特定健診 CDA R2）を作る。

  python make_kenshin_xml.py

samples/kenshin_xml/ に、加入者1名につき1ファイル出力します。
検査値は年代・性別から機械的に決めた見本データで、実在の結果ではありません。
加入者を追加したあとに実行し直すと、追加分のXMLも作られます。
"""
import io
import os
import sqlite3
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "hia.db")
OUT = os.path.join(BASE, "samples", "kenshin_xml")

ITEMS = [
    ("9N001000000000001", "収縮期血圧", "mmHg"),
    ("9N006000000000001", "拡張期血圧", "mmHg"),
    ("3D045000001927101", "ＨｂＡ１ｃ（ＮＧＳＰ）", "%"),
    ("3D010000001926101", "空腹時血糖", "mg/dL"),
    ("3F077000002327101", "ＬＤＬコレステロール", "mg/dL"),
    ("3F070000002327101", "ＨＤＬコレステロール", "mg/dL"),
    ("3F015000002327201", "中性脂肪", "mg/dL"),
    ("3B035000002327101", "ＡＳＴ（ＧＯＴ）", "U/L"),
    ("3B045000002327101", "ＡＬＴ（ＧＰＴ）", "U/L"),
    ("3B090000002327101", "γ－ＧＴ（γ－ＧＴＰ）", "U/L"),
    ("9N016000000000001", "ＢＭＩ", "kg/m2"),
    ("1A020000019113201", "尿蛋白", ""),
]

TPL = '''<?xml version="1.0" encoding="UTF-8"?>
<!-- 特定健診情報（HL7 CDA R2）の見本データ。実在の個人の結果ではありません。 -->
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <realmCode code="JP"/>
  <typeId root="2.16.840.1.113883.1.3" extension="POCD_HD000040"/>
  <templateId root="1.2.392.200119.6.1001"/>
  <id root="1.2.392.200119.6.102" extension="{doc_id}"/>
  <code code="10" codeSystem="1.2.392.200119.6.1005" displayName="特定健診情報"/>
  <title>特定健康診査結果</title>
  <effectiveTime value="{exam_date}"/>
  <confidentialityCode code="N" codeSystem="2.16.840.1.113883.5.25"/>
  <languageCode code="ja-JP"/>

  <recordTarget>
    <patientRole>
      <!-- 被保険者証の記号・番号・枝番で加入者を特定します -->
      <id root="1.2.392.200119.6.208" extension="{mark}"/>
      <id root="1.2.392.200119.6.209" extension="{no}"/>
      <id root="1.2.392.200119.6.210" extension="{branch}"/>
      <patient>
        <administrativeGenderCode code="{sex}" codeSystem="1.2.392.200119.6.1101"/>
        <birthTime value="{birth}"/>
      </patient>
    </patientRole>
  </recordTarget>

  <author>
    <time value="{exam_date}"/>
    <assignedAuthor>
      <id root="1.2.392.200119.6.102" extension="9999999999"/>
      <representedOrganization><name>見本健診センター</name></representedOrganization>
    </assignedAuthor>
  </author>

  <custodian>
    <assignedCustodian>
      <representedCustodianOrganization>
        <id root="1.2.392.200119.6.101" extension="{kenpo_code}"/>
        <name>{kenpo_name}</name>
      </representedCustodianOrganization>
    </assignedCustodian>
  </custodian>

  <component><structuredBody><component><section>
    <code code="1" codeSystem="1.2.392.200119.6.1002" displayName="検査結果"/>
    <title>検査結果</title>
{entries}  </section></component></structuredBody></component>
</ClinicalDocument>
'''
E_NUM = '''    <entry><observation classCode="OBS" moodCode="EVN">
      <code code="{code}" codeSystem="1.2.392.200119.6.1005" displayName="{name}"/>
      <value xsi:type="PQ" value="{value}" unit="{unit}"
             xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>
    </observation></entry>
'''
E_STR = '''    <entry><observation classCode="OBS" moodCode="EVN">
      <code code="{code}" codeSystem="1.2.392.200119.6.1005" displayName="{name}"/>
      <value xsi:type="ST"
             xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">{value}</value>
    </observation></entry>
'''


def age_of(birth):
    try:
        return datetime.now().year - int(str(birth)[:4])
    except (TypeError, ValueError):
        return 40


def make_values(age, sex, seed):
    """年代・性別から検査値を機械的に作る。年齢が上がるほど有所見が増える。"""
    # 0.0（若く健康）〜 1.0（高齢で有所見が多い）
    w = min(max((age - 22) / 45, 0.0), 1.0)
    w = min(w + ((seed % 30) - 12) / 100.0, 1.05)     # 個人差
    w = max(w, 0.0)
    m = 1.0 if sex == "男" else 0.92                   # 男性はやや高め

    def v(lo, hi, r=0):
        x = lo + (hi - lo) * w * m
        return round(x, r) if r else int(round(x))

    sbp = v(104, 168)
    dbp = v(64, 100)
    hba1c = round(4.9 + 2.6 * w * m, 1)
    glu = v(80, 145)
    ldl = v(88, 176)
    hdl = v(78, 33)                                    # 高いほど良い
    tg = v(62, 340)
    ast = v(14, 48)
    alt = v(12, 58)
    ggt = v(13, 128)
    bmi = round(18.6 + 10.6 * w * m, 1)
    up = "-" if w < 0.45 else ("±" if w < 0.7 else ("1+" if w < 0.88 else "2+"))
    return [sbp, dbp, hba1c, glu, ldl, hdl, tg, ast, alt, ggt, bmi, up]


def main():
    if not os.path.exists(DB):
        print("hia.db がありません。先に seed.py を実行してください。")
        return
    os.makedirs(OUT, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT m.*, k.code AS kenpo_code, k.name AS kenpo_name FROM member m"
        " JOIN kenpo k ON k.id=m.kenpo_id ORDER BY k.code, m.member_no,"
        " m.cert_branch").fetchall()
    con.close()
    if not rows:
        print("加入者が登録されていません。")
        return

    # 前回作ったものは消して作り直す（マスタと必ず一致させる）
    for f in os.listdir(OUT):
        if f.lower().endswith(".xml"):
            os.remove(os.path.join(OUT, f))

    exam = f"{datetime.now().year}0601"
    n = 0
    for r in rows:
        mark = (r["cert_mark"] or "").strip()
        no = (r["member_no"] or "").strip()
        br = (r["cert_branch"] or "").strip() or "0"
        birth = (r["birth"] or "").replace("-", "")[:8] or "19800101"
        sex_code = "2" if (r["sex"] or "") == "女" else "1"
        seed = abs(hash((mark, no, br))) % 1000
        values = make_values(age_of(r["birth"]), r["sex"] or "男", seed)
        entries = ""
        for (code, name, unit), v in zip(ITEMS, values):
            t = E_STR if isinstance(v, str) else E_NUM
            entries += t.format(code=code, name=name, value=v, unit=unit)
        xml = TPL.format(doc_id=f"{exam}{no}{br}", exam_date=exam, mark=mark, no=no,
                         branch=br, sex=sex_code, birth=birth,
                         kenpo_code=r["kenpo_code"], kenpo_name=r["kenpo_name"],
                         entries=entries)
        fn = f"{r['kenpo_code']}_{mark or 'x'}_{no}_{br}.xml"
        io.open(os.path.join(OUT, fn), "w", encoding="utf-8", newline="\n").write(xml)
        n += 1
    print(f"健診結果XMLを {n} 件作成しました： samples/kenshin_xml/")
    print("　マスタに登録されている加入者ぶんです。加入者を追加したら実行し直してください。")


if __name__ == "__main__":
    main()
