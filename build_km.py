# -*- coding: utf-8 -*-
"""HIA総合管理（緑）の画面を組み立てる。

原本 HIA_integrated.html に対して行う変更はここに集約している。
原本を差し替えた場合は、このスクリプトを実行すれば同じ変更を再適用できる。

  python build_km.py [原本のパス]
"""
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/HIA_integrated.html"
OUT = os.path.join(BASE, "static", "km_app.html")

HEAD_CSS = '''<style>
/* ============================================================
   本システム（サーバ連携画面）の埋め込み
   ============================================================ */
.sys-frame{width:100%;border:0;display:block;min-height:520px;overflow:hidden;background:transparent}
/* 追加ページのコンテナ。見出しの縦位置を一覧・詳細でそろえるため基準にする */
[id^="view-sys-"]{position:relative}
/* 見出し・説明文・戻るボタンの配置
   「戻る」は常に右上へ固定し、行の高さに影響させない */
.sys-head{display:flex;align-items:flex-start;gap:10px;margin:0 0 6px;min-height:0}
.sys-head h1{margin:0}
.sys-head .back-btn{position:absolute;top:0;right:0;margin:0}
.sys-head.floating{margin:0}
.sys-note{margin:0 0 18px;font-size:13px;color:#666;line-height:1.7}
/* サーバ連携バッジは画面の基調色に合わせる */
.sys-flag{display:inline-block;padding:3px 10px;border-radius:4px;font-size:10px;font-weight:700;
  background:#e6efee;color:#2f4f4f;vertical-align:middle}
/* 健康保険組合一覧：組合名は一定の幅で省略表示（全文は title で確認できる） */
#kl-table td.kl-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:left}
#kl-table th:nth-child(4){text-align:left;max-width:240px}
#kl-table td.kl-name{max-width:240px}
/* 社内LANで Google Fonts に到達できない場合のフォールバック */
*{font-family:"Noto Sans JP","Yu Gothic UI","Yu Gothic","Hiragino Kaku Gothic ProN",Meiryo,"MS PGothic",sans-serif}
</style>
</head>'''

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

SYS_JS = '''
/* ============ 本システム（サーバ連携画面）の読み込み ============ */
const SYS = {
  /* iframe 内が自前の見出しを持つ場合は、シェル側の見出しと説明を隠す
     （一覧＝シェル側の見出し／詳細画面＝画面側の見出し の一方だけを表示する） */
  setHeader(frameEl, hasOwnHeading){
    const host = frameEl && frameEl.closest ? frameEl.closest('.view') : null;
    if(!host) return;
    const head = host.querySelector('.sys-head');
    const note = host.querySelector('.sys-note');
    if(head){
      const t = head.querySelector('h1');
      const f = head.querySelector('.sys-flag');
      const bk = head.querySelector('.back-btn');
      if(t) t.style.display = hasOwnHeading ? 'none' : '';
      if(f) f.style.display = hasOwnHeading ? 'none' : '';
      if(bk) bk.style.display = hasOwnHeading ? 'none' : '';
      head.classList.toggle('floating', !!hasOwnHeading);
    }
    if(note) note.style.display = hasOwnHeading ? 'none' : '';
  },
  open(key){
    const v = document.getElementById(VIEWS[key]);
    if(!v) return;
    const f = v.querySelector('iframe.sys-frame');
    if(f && !f.getAttribute('src')){ f.setAttribute('src', f.dataset.src); }
    /* 高さを固定する画面は、表示のたびに画面の高さへ合わせ直す */
    setTimeout(() => {
      try{
        if(f && f.contentDocument
           && f.contentDocument.body.classList.contains('fixed-h')) SYS.fitHeight(f);
      }catch(e){}
    }, 60);
  },
  resize(frameEl, h){ if(frameEl && h){ frameEl.style.height = (h + 24) + 'px'; } },
  /* 画面いっぱいに固定する（一覧のように中身だけスクロールさせたい画面向け） */
  fitHeight(frameEl){
    if(!frameEl) return;
    window.scrollTo(0, 0);                       /* 画面内に収めるため先頭へ */
    const top = frameEl.getBoundingClientRect().top;
    const h = Math.max(320, Math.round(window.innerHeight - top - 28));
    frameEl.style.height = h + 'px';
    SYS._fitTarget = frameEl;
  }
};
window.SYS = SYS;
window.addEventListener('resize', function(){
  if(SYS._fitTarget && SYS._fitTarget.isConnected) SYS.fitHeight(SYS._fitTarget);
});
'''

KENPO_ACTIONS = '''<span id="ke-db-msg" class="sys-note"
              style="margin:0 12px 0 0;text-align:left;max-width:520px;"></span>
        <button class="btn-cancel" onclick="KenpoDB.goList()">登録をやめて一覧を見る</button>
        <button class="btn-submit-page" onclick="KenpoDB.save(this)">登録する</button>'''

