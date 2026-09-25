# -*- coding: utf-8 -*-
"""HIA健保管理（青）の画面を組み立てる。

原本 HIA健保管理.html に対して行う変更はここに集約している。
原本を差し替えた場合は、このスクリプトを実行すれば同じ変更を再適用できる。

  python build_kenpo.py [原本のパス]
"""
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/HIA健保管理.html"
OUT = os.path.join(BASE, "static", "hia_app.html")

HEAD_CSS = '''
/* ============================================================
   本システム（サーバ連携画面）の埋め込み
   ============================================================ */
.sys-frame{width:100%;border:0;display:block;min-height:520px;overflow:hidden;background:transparent}
/* 追加ページのコンテナ。見出しの縦位置を一覧・詳細でそろえるため基準にする */
[id^="page-sys-"]{position:relative}
/* 見出し・説明文・戻るボタンの配置
   「戻る」は常に右上へ固定し、行の高さに影響させない */
.sys-head{display:flex;align-items:flex-start;gap:10px;margin:0 0 6px;min-height:0}
.sys-head h1,.sys-head .page-title{margin:0}
.sys-head .btn-back{position:absolute;top:0;right:0;margin:0}
.sys-head.floating{margin:0}
.sys-note{margin:0 0 18px;font-size:13px;color:var(--gray-label);line-height:1.7}
/* サーバ連携バッジは画面の基調色に合わせる */
.sys-flag{display:inline-block;padding:3px 10px;border-radius:4px;font-size:10px;font-weight:700;
  background:#ebf8ff;color:var(--primary);vertical-align:middle}
</style>'''

SYS_JS = '''
/* ============ 本システム（サーバ連携画面）の読み込み ============ */
var SYS = {
  /* iframe 内が自前の見出しを持つ場合は、シェル側の見出しと説明を隠す
     （一覧＝シェル側の見出し／詳細画面＝画面側の見出し の一方だけを表示する） */
  setHeader: function(frameEl, hasOwnHeading){
    var host = frameEl && frameEl.closest ? frameEl.closest('.page') : null;
    if(!host) return;
    var head = host.querySelector('.sys-head');
    var note = host.querySelector('.sys-note');
    if(head){
      var t = head.querySelector('h1');
      var f = head.querySelector('.sys-flag');
      var bk = head.querySelector('.btn-back');
      if(t) t.style.display = hasOwnHeading ? 'none' : '';
      if(f) f.style.display = hasOwnHeading ? 'none' : '';
      if(bk) bk.style.display = hasOwnHeading ? 'none' : '';
      head.classList.toggle('floating', !!hasOwnHeading);
    }
    if(note) note.style.display = hasOwnHeading ? 'none' : '';
  },
  open: function(pageId){
    var pg = document.getElementById('page-' + pageId);
    if(!pg) return;
    var f = pg.querySelector('iframe.sys-frame');
    if(f && !f.getAttribute('src')){ f.setAttribute('src', f.dataset.src); }
    /* 高さを固定する画面は、表示のたびに画面の高さへ合わせ直す */
    setTimeout(function(){
      try{
        if(f && f.contentDocument
           && f.contentDocument.body.classList.contains('fixed-h')) SYS.fitHeight(f);
      }catch(e){}
    }, 60);
  },
  reload: function(pageId){
    var pg = document.getElementById('page-' + pageId);
    var f = pg && pg.querySelector('iframe.sys-frame');
    if(f){ f.setAttribute('src', f.dataset.src); }
  },
  resize: function(frameEl, h){ if(frameEl && h){ frameEl.style.height = (h + 24) + 'px'; } },
  /* 画面いっぱいに固定する（一覧のように中身だけスクロールさせたい画面向け） */
  fitHeight: function(frameEl){
    if(!frameEl) return;
    window.scrollTo(0, 0);                       /* 画面内に収めるため先頭へ */
    var top = frameEl.getBoundingClientRect().top;
    var h = Math.max(320, Math.round(window.innerHeight - top - 28));
    frameEl.style.height = h + 'px';
    SYS._fitTarget = frameEl;
  }
};
window.SYS = SYS;
window.addEventListener('resize', function(){
  if(SYS._fitTarget && SYS._fitTarget.isConnected) SYS.fitHeight(SYS._fitTarget);
});
'''

