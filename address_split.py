# -*- coding: utf-8 -*-
"""住所文字列を 都道府県・市区町村・残り（番地など）に分ける（できる範囲で。手直しは編集画面で）。"""
import re

_PREF = re.compile(r"^(東京都|北海道|京都府|大阪府|.{2,3}?県)")
# 市区町村：郡＋町村 → 市（政令市の区まで） → 区 → 町・村 の順に試す
_CITY = [re.compile(r"^(.+?郡.+?[町村])"),
         re.compile(r"^(.+?市(?:[^\d０-９\-−－丁]{1,5}?区)?)"),
         re.compile(r"^(.+?区)"),
         re.compile(r"^(.+?[町村])")]


def split_address(full):
    """'大阪府〇〇市△△町9-7-21' → ('大阪府', '〇〇市', '△△町9-7-21')。
    都道府県が見つからないときは ('', '', full) を返す（元の値を失わない）。"""
    s = (full or "").strip()
    if not s:
        return "", "", ""
    m = _PREF.match(s)
    if not m:
        return "", "", s
    pref, rest = m.group(1), s[m.end():]
    for pat in _CITY:
        c = pat.match(rest)
        if c and len(c.group(1)) <= 12:
            return pref, c.group(1), rest[c.end():]
    return pref, "", rest


def join_address(pref, city, address, address2=""):
    return "".join(x for x in (pref or "", city or "", address or "") if x) + \
        (("　" + address2) if address2 else "")