KENPO_DB_JS = '''
/* ============ 健康保険組合の登録（データベース連携） ============ */
const KenpoDB = {
  msg(text, ok){
    const el = document.getElementById('ke-db-msg');
    if(!el) return;
    el.textContent = text || '';
    el.style.color = ok ? '#15803d' : '#e53e3e';
  },
  save(btn){
    const no = (document.getElementById('ke-no') || {}).value || '';
    const nm = (document.getElementById('ke-name') || {}).value || '';
    this.msg('', true);
    btn.disabled = true;
    fetch('/api/kenpos', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: no, name: nm})
    }).then(r => r.json()).then(d => {
      btn.disabled = false;
      this.msg(d.message, !!d.ok);
      if(d.ok){
        document.getElementById('ke-no').value = '';
        document.getElementById('ke-name').value = '';
      }
    }).catch(() => {
      btn.disabled = false;
      this.msg('登録できませんでした。通信状態を確認してください。', false);
    });
  },
  goList(){ navigate('kenpo-list-table'); },
  /* 保険者番号・名称で絞り込む（データベースの内容に対して行う） */
  filter(){
    const no = ((document.getElementById('kl-no') || {}).value || '').trim().toLowerCase();
    const nm = ((document.getElementById('kl-name') || {}).value || '').trim().toLowerCase();
    const rows = [].slice.call(document.querySelectorAll('#kl-table tbody tr'));
    let n = 0;
    rows.forEach(function(r){
      if(!r.dataset.code && !r.dataset.name) return;
      const ok = (!no || r.dataset.code.toLowerCase().indexOf(no) >= 0)
              && (!nm || r.dataset.name.toLowerCase().indexOf(nm) >= 0);
      r.style.display = ok ? '' : 'none';
      if(ok){ n++; r.querySelector('.kl-no').textContent = n; }
    });
    const cnt = document.getElementById('kl-count');
    if(cnt) cnt.textContent = '件数: ' + n;
  },
  /* 健康保険組合一覧をデータベースの内容で表示する */
  fillList(){
    const tb = document.querySelector('#kl-table tbody');
    if(!tb) return;
    fetch('/api/kenpos', {credentials: 'same-origin'})
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if(!d || !d.rows) return;
        tb.innerHTML = '';
        d.rows.forEach((row, i) => {
          const tr = document.createElement('tr');
          tr.dataset.id = row.id;
          tr.dataset.code = row.code;
          tr.dataset.name = row.name;
          tr.innerHTML =
            '<td class="kl-no">' + (i + 1) + '</td>' +
            '<td>' + (row.created_at || '').slice(0, 10) + '</td>' +
            '<td>' + esc(row.code) + '</td>' +
            '<td class="kl-name" title="' + esc(row.name) + '">' + esc(row.name) + '</td>' +
            '<td>' + row.n_company + '</td>' +
            '<td>' + (row.n_hospital || 0) + '</td>';
          /* トグル4つ（公開・健診代行・保健指導・インフル）*/
          tr.appendChild(klToggle('pub', row.id, 'publish_auth', row.publish_auth));
          tr.appendChild(klToggle('ken', row.id, 'kenshin_auth', row.kenshin_auth));
          tr.appendChild(klToggle('gui', row.id, 'guidance_auth', row.guidance_auth));
          tr.appendChild(klToggle('flu', row.id, 'flu_enabled', row.flu_enabled));
          const opTd = document.createElement('td');
          opTd.className = 'op-cell';
          opTd.innerHTML =
            '<button class="tbl-btn kl-support" title="この健保の画面に入ってサポートします"' +
            ' onclick="supportLogin(' + row.id + ')">サポートログイン</button>' +
            '<button class="tbl-btn tbl-btn-edit" data-go="kenpo-edit">編集</button>';
          tr.appendChild(opTd);
          tb.appendChild(tr);
        });
        const cnt = document.getElementById('kl-count');
        if(cnt) cnt.textContent = '件数: ' + d.rows.length;
        if(!tb.dataset.bound){
          tb.dataset.bound = '1';
          tb.addEventListener('click', function(e){
            const b = e.target.closest('[data-go]');
            if(b) navigate(b.dataset.go);
          });
        }
      }).catch(() => {});
  }
};
window.KenpoDB = KenpoDB;

/* 健康保険組合一覧のトグル（公開・健診代行・保健指導・インフル）*/
function klToggle(pfx, id, key, on){
  const eid = 'kl-' + pfx + '-' + id;
  const box = document.createElement('div');
  box.className = 'toggle-wrap';
  const inp = document.createElement('input');
  inp.type = 'checkbox';
  inp.id = eid;
  inp.checked = !!Number(on);
  inp.addEventListener('change', function(){ klSetFlag(id, key, inp); });
  const lab = document.createElement('label');
  lab.setAttribute('for', eid);
  const th = document.createElement('span');
  th.className = 'thumb';
  lab.appendChild(th);
  box.appendChild(inp);
  box.appendChild(lab);
  const td = document.createElement('td');
  td.appendChild(box);
  return td;
}
function klSetFlag(id, key, el){
  const before = !el.checked;
  fetch('/api/kenpos/' + id + '/flag', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: key, value: el.checked ? 1 : 0})})
    .then(r => r.json())
    .then(d => {
      if(!d || !d.ok){
        el.checked = before;
        alert((d && d.message) || '変更できませんでした。');
      }
    })
    .catch(() => { el.checked = before; alert('通信できませんでした。'); });
}
window.klToggle = klToggle;
window.klSetFlag = klSetFlag;

/* 健康保険組合のサポートログイン（当社スタッフのみ）*/
function supportLogin(kenpoId){
  const tr = [...document.querySelectorAll('#kl-table tbody tr')]
    .find(r => r.dataset.id === String(kenpoId));
  const name = tr ? tr.dataset.name : '';
  const msg = ['この健康保険組合の画面に入ります。よろしいですか？', ''].concat(
    name ? ['対象：' + name] : [],
    ['・サポート対応のための機能です',
     '・操作はすべて記録されます',
     '・終了するまで当社の画面には戻れません']).join(String.fromCharCode(10));
  if(!confirm(msg)) return;
  const f = document.createElement('form');
  f.method = 'post'; f.action = '/support/start';
  const i = document.createElement('input');
  i.type = 'hidden'; i.name = 'kenpo_id'; i.value = kenpoId;
  f.appendChild(i); document.body.appendChild(f); f.submit();
}
window.supportLogin = supportLogin;
'''