HEADER_JS = '''
<script>
/* ヘッダーのログアウトとログイン情報（シェルは静的HTMLのためここで差し込む） */
(function(){
  document.querySelectorAll('.header-logout').forEach(function(a){
    a.setAttribute('href', '/logout');
    a.removeAttribute('onclick');
    a.onclick = null;
  });
  fetch('/api/me', {credentials:'same-origin'})
    .then(function(r){ return r.ok ? r.json() : null; })
    .then(function(me){
      if(!me) return;
      var n = document.querySelector('.header-username');
      var av = document.querySelector('.header-avatar');
      if(n){ n.textContent = me.name; n.title = me.email + '（' + me.role + '）'; }
      if(av){ av.textContent = me.initials; av.title = me.role + '／' + me.scope; }
    })
    .catch(function(){});
})();
</script>
'''

SB_MASTER = """
    <div class="sb-cat-item" id="cat-master" data-cat="master" onclick="selectCategory('master')">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10M15 10v10"/>
      </svg>
      <span class="sb-cat-label">マスタ</span>
    </div>
"""

SB_RISK = """
    <div class="sb-cat-item" id="cat-risk" data-cat="risk" onclick="selectCategory('risk')">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="M3 15l4-6 4 4 4-7 6 9"/><path d="M3 20h18"/><circle cx="7" cy="9" r="1.4"/>
      </svg>
      <span class="sb-cat-label">疾患予測</span>
    </div>
"""

CAT_RISK = """  risk: {
    title: '疾患予測',
    desc: '健診データとNSIPS（調剤実績）から、将来の生活習慣病リスクを予測します',
    cards: [
    ]
  },
"""

SB_RECEIPT = """
    <div class="sb-cat-item" id="cat-receipt" data-cat="receipt" onclick="selectCategory('receipt')">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="M6 2h9l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z"/><path d="M14 2v6h6M9 13h6M9 17h4"/>
      </svg>
      <span class="sb-cat-label">レセプト</span>
    </div>
"""

CAT_RECEIPT = """  receipt: {
    title: 'レセプト管理',
    subtitle: 'レセプト情報のアップロードと履歴の確認',
    color: '#805ad5', iconBg: '#f3ebff',
    cards: [
    ]
  },
"""

CAT_MASTER = """  master: {
    title: 'マスタ管理',
    subtitle: '企業・事業所・加入者の登録と編集',
    color: '#2b6cb0', iconBg: '#ebf8ff',
    cards: [
      { page:'sys-company', label:'企業情報', desc:'企業の登録・編集・削除。企業コードは自動採番', tag:'マスタ', tagClass:'tag-sys',
        icon:'<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 8h2M13 8h2M9 12h2M13 12h2M9 16h6"/>' },
      { page:'sys-office', label:'事業所情報', desc:'事業所の登録・編集・削除。事業所コードは自動採番', tag:'マスタ', tagClass:'tag-sys',
        icon:'<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>' },
      { page:'sys-dept', label:'部署情報', desc:'部署の登録・編集・削除。事業所の下に置く', tag:'マスタ', tagClass:'tag-sys',
        icon:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="8" y="14" width="8" height="7" rx="1"/><path d="M6.5 10v2h11v-2M12 12v2"/>' },
      { page:'sys-member', label:'加入者情報', desc:'加入者の登録・編集・削除・CSV取込', tag:'マスタ', tagClass:'tag-sys',
        icon:'<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/>' },
    ]
  },
"""

RISK_CARDS = """      { page:'sys-risk', label:'予測結果一覧', desc:'属性区分ごとのリスクスコアと保健指導の優先度', tag:'予測', tagClass:'tag-sys',
        icon:'<path d="M3 15l4-6 4 4 4-7 6 9"/><path d="M3 20h18"/><circle cx="7" cy="9" r="1.4"/>' },
      { page:'sys-risk-kenshin', label:'健診結果の連携', desc:'予約管理システムからのバッチ連携の実行と履歴', tag:'予測', tagClass:'tag-sys',
        icon:'<path d="M8 3H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V5a2 2 0 00-2-2h-2"/><rect x="8" y="2" width="8" height="4" rx="1"/><path d="M8 12h3l1.5-3 2 6 1.5-3h2"/>' },
      { page:'sys-risk-nsips', label:'NSIPS連携状況', desc:'調剤システムとのAPI連携の実行と履歴', tag:'予測', tagClass:'tag-sys',
        icon:'<path d="M4 7h6a4 4 0 010 8H8"/><circle cx="18" cy="7" r="2.4"/><circle cx="6" cy="15" r="2.4"/><path d="M14 7h1.6"/>' },
"""

