  hia: {
    title: '疾患予測・受診勧奨',
    subtitle: '病気リスクの確認・受診勧奨対象者の管理・案内文書の送付',
    color: '#38a169', iconBg: '#f0fff4',
    cards: [
      { page:'hia-dashboard', label:'疾患予測',          desc:'疾患別リスク・対象者数の前年比確認',    tag:'リスク管理', tagClass:'tag-view',
        icon:'<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>' },
      { page:'hia-list',      label:'受診勧奨対象者一覧', desc:'判定E対象者の管理・メール送信・CSV出力', tag:'対象者管理', tagClass:'tag-view',
        icon:'<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/>' },
    ]
  }