CARD_ACCOUNT = '''        <div class="top-card" onclick="navigate('sys-account')">
          <div class="top-card-icon">
            <svg width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/><path d="M17 11l2 2 4-4"/></svg>
          </div>
          <div class="top-card-title">アカウント管理<span class="sys-flag" style="margin-left:8px">サーバ連携</span></div>
          <div class="top-card-desc">アカウントの一覧確認・検索、代表者アカウントの発行、権限の登録・編集・削除</div>
        </div>
        <div class="top-card" onclick="navigate('sys-log')">
          <div class="top-card-icon">
            <svg width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M9 13h6M9 17h4"/></svg>
          </div>
          <div class="top-card-title">操作ログ管理<span class="sys-flag" style="margin-left:8px">サーバ連携</span></div>
          <div class="top-card-desc">HIA健保管理での操作も含めた全ログを横断して確認・出力</div>
        </div>
'''


def view(vid, title, desc, path, back='settings'):
    return f'''
    <!-- ======== 本システム: {title} ======== -->
    <div class="view" id="{vid}">
      <div class="sys-head">
        <h1>{title}</h1>
        <span class="sys-flag">サーバ連携</span>
        <button class="back-btn" onclick="navigate('{back}')">← 戻る</button>
      </div>
      <p class="sys-note">{desc}</p>
      <iframe class="sys-frame" data-src="{path}" title="{title}"></iframe>
    </div>
'''