SET_CARDS = """      { page:'sys-account', label:'アカウント管理', desc:'アカウントの登録・編集・削除。登録前に閲覧範囲を確認', tag:'管理', tagClass:'tag-sys',
        icon:'<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/><path d="M17 11l2 2 4-4"/>' },
"""


def page(pid, title, cat, desc, path):
    return f'''
      <!-- ======== 本システム: {title} ======== -->
      <div class="page" id="page-{pid}">
        <div class="sys-head">
          <h1 class="page-title">{title}</h1>
          <span class="sys-flag">サーバ連携</span>
          <button class="btn-back" onclick="selectCategory('{cat}')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
            戻る
          </button>
        </div>
        <p class="sys-note">{desc}</p>
        <iframe class="sys-frame" data-src="{path}" title="{title}"></iframe>
      </div>
'''


def take_card(html, label):
    """CAT_CARD_CONFIG の data カテゴリからカード定義を切り出して返す"""
    di = html.index("  data: {")
    dend = html.index("  master: {") if "  master: {" in html else html.index("  influenza: {")
    dseg = html[di:dend]
    k = dseg.index(label)
    a = dseg.rindex("      { page:", 0, k)
    e = dseg.index("' },", k) + len("' },") + 1
    card = dseg[a:e]
    return html[:di] + dseg[:a] + dseg[e:] + html[dend:], card


def put_cards(html, cat, cards):
    """カテゴリの cards の先頭にカード定義を差し込む"""
    m = re.search(r"(  " + cat + r": \{.*?cards: \[\n)", html, re.S)
    return html[:m.end()] + "".join(cards) + html[m.end():]


def put_after(html, cat, after_label, cards):
    """カテゴリの指定カードの直後にカード定義を差し込む"""
    m = re.search(r"(  " + cat + r": \{.*?cards: \[\n)", html, re.S)
    start = m.end()
    end = html.index("    ]", start)
    seg = html[start:end]
    k = seg.index(after_label)
    e = seg.index("' },", k) + len("' },") + 1
    return html[:start] + seg[:e] + "".join(cards) + seg[e:] + html[end:]


def drop_page(html, page_id):
    """画面（div.page）をコメントごと削除する"""
    pi = html.index('<div class="page" id="page-' + page_id + '">')
    ci = html.rindex("<!-- ===========================", 0, pi)
    ce = html.index("<!-- ===========================",
                    html.index("/page-" + page_id + " -->", pi))
    return html[:ci] + html[ce:]


def order_sidebar(html, order):
    """サイドバーの並びを指定どおりにする"""
    items = re.findall(r'    <div class="sb-cat-item" id="cat-([a-z]+)".*?\n    </div>\n',
                       html, re.S)
    blocks = {}
    for m in re.finditer(r'    <div class="sb-cat-item" id="cat-([a-z]+)".*?\n    </div>\n',
                         html, re.S):
        blocks[m.group(1)] = m.group(0)
    assert set(blocks) == set(order), f"カテゴリの指定に漏れがあります: {set(blocks) ^ set(order)}"
    first = html.index('    <div class="sb-cat-item"')
    last = html.rindex('    <div class="sb-cat-item"')
    end = html.index("    </div>\n", last) + len("    </div>\n")
    return html[:first] + "\n".join(blocks[k] for k in order) + html[end:]


def fix_back(html, page_id, cat):
    """画面の「戻る」ボタンの移動先カテゴリを直す"""
    pi = html.index('id="page-' + page_id + '"')
    pe = html.index("</button>", pi)
    return (html[:pi]
            + re.sub(r"selectCategory\('[a-z]+'\)", "selectCategory('" + cat + "')",
                     html[pi:pe])
            + html[pe:])