def build():
    s = io.open(SRC, encoding="utf-8").read()
    orig = len(s)

    # 1) スタイル
    s = s.replace("</head>", HEAD_CSS, 1)

    # 2) VIEWS に追加ビューを登録し、統合したモック（account-list）を外す
    s = s.replace("""  'chat-2fa':'view-chat-2fa',
  'chat-yoroku':'view-chat-yoroku'
};""",
                  """  'chat-2fa':'view-chat-2fa',
  'chat-yoroku':'view-chat-yoroku',
  'sys-account':'view-sys-account',
  'sys-log':'view-sys-log'
};""", 1)
    s = s.replace("  'account-list':'view-account-list',\n", "")

    # 3) navigate：サイドバー連動と遅延ロード
    s = s.replace(
        """  if(key==='settings'||key==='mail-setting'||key==='pw-change'||key==='account-list'){ document.querySelector('[data-nav="settings"]').classList.add('active'); }""",
        """  if(key==='settings'||key==='mail-setting'||key==='pw-change'||key==='sys-account'||key==='sys-log'){ document.querySelector('[data-nav="settings"]').classList.add('active'); }
  if(key==='kenpo-list-table'){ KenpoDB.fillList(); }
  if(key.indexOf('sys-')===0){ SYS.open(key); }""", 1)
    s = s.replace("  if(key==='account-list') AC.render();\n", "")

    # 4) サイドバーのクリック処理（原本では未実装）
    s = s.replace("""document.addEventListener('DOMContentLoaded',()=>{
  navigate('kenpo-list');
  
});""",
                  """document.addEventListener('DOMContentLoaded',()=>{

  /* サイドバーのクリックで各セクションへ移動する（原本では未実装） */
  document.querySelectorAll('.sidebar>ul>li[data-nav]').forEach(li=>{
    li.addEventListener('click',()=>{
      const key = li.getAttribute('data-nav');
      if(key && VIEWS[key]){ navHistory.length = 0; navigate(key); }
    });
  });

  navigate('kenpo-list');
});""", 1)

    # 5) モック画面「アカウント設定」を削除（アカウント管理に統合）
    i = s.index('<!-- ========== アカウント一覧 ========== -->')
    j = s.index('<!-- ========== ', i + 10)
    s = s[:i] + s[j:]

    # 6) AC設定トップ：モックのカードを外し、追加カードを先頭に置く
    i = s.index('<div class="view" id="view-settings">')
    end = s.index('<!-- ========== ', i)
    block = s[i:end]
    k = block.index("""<div class="top-card" onclick="navigate('account-list')">""")
    m = block.index("</div>\n        </div>", k) + len("</div>\n        </div>")
    block = block[:k] + block[m:]
    # 先頭のカードの直前に挿入する
    anchor = re.search(r'\n( *)<div class="top-card" onclick=', block)
    block = block[:anchor.start()] + "\n" + CARD_ACCOUNT.rstrip("\n") + block[anchor.start():]
    s = s[:i] + block + s[end:]

    # 6.4) 健康保険組合一覧の検索を、データベースの内容に対して行う
    s = s.replace('id="kl-no" oninput="KL.filter()"', 'id="kl-no" oninput="KenpoDB.filter()"')
    s = s.replace('id="kl-name" oninput="KL.filter()"', 'id="kl-name" oninput="KenpoDB.filter()"')

    # 6.5) 基本設定タブの「更新」を、データベースへ登録するボタンに置き換える
    old = '<button class="btn-submit-page" onclick="alert(\'更新しました。\')">更新</button>'
    assert old in s, "基本設定タブの更新ボタンが見つかりません"
    s = s.replace(old, KENPO_ACTIONS, 1)

    # 7) 追加ビュー本体
    new = (view('view-sys-judge-group', '判定グループ',
                '複合した検査の値から、まとめて判定するルールを管理します。'
                '例：血糖＝空腹時血糖＋HbA1c のうち重い判定を採用。'
                '検査マスタの検査項目に対応します。', '/risk/groups', back='master')
           + view('view-sys-account', 'アカウント管理',
                '各健保・企業の代表者アカウントを発行し、権限の登録・編集・削除と一覧確認を行います。',
                '/accounts')
           + view('view-sys-log', '操作ログ管理',
                  'HIA総合管理とHIA健保管理の操作ログを1つのデータベースに集約しています。',
                  '/logs'))
    k = s.index('<!-- ========== メール設定 ========== -->')
    s = s[:k] + new + "\n    " + s[k:]

    # 8) SYS モジュールと健保登録の処理
    m = re.search(r'\nfunction navigate\(key\)\{', s)
    s = s[:m.start()] + "\n" + SYS_JS + KENPO_DB_JS + s[m.start():]

    # 9) 健康保険組合一覧：組合名を固定幅にして他の列を広げる
    s = s.replace(
        '<div class="tbl-scroll"><table class="fee-table" id="kl-table" style="min-width:1320px;">',
        '<div class="tbl-scroll"><table class="fee-table" id="kl-table"'
        ' style="min-width:1536px;table-layout:fixed;width:100%;">')
    s = s.replace(
        '<th style="width:40px;">No</th><th style="width:100px;">登録日</th>'
        '<th style="width:100px;">保険者番号</th><th>健康保険組合名</th>',
        '<th style="width:48px;">No</th><th style="width:120px;">登録日</th>'
        '<th style="width:120px;">保険者番号</th><th style="width:200px;">健康保険組合名</th>')
    s = s.replace(
        '<th style="width:90px;">登録企業数</th><th style="width:120px;">登録医療機関数</th>'
        '<th style="width:90px;">公開権限</th><th style="width:110px;">健診代行権限</th>'
        '<th style="width:110px;">保健指導権限</th><th style="width:100px;">インフル機能</th>'
        '<th style="width:80px;">UL</th><th style="width:200px;">操作</th>',
        '<th style="width:110px;">登録企業数</th><th style="width:140px;">登録医療機関数</th>'
        '<th style="width:108px;">公開権限</th><th style="width:130px;">健診代行権限</th>'
        '<th style="width:130px;">保健指導権限</th><th style="width:120px;">インフル機能</th>'
        '<th style="width:90px;">UL</th><th style="width:220px;">操作</th>')
    s = s.replace('        <td>${esc(row.insurerName)}</td><td>${row.companyCount}</td>',
                  '        <td class="kl-name" title="${esc(row.insurerName)}">'
                  '${esc(row.insurerName)}</td><td>${row.companyCount}</td>')

    # 10) ヘッダーのログアウトを有効化し、ログイン情報を差し込む
    s = s.replace('<a href="#" class="header-logout" onclick="return false;">',
                  '<a href="/logout" class="header-logout">')
    s = s.replace('<a class="header-logout" href="#" onclick="return false;">',
                  '<a class="header-logout" href="/logout">')
    s = s.replace("</body>", HEADER_JS + "</body>", 1)

    # ---- サイドバーの文字を太くして読みやすくする ----
    s = s.replace(
        ".sb-label{font-size:11px;font-weight:400;",
        ".sb-label{font-size:11.5px;font-weight:600;")

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

    # ---- 医療機関手数料設定：健康保険組合一覧の列幅を整える ----
    # 「インフル機能」が90pxに収まらず見出しが詰まっていたため広げ、
    # 組合名は左寄せにして読みやすくする。
    s = s.replace(
        '''                <th style="width:40px;">No</th>
                <th style="width:80px;">保険者番号</th>
                <th>健康保険組合名</th>
                <th style="width:90px;">健診代行</th>
                <th style="width:200px;">健診期間</th>
                <th style="width:90px;">インフル機能</th>
                <th style="width:200px;">接種対象期間</th>
                <th style="width:100px;">操作</th>''',
        '''                <th style="width:48px;">No</th>
                <th style="width:104px;">保険者番号</th>
                <th class="tl">健康保険組合名</th>
                <th style="width:104px;">健診代行</th>
                <th style="width:196px;">健診期間</th>
                <th style="width:116px;">インフル機能</th>
                <th style="width:196px;">接種対象期間</th>
                <th style="width:104px;">操作</th>''')

    # ナレッジ一覧の「ドキュメント数」も80pxに収まらないため広げる
    s = s.replace('<th style="width:80px;">ドキュメント数</th>',
                  '<th style="width:124px;">ドキュメント数</th>')

    # 健康保険組合一覧が枠をはみ出していたため、サポートログインの列を含めて幅を見直す
    s = s.replace("#kl-table th:nth-child(4){text-align:left;max-width:240px}",
                  "#kl-table th:nth-child(4){text-align:left;max-width:220px}")
    s = s.replace("#kl-table td.kl-name{max-width:240px}",
                  "#kl-table td.kl-name{max-width:220px}\n"
                  "/* 健康保険組合一覧は1行に収める */\n"
                  "#kl-table th:last-child,#kl-table td:last-child{width:206px}\n"
                  "#kl-table td:last-child{white-space:nowrap;padding:6px 8px}\n"
                  "#kl-table td.op-cell .tbl-btn{width:auto;margin:0 2px;padding:0 10px;\n"
                  "  height:30px;min-height:30px;font-size:11.5px}\n"
                  "#kl-table td.op-cell .kl-support{background:#2f4f4f;color:#fff;\n"
                  "  border-color:#2f4f4f}\n"
                  "#kl-table tbody td{height:44px;padding-top:4px;padding-bottom:4px}\n"
                  "#kl-table .toggle-wrap{display:inline-flex}\n"
                  "#kl-table{table-layout:fixed;width:100%}")

    # 健保一覧は列が12あり min-width で横スクロールが出ていたため、
    # 幅の広い列を詰めて枠に収める。
    s = s.replace('id="kl-table" style="min-width:1536px;table-layout:fixed;width:100%;"',
                  'id="kl-table" style="table-layout:fixed;width:100%;"')
    for a, c in (('<th style="width:120px;">登録日</th>', '<th style="width:104px;">登録日</th>'),
                 ('<th style="width:120px;">保険者番号</th>', '<th style="width:104px;">保険者番号</th>'),
                 ('<th style="width:110px;">登録企業数</th>', '<th style="width:96px;">登録企業数</th>'),
                 ('<th style="width:140px;">登録医療機関数</th>', '<th style="width:126px;">登録医療機関数</th>'),
                 ('<th style="width:108px;">公開権限</th>', '<th style="width:92px;">公開権限</th>'),
                 ('<th style="width:130px;">健診代行権限</th>', '<th style="width:100px;">健診代行権限</th>'),
                 ('<th style="width:130px;">保健指導権限</th>', '<th style="width:100px;">保健指導権限</th>'),
                 ('<th style="width:120px;">インフル機能</th>', '<th style="width:100px;">インフル機能</th>'),
                 ('<th style="width:200px;">健康保険組合名</th>', '<th style="width:170px;">健康保険組合名</th>'),
                 ('<th style="width:90px;">UL</th>', '<th style="width:74px;">UL</th>'),
                 ('<th style="width:220px;">操作</th>', '<th style="width:206px;">操作</th>')):
        s = s.replace(a, c)

    # 組合名のセルを左寄せにする（行はJSで作るため、CSSで指定する）
    s = s.replace("</style>", """
/* 医療機関手数料設定：健康保険組合名は左寄せにする */
#fs-kenpo-table th.tl,
#fs-kenpo-table tbody td:nth-child(3){text-align:left;padding-left:16px}
#fs-kenpo-table tbody td:nth-child(3){overflow:hidden;text-overflow:ellipsis}
</style>""", 1)

    # ---- 判定グループ（マスタ管理に置く。検査マスタの検査項目に対応）----
    if "'sys-judge-group':'view-sys-judge-group'" not in s:
        s = s.replace("  'master-criteria':'view-master-criteria',",
                      "  'master-criteria':'view-master-criteria',\n"
                      "  'sys-judge-group':'view-sys-judge-group',", 1)
    if "key==='sys-judge-group'" not in s:
        s = s.replace("||key==='master-oid'){ document.querySelector('[data-nav=\"master\"]')",
                      "||key==='master-oid'||key==='sys-judge-group'){"
                      " document.querySelector('[data-nav=\"master\"]')", 1)
    if "navigate('sys-judge-group')" not in s:
        k = s.find("<div class=\"top-card\" onclick=\"navigate('master-criteria')\">")
        if k >= 0:
            d = s.find("top-card-desc", k)
            e = s.find("</div>", d)
            e = s.find("</div>", e + 6)
            card = (
                '<div class="top-card" onclick="navigate(\'sys-judge-group\')">\n'
                '          <div class="top-card-icon">\n'
                '            <svg width="22" height="22" fill="none" stroke="currentColor"'
                ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'
                ' viewBox="0 0 24 24"><rect x="3" y="4" width="7" height="7" rx="1.5"/>'
                '<rect x="14" y="4" width="7" height="7" rx="1.5"/>'
                '<rect x="8.5" y="14" width="7" height="6" rx="1.5"/>'
                '<path d="M6.5 11v1.5h11V11"/><path d="M12 12.5V14"/></svg>\n'
                '          </div>\n'
                '          <div class="top-card-title">判定グループ</div>\n'
                '          <div class="top-card-desc">複合した検査（空腹時血糖＋HbA1c など）を'
                'まとめて判定するルールの管理</div>\n'
                '        </div>')
            s = s[:e + 6] + "\n        " + card + s[e + 6:]

    # ---- OID結果マスタ ----
    # 新しい原本（HIA_integrated.html）には OID結果マスタ が含まれているため、
    # 足りない配線だけを補う（原本に無い場合は parts/oid から組み込む）。
    if 'id="view-master-oid"' in s:
        # VIEWS への登録が無ければ足す
        if "'master-oid':'view-master-oid'" not in s:
            s = s.replace("  'master-criteria':'view-master-criteria',",
                          "  'master-criteria':'view-master-criteria',\n"
                          "  'master-oid':'view-master-oid',", 1)
        # サイドバーの「マスタ管理」を選択状態にする条件
        if "key==='master-oid'){" not in s and "||key==='master-oid'" not in s:
            s = s.replace("||key==='master-year'){ document.querySelector('[data-nav=\"master\"]')",
                          "||key==='master-year'||key==='master-oid'){"
                          " document.querySelector('[data-nav=\"master\"]')", 1)
        # 表示時の初期化
        if "if(key==='master-oid') OID.render();" not in s:
            for anchor in ("  if(key==='master-year') MS.renderYearMaster();",
                           "  if(key==='master-criteria') MS.render();"):
                if anchor in s:
                    s = s.replace(anchor, anchor + "\n  if(key==='master-oid') OID.render();", 1)
                    break
        # ① 他の画面と同じ「条件検索」のカードを一覧の上に追加する
        old_head = ('<div style="display:flex;justify-content:space-between;'
                    'align-items:center;margin-bottom:12px;gap:12px;flex-wrap:wrap;">')
        k = s.find('id="view-master-oid"')
        h = s.find(old_head, k)
        if h >= 0 and "oid-search-card" not in s:
            search = (
                '<div class="right-panel no-flex oid-search-card">\n'
                '          <h2>条件検索</h2>\n'
                '          <div class="search-row">\n'
                '            <div class="field"><label>OIDコード</label>'
                '<input type="text" id="oid-f-code" placeholder="部分一致"'
                ' oninput="OID.render()"></div>\n'
                '            <div class="field wide"><label>名称</label>'
                '<input type="text" id="oid-f-name" placeholder="部分一致"'
                ' oninput="OID.render()"></div>\n'
                '            <div class="field"><label>分類</label>\n'
                '              <select id="oid-f-cat" onchange="OID.render()">\n'
                '                <option value="">すべて</option>\n'
                '                <option value="基本情報">基本情報</option>\n'
                '                <option value="報告">報告関連</option>\n'
                '                <option value="区分">区分</option>\n'
                '                <option value="検査">検査項目</option>\n'
                '                <option value="判定">判定・結果</option>\n'
                '                <option value="問診">問診</option>\n'
                '                <option value="保健指導">保健指導</option>\n'
                '                <option value="後期">後期高齢者</option>\n'
                '                <option value="その他">その他</option>\n'
                '              </select>\n'
                '            </div>\n'
                '            <div class="field"><label>値・説明／備考</label>'
                '<input type="text" id="oid-f-kw" placeholder="本文を検索"'
                ' oninput="OID.render()"></div>\n'
                '          </div>\n'
                '        </div>\n'
                '        <div class="right-panel no-flex">\n        ')
            # 一覧パネルの前に検索カードを差し込む
            panel = s.rfind('<div class="right-panel no-flex">', k, h)
            s = s[:panel] + search + s[panel + len('<div class="right-panel no-flex">'):]

        # 一覧の上にあった絞り込み（分類・キーワード）は検索カードへ移したので隠す
        s = s.replace('<select id="oid-filter-cat" onchange="OID.render()"',
                      '<select id="oid-filter-cat" onchange="OID.render()" hidden', 1)
        s = s.replace('<input type="text" id="oid-filter-kw" oninput="OID.render()"',
                      '<input type="text" id="oid-filter-kw" oninput="OID.render()" hidden', 1)

        # ② 検索カードの入力も絞り込みに使う
        s = s.replace(
            """  function getFiltered(){
    const cat = document.getElementById('oid-filter-cat')?.value || '';
    const kw = (document.getElementById('oid-filter-kw')?.value || '').toLowerCase();
    return data.filter(r => {
      if(cat && r.cat !== cat) return false;
      if(kw){
        return r.code.toLowerCase().includes(kw) || r.name.toLowerCase().includes(kw);
      }
      return true;
    });
  }""",
            """  function getFiltered(){
    // 条件検索のカード（無い場合は一覧上部の絞り込み）を見る
    const v = (id) => (document.getElementById(id)?.value || '').trim().toLowerCase();
    const cat = document.getElementById('oid-f-cat')?.value
             || document.getElementById('oid-filter-cat')?.value || '';
    const fCode = v('oid-f-code');
    const fName = v('oid-f-name');
    const fKw   = v('oid-f-kw');
    const kw    = v('oid-filter-kw');
    return data.filter(r => {
      const code = String(r.code || '').toLowerCase();
      const name = String(r.name || '').toLowerCase();
      const body = (String(r.values || '') + String(r.note || '')).toLowerCase();
      if(cat && r.cat !== cat) return false;
      if(fCode && !code.includes(fCode)) return false;
      if(fName && !name.includes(fName)) return false;
      if(fKw && !body.includes(fKw)) return false;
      if(kw && !(code.includes(kw) || name.includes(kw))) return false;
      return true;
    });
  }""", 1)

        # 省略した文の全文は、マウスを乗せて確認できるようにする
        s = s.replace(
            '\'<td class="tbl-left" style="font-size:12px;color:#334155;line-height:1.5;">\'+esc(r.values||\'—\')+\'</td>\'',
            '\'<td class="tbl-left" style="font-size:12px;color:#334155;" title="\'+esc(r.values||\'\')+\'">\'+esc(r.values||\'—\')+\'</td>\'', 1)
        s = s.replace(
            '\'<td class="tbl-left" style="font-size:11px;color:#64748b;line-height:1.5;">\'+esc(r.note||\'—\')+\'</td>\'',
            '\'<td class="tbl-left" style="font-size:11px;color:#64748b;" title="\'+esc(r.note||\'\')+\'">\'+esc(r.note||\'—\')+\'</td>\'', 1)

        # ③ 値・説明が長い場合は「…」で省略し、全文はマウスを乗せて確認する
        s = s.replace("#view-master-oid table{table-layout:fixed}", "")
        s = s.replace("</style>", """
/* OID結果マスタ：長い説明は…で省略し、全文は title で見せる */
#view-master-oid table{table-layout:fixed}
#view-master-oid td{vertical-align:middle}
#view-master-oid td:nth-child(3){font-family:'Inter',Consolas,monospace}
#view-master-oid td:nth-child(4),
#view-master-oid td:nth-child(5),
#view-master-oid td:nth-child(6){overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;max-width:0}
#view-master-oid td:last-child{white-space:nowrap}
</style>""", 1)

    else:
        oid_dir = os.path.join(BASE, "parts", "oid")
        if os.path.isdir(oid_dir):
            oid_card = io.open(os.path.join(oid_dir, "card.html"), encoding="utf-8").read()
            oid_view = io.open(os.path.join(oid_dir, "view.html"), encoding="utf-8").read()
            oid_js = io.open(os.path.join(oid_dir, "oid.js"), encoding="utf-8").read()
            k = s.find('<div class="top-card" onclick="navigate(\'master-criteria\')">')
            if k >= 0:
                d = s.find("top-card-desc", k)
                e = s.find("</div>", d)
                e = s.find("</div>", e + 6)
                s = s[:e + 6] + "\n        " + oid_card.strip() + s[e + 6:]
            mark = '<div class="view" id="view-master-criteria">'
            k = s.find(mark)
            if k >= 0:
                nxt = s.find('<div class="view"', k + 20)
                s = s[:nxt] + oid_view.strip() + "\n\n    " + s[nxt:]
            s = s.replace("  'master-criteria':'view-master-criteria',",
                          "  'master-criteria':'view-master-criteria',\n"
                          "  'master-oid':'view-master-oid',", 1)
            s = s.replace("  if(key==='master-criteria') MS.render();",
                          "  if(key==='master-criteria') MS.render();\n"
                          "  if(key==='master-oid') OID.render();", 1)
            k = s.find("const MS=(()=>{")
            if k < 0:
                k = s.rfind("</script>")
            s = s[:k] + oid_js.strip() + "\n\n" + s[k:]

    # ---- 判定マスタ編集：列幅を整え、入力欄の高さを40pxにそろえる ----
    s = s.replace(
        '''              <th style="width:40px;">No</th>
              <th style="width:180px;">判定区分</th>
              <th style="width:80px;">性別</th>
              <th>下限値(以上)</th>
              <th>上限値(未満)</th>
              <th style="width:70px;">操作</th>''',
        '''              <th style="width:56px;">No</th>
              <th style="width:240px;">判定区分</th>
              <th style="width:120px;">性別</th>
              <th style="width:190px;">下限値(以上)</th>
              <th style="width:190px;">上限値(未満)</th>
              <th style="width:96px;">操作</th>''')
    # 判定基準の表はインラインの width:100% を外す（値の欄が広がりすぎるため）
    k = s.find('id="view-master-judgment-edit"')
    if k >= 0:
        m = s.find('<table class="fee-table" style="width:100%;">', k)
        if m >= 0:
            s = s[:m] + '<table class="fee-table ms-crit-table">' + \
                s[m + len('<table class="fee-table" style="width:100%;">'):]

    # 原本の 32px 固定指定を外す
    s = s.replace(
        "#ms-codesWrap .form-control,#ms-critWrap .form-control{height:32px;padding:4px 8px;background:#fff;}",
        "#ms-codesWrap .form-control,#ms-critWrap .form-control{padding:0 10px;background:#fff;}")

    # ---- 入力欄・ボタンの高さを全画面で40pxにそろえる ----
    s = s.replace("</style>", """
/* 入力欄・プルダウンの高さを全画面で40pxにそろえる */
input[type="text"], input[type="number"], input[type="password"],
input[type="date"], input[type="month"], input[type="time"],
input[type="email"], input[type="tel"], input[type="search"], select,
.form-control {
  height: 40px !important;
  min-height: 40px !important;
  box-sizing: border-box;
}
textarea { min-height: 40px; box-sizing: border-box; }
/* ボタンも全画面で40pxにそろえる（表の中の小さなボタンを含む）*/
button, .tbl-btn, .back-btn, .btn-save, .btn-submit-page, .btn-cancel-page,
.top-card button, .modal-back button {
  height: 40px !important;
  min-height: 40px !important;
  padding-top: 0 !important;
  padding-bottom: 0 !important;
  box-sizing: border-box;
}
/* トグルは高さを変えない */
.toggle-wrap label { height: 22px !important; min-height: 22px !important }
/* 丸いページ送りは正円のまま */
.pager-btn { width: 40px !important; height: 40px !important; padding: 0 !important }
/* 判定マスタ編集：値の欄は右寄せ、行の余白を詰める */
#ms-critWrap td { padding: 6px 10px; height: 52px }
#ms-critWrap input[type="number"], #ms-critWrap input[type="text"] { text-align: right }
/* 判定マスタ編集：表を内容の幅にとどめる（値の欄が広がりすぎないように）*/
.ms-crit-table { width: 900px; max-width: 100%; table-layout: fixed }
</style>""", 1)

    # ---- サイドバーの横幅を96pxにする（本文の左余白もあわせる）----
    s = s.replace("left:0;width:88px;height:calc(100vh - 52px);",
                  "left:0;width:96px;height:calc(100vh - 52px);")
    s = s.replace("body{background:#f7f8f9;color:#333;margin-left:88px;}",
                  "body{background:#f7f8f9;color:#333;margin-left:96px;}")

    io.open(OUT, "w", encoding="utf-8").write(s)
    return orig, len(s), s



if __name__ == "__main__":
    a, b, s = build()
    print(f"生成: {OUT}")
    print(f"  {a:,} → {b:,} 文字")
    old = set(re.findall(r'id="(view-[a-z0-9\-]+)"',
                         io.open(SRC, encoding="utf-8").read()))
    new = set(re.findall(r'id="(view-[a-z0-9\-]+)"', s))
    print("  削除したビュー:", sorted(old - new) or "なし")
    print("  追加したビュー:", sorted(new - old))
    i = s.index('<div class="view" id="view-settings">')
    seg = s[i:s.index('<!-- ========== ', i)]
    print("  AC設定のカード順:", re.findall(r'<div class="top-card-title">([^<]*)', seg))
    print("  top-cards の囲み:", 'class="top-cards theme-settings"' in seg)
    print("  account-list の残存:", s.count("account-list"))