def find_close(html, open_idx):
    """open_idx にある開始タグに対応する </div> の位置を返す"""
    i = html.index(">", open_idx) + 1
    depth = 1
    for m in re.finditer(r"<div\b|</div\s*>", html[i:]):
        if m.group(0).startswith("<div"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return i + m.start()
    raise RuntimeError("閉じタグが見つかりません")


def build():
    s = io.open(SRC, encoding="utf-8").read()
    orig = len(s)

    # 1) 名称を HIA健保管理 に統一
    s = s.replace("<title>くすりの窓口 健保管理システム</title>", "<title>HIA健保管理</title>")
    s = s.replace("くすりの窓口 健保管理システム", "HIA健保管理")
    s = s.replace("くすりの窓口 健保管理", "HIA健保管理")

    # 2) スタイル
    s = s.replace("</style>", HEAD_CSS, 1)
    # 表示倍率（90%相当）のスタイル。いちばん外側の文書だけが読み込む
    s = s.replace("</head>", '<link rel="stylesheet" href="/ui/zoom.css">\n</head>', 1)

    # 3) サイドバーの寸法（HIA総合管理と統一）
    s = s.replace(".sidebar {\n  width: 72px;", ".sidebar {\n  width: 88px;", 1)
    m = re.search(r"\.sb-cat-item \{[^}]*\}", s)
    s = s.replace(m.group(0), m.group(0).replace("width: 52px;", "width: 72px;")
                  .replace("height: 52px;", "height: 64px;").replace("gap: 4px;", "gap: 5px;"), 1)
    m = re.search(r"\.sb-cat-label \{[^}]*\}", s)
    s = s.replace(m.group(0), m.group(0).replace("font-size: 8px;", "font-size: 11px;")
                  .replace("font-weight: 700;", "font-weight: 400;")
                  .replace("line-height: 1.2;", "line-height: 1.1;"), 1)

    # 3.5) 使わないカードと画面を削除する
    #   接種対象者情報 UP … 使用しない
    #   加盟企業情報 UP／加入者情報 UP … マスタの一括インポートで代替
    for label, page_id in (("接種対象者情報", "target-upload"),
                           ("加盟企業情報 UP", "corp-upload"),
                           ("加入者情報 UP", "member-upload"),
                           ("健診コースデータ UP", "course-upload"),
                           ("各種フォーマット出力", "format-dl")):
        s, _ = take_card(s, label)
        s = drop_page(s, page_id)

    # 3.6) 原本のカードを移動する。「共通・基盤データ管理」は空にして廃止する
    s, card_vac = take_card(s, "接種実績")
    s, card_rcp = take_card(s, "レセプト情報 UP")
    s, card_hist = take_card(s, "UP履歴：レセプト")

    # 4) サイドバーに「マスタ」「レセプト」を追加し、カテゴリ定義とカードを登録する
    anchor = '    <div class="sb-cat-item" id="cat-influenza"'
    s = s.replace(anchor,
                  SB_MASTER.strip("\n") + "\n\n" + SB_RECEIPT.strip("\n") + "\n\n"
                  + SB_RISK.strip("\n") + "\n\n" + anchor, 1)
    s = s.replace("  influenza: {",
                  CAT_MASTER + CAT_RECEIPT + CAT_RISK + "  influenza: {", 1)
    s = put_cards(s, "receipt", [card_rcp, card_hist])
    s = put_cards(s, "risk", [RISK_CARDS])
    s = put_after(s, "influenza", "申請一覧", [card_vac])
    s = put_cards(s, "settings", [SET_CARDS])

    # 5) 空になった「共通・基盤データ管理」をサイドバーとカテゴリ定義から外す
    di = s.index('    <div class="sb-cat-item" id="cat-data"')
    s = s[:di] + s[s.index('    <div class="sb-cat-item"', di + 10):]
    ci = s.index("  data: {")
    s = s[:ci] + s[s.index("  master: {"):]

    # 5.4) レイアウトの高さを画面に合わせる（原本は1080px固定だった）
    s = s.replace(".app-container { display:flex; min-height:calc(1080px - 52px); }",
                  ".app-container { display:flex; min-height:calc(100vh / var(--ui-zoom,1) - 52px); }")
    s = s.replace("system-ui,sans-serif; min-height:1080px; }",
                  "system-ui,sans-serif; min-height:calc(100vh / var(--ui-zoom,1)); }")
    # 表示倍率（--ui-zoom）の分だけ 100vh が短くなるので割り戻す
    s = s.replace("  height: calc(100vh - 52px);\n  overflow-y: auto;",
                  "  height: calc(100vh / var(--ui-zoom,1) - 52px);\n  overflow-y: auto;", 1)

    # 5.5) 4文字のラベルは1行で表示する
    s = s.replace('<span class="sb-cat-label">健康<br>診断</span>',
                  '<span class="sb-cat-label">健康診断</span>')
    s = s.replace('<span class="sb-cat-label">保健<br>指導</span>',
                  '<span class="sb-cat-label">保健指導</span>')

    # 6) サイドバーの並び
    s = order_sidebar(s, ["kenshin", "hoken", "influenza", "analysis",
                          "risk", "receipt", "master", "settings"])

    # 画面の「戻る」も移動先のカテゴリへ
    s = fix_back(s, "vaccination-upload", "influenza")
    s = fix_back(s, "receipt-upload", "receipt")
    s = fix_back(s, "receipt-history", "receipt")

    # 5) initPage フック
    s = s.replace("""function initPage(pageId) {
  if (initialized[pageId]) {""",
                  """function initPage(pageId) {
  if (pageId.indexOf('sys-') === 0) { SYS.open(pageId); return; }
  if (initialized[pageId]) {""", 1)

    # 6) SYS モジュール
    m = re.search(r"\nfunction initPage\(pageId\) \{", s)
    s = s[:m.start()] + "\n" + SYS_JS + s[m.start():]

    # 7) ページ本体を #mainArea の内側に置く
    new = (page('sys-risk', '疾患予測', 'risk',
                '取込んだ健診結果をもとに、加入者ひとりずつの3年後の生活習慣病リスクを'
                '予測します。保健指導の優先度づけにお使いください。',
                '/risk')
           + page('sys-risk-kenshin', '健診結果の連携', 'risk',
                  '当社の予約管理システムから健診結果をバッチで取込みます。'
                  '取込むのは判定区分の人数だけで、検査値そのものは保持しません。',
                  '/risk/kenshin')
           + page('sys-risk-nsips', 'NSIPS連携状況', 'risk',
                  '自社の調剤システムとのAPI連携の状況を確認します。'
                  '個人を特定しうる項目を検知した場合は取込まず除外します。', '/risk/nsips')
           + page('sys-company', '企業情報', 'master',
                '企業の登録・編集・削除を行います。企業コードは健保が管理する番号です。', '/companies')
           + page('sys-office', '事業所情報', 'master',
                  '事業所の登録・編集・削除を行います。事業所コードはシステムが自動採番します。', '/offices')
           + page('sys-dept', '部署情報', 'master',
                  '部署の登録・編集・削除を行います。部署は事業所の下に置きます。', '/departments')
           + page('sys-member', '加入者情報', 'master',
                  '加入者の登録・編集・削除とCSV取込を行います。閲覧範囲内の加入者だけが表示されます。',
                  '/members')
           + page('sys-account', 'アカウント管理', 'settings',
                  'アカウントの登録・編集・削除を行います。登録前に、そのアカウントが閲覧できる加入者を確認します。',
                  '/accounts'))
    oi = s.index('<div class="main-area" id="mainArea">')
    ci = find_close(s, oi)
    s = s[:ci] + new + "    " + s[ci:]

    # 8) 外部CDNをやめて同梱ファイルを使う（社内LANでも動くようにする）
    for a, b in [("https://cdn.jsdelivr.net/npm/chart.js", "/static/vendor/chart.min.js"),
                 ("https://cdn.jsdelivr.net/npm/xlsx-js-style@1.2.0/dist/xlsx.bundle.js",
                  "/static/vendor/xlsx.bundle.js"),
                 ("https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js",
                  "/static/vendor/html2canvas.min.js"),
                 ("https://cdn.jsdelivr.net/npm/jspdf@2.5.1/dist/jspdf.umd.min.js",
                  "/static/vendor/jspdf.umd.min.js")]:
        s = s.replace(a, b)

    # 9) ヘッダーのログアウトを有効化し、ログイン情報を差し込む
    s = s.replace('<a class="header-logout" href="#" onclick="return false;">',
                  '<a class="header-logout" href="/logout">')
    s = s.replace('<a href="#" class="header-logout" onclick="return false;">',
                  '<a href="/logout" class="header-logout">')
    s = s.replace("</body>", HEADER_JS + "</body>", 1)

    # ---- サポートログイン中の表示をヘッダーに出す ----
    s = s.replace("""  <div style="display:flex;align-items:center;gap:16px;">
    <div class="header-user">""",
"""  <div style="display:flex;align-items:center;gap:16px;">
    <!-- サポートログイン中の表示（当社スタッフが健保の画面に入っているとき） -->
    <div id="support-tag" style="display:none">
      <span class="sp-ico" aria-hidden="true">!</span>
      <span class="sp-body">
        <span class="sp-label">サポートログイン中</span>
        <b class="sp-kenpo" id="support-kenpo"></b>
      </span>
      <form method="post" action="/support/end" style="margin:0">
        <button class="sp-end" title="サポートを終了して当社の画面に戻ります">終了</button>
      </form>
    </div>
    <div class="header-user">""")

    # 表示の切り替え
    s = s.replace("</body>", """
<script>
/* サポートログイン中かを確認してヘッダーに表示する */
fetch('/api/support/state', {credentials:'same-origin'})
  .then(r => r.ok ? r.json() : null)
  .then(d => {
    if(!d || !d.support) return;
    document.getElementById('support-kenpo').textContent =
      d.kenpo.code + '　' + d.kenpo.name;
    document.getElementById('support-tag').style.display = '';
    document.body.classList.add('is-support');
  }).catch(() => {});
</script>
</body>""")

    # スタイル
    s = s.replace("</style>", """
/* サポートログイン中の表示（ヘッダー） */
#support-tag{display:flex;align-items:center;gap:9px;padding:5px 10px 5px 8px;
  border-radius:6px;background:#7b341e;color:#fff;font-size:12px;
  box-shadow:0 1px 4px rgba(0,0,0,.2)}
#support-tag .sp-ico{flex:0 0 auto;width:18px;height:18px;border-radius:50%;
  background:#fff;color:#7b341e;font-weight:700;display:inline-flex;
  align-items:center;justify-content:center;font-size:12px}
#support-tag .sp-body{display:flex;flex-direction:column;line-height:1.35}
#support-tag .sp-label{font-size:10px;opacity:.9;letter-spacing:.04em}
#support-tag .sp-kenpo{font-size:12px;font-weight:700;white-space:nowrap}
#support-tag .sp-end{padding:4px 11px;border-radius:4px;border:1px solid #fff;
  background:transparent;color:#fff;font-size:11px;font-weight:700;cursor:pointer}
#support-tag .sp-end:hover{background:#fff;color:#7b341e}
/* サポート中はヘッダーの下に細い帯を出して、状態が分かるようにする */
body.is-support header{box-shadow:0 3px 0 0 #7b341e}
</style>""")

    # ---- サイドバーの文字を太くして読みやすくする ----
    s = s.replace(
        ".sb-cat-label {\n  font-size: 11px;\n  font-weight: 400;",
        ".sb-cat-label {\n  font-size: 11.5px;\n  font-weight: 600;")
    # 選択していない項目の文字が薄すぎると太字が沈むので、少し濃くする
    s = s.replace("color: rgba(255,255,255,0.48);", "color: rgba(255,255,255,0.68);")

    # ---- ボタンの高さを全画面で40pxに統一 ----
    s = s.replace("</style>", """
/* ============================================================
   ボタンの高さを全画面で 40px に統一
   ============================================================ */
button,
.btn-primary, .btn-secondary, .btn-action, .btn-csv, .btn-zip, .btn-edit,
.btn-submit, .btn-back, .btn-save, .btn-sm, .btn-ghost, .btn-cancel,
.btn-cancel-page, .btn-submit-page, .modal-btn-sec, .modal-btn-pri,
.tbl-btn, .tbl-btn-edit, .tbl-btn-confirm, .status-btn, .st-btn,
.flu-act-btn, .flu-prev-btn, .flu-status-btn, .flu-csv-btn, .flu-detail-btn,
a.btn-primary, a.btn-secondary, a.btn-sm, a.btn-back {
  height: 40px !important;
  min-height: 40px !important;
  padding-top: 0 !important;
  padding-bottom: 0 !important;
  display: inline-flex !important;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  line-height: 1.2;
}
/* 丸いページ送りは正円のまま大きくする */
.pager-btn { width: 40px !important; height: 40px !important; padding: 0 !important; }
/* 表の中はボタンに合わせて行の余白を詰める */
td.op-cell, .result-table td.ops { padding-top: 6px; padding-bottom: 6px; }
/* 閉じるボタンなど、正方形で使うものは幅も40pxに揃える */
.modal-x, .modal-close { width: 40px !important; height: 40px !important;
  padding: 0 !important; }
</style>""", 1)

    # 原本の古い指定（13px固定）を外す
    s = s.replace(
        "input::placeholder, textarea::placeholder { color:#a0aec0; font-size:13px; opacity:1; }",
        "")
    s = s.replace(".flu-input::placeholder { color:#a0aec0; }", "")

    # ---- プレースホルダーの見た目を統一 ----
    s = s.replace("</style>", """
/* プレースホルダーの見た目を全画面でそろえる */
input::placeholder,
textarea::placeholder {
  color: #a0aec0 !important;
  opacity: 1 !important;
  font-size: inherit !important;
  font-weight: 400 !important;
  font-style: normal !important;
  letter-spacing: normal !important;
}
</style>""", 1)

    # ---- プルダウンの見た目を統一 ----
    s = s.replace("</style>", """
/* プルダウンの見た目を全画面でそろえる */
select:not([multiple]) {
  height: 35px !important;
  padding: 0 34px 0 12px !important;
  border: 1px solid #ccc !important;
  border-radius: 6px !important;
  background-color: #fff !important;
  color: #2d3748;
  font-size: 13px;
  font-family: inherit;
  box-sizing: border-box;
  cursor: pointer;
  -webkit-appearance: none;
  -moz-appearance: none;
  appearance: none;
  background-image: url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5L6 6.5L11 1.5' fill='none' stroke='%23718096' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") !important;
  background-repeat: no-repeat !important;
  background-position: right 12px center !important;
  background-size: 12px 8px !important;
}
select:not([multiple]):hover { border-color: #a0aec0 !important }
select:not([multiple]):focus { outline: 2px solid #3182CE; border-color: #3182CE !important }
select:disabled { background-color: #f7fafc !important; color: #718096; cursor: not-allowed;
  background-image: url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5L6 6.5L11 1.5' fill='none' stroke='%23cbd5e0' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") !important }
</style>""", 1)

    # ---- 入力欄の高さを40pxにそろえる ----
    s = s.replace("</style>", """
/* 入力欄・プルダウン・ボタンの高さを全画面で40pxにそろえる */
input[type="text"], input[type="number"], input[type="password"],
input[type="date"], input[type="month"], input[type="time"],
input[type="email"], input[type="tel"], input[type="search"], select,
.form-control {
  height: 40px !important;
  min-height: 40px !important;
  box-sizing: border-box;
}
textarea { min-height: 40px; box-sizing: border-box; }
/* 原本の小さめ指定（インフル補助の入力欄など）も40pxにそろえる */
.flu-edit, .flu-input, input.flu-edit {
  height: 40px !important; min-height: 40px !important; box-sizing: border-box;
}
</style>""", 1)

    # ---- サイドバーの横幅は 88px（原本のまま）----

    io.open(OUT, "w", encoding="utf-8").write(s)
    return orig, len(s), s


if __name__ == "__main__":
    a, b, s = build()
    print(f"生成: {OUT}")
    print(f"  {a:,} → {b:,} 文字")
    src = io.open(SRC, encoding="utf-8").read()
    old = set(re.findall(r'id="(page-[a-z0-9\-]+)"', src))
    new = set(re.findall(r'id="(page-[a-z0-9\-]+)"', s))
    print("  既存ページの欠落:", sorted(old - new) or "なし")
    print("  追加したページ:", sorted(new - old))
    print("  CDN参照の残り:", s.count("cdn.jsdelivr"))
    print("  「くすりの窓口」の残り:", s.count("くすりの窓口"))
    i = s.index("  settings: {")
    print("  設定・サポートのカード:",
          re.findall(r"label:'([^']+)'", s[i:s.index("]", i)]))
    for cat in ("kenshin", "master", "receipt", "influenza", "settings"):
        i = s.index("  " + cat + ": {")
        print(f"  {cat:10}", re.findall(r"label:'([^']+)'", s[i:s.index("]", i)]))
    print("  サイドバー:", re.findall(r'class="sb-cat-label">([^<]+)<', s))
