// デザインストック クリッパー：右クリックメニュー→ツール(127.0.0.1:5000)のAPIへ送る係。
// fetchはこのservice workerから行う（host_permissionsがあるのでCORS不要・httpsページ→httpローカルの制限も受けない）
const API = "http://127.0.0.1:5000";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "save_part",
    title: "🧩 このセクションを部品として保存（＋サイトも登録）",
    contexts: ["all"],
  });
  chrome.contextMenus.create({
    id: "save_site",
    title: "📚 このサイトをストックに登録（スクショ＋ベクトル化）",
    contexts: ["all"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!tab || !tab.id) return;

  if (info.menuItemId === "save_site") {
    const reg = await registerSite(tab.url);
    toast(tab.id, reg.ok, reg.msg);
    return;
  }

  if (info.menuItemId === "save_part") {
    // ①content scriptに「右クリックした部品のHTML+CSSを抜いて」と頼む
    let part;
    try {
      part = await chrome.tabs.sendMessage(tab.id, { type: "extract_part" });
    } catch (e) {
      toast(tab.id, false, "このページでは使えません（ページを再読み込みしてから試してください）");
      return;
    }
    if (!part || !part.ok) {
      if (part && part.msg) toast(tab.id, false, part.msg);
      return; // 名前入力キャンセル等は静かに終わる
    }
    // ②ツールへ部品保存
    let saved;
    try {
      const r = await fetch(API + "/api/section_fav/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ html: part.html, headcss: "", name: part.name, kind: part.kind }),
      });
      saved = await r.json();
    } catch (e) {
      toast(tab.id, false, "ツールに接続できません（常駐サーバーが動いているか確認してください）");
      return;
    }
    if (!saved.ok) {
      toast(tab.id, false, "部品の保存に失敗：" + (saved.message || ""));
      return;
    }
    // ③サイト本体も同時登録（登録済みならスキップ）
    const reg = await registerSite(tab.url);
    toast(tab.id, true, "🧩 部品「" + part.name + "」を保存しました\n" + reg.msg);
  }
});

// サイトをストック登録する（登録済みなら何もしない）。戻り値 {ok, msg}
async function registerSite(url) {
  if (!/^https?:/.test(url || "")) return { ok: false, msg: "このURLはストック登録できません" };
  try {
    const sites = await (await fetch(API + "/api/sites")).json();
    const norm = (u) => String(u || "").replace(/\/+$/, "");
    if ((sites.hits || []).some((h) => norm(h.url) === norm(url))) {
      return { ok: true, msg: "📚 このサイトは登録済みです" };
    }
    const r = await (
      await fetch(API + "/api/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls: url }),
      })
    ).json();
    if (r.ok) return { ok: true, msg: "📚 サイト登録を開始しました（撮影→ベクトル化は裏で進みます）" };
    return { ok: false, msg: "⚠ サイト登録できず：" + (r.message || "別の登録が実行中かも") };
  } catch (e) {
    return { ok: false, msg: "⚠ ツールに接続できません（常駐サーバー未起動かも）" };
  }
}

// ページ内にトースト表示を頼む（通知権限を増やさずに済む）
function toast(tabId, ok, msg) {
  chrome.tabs.sendMessage(tabId, { type: "toast", ok: ok, msg: msg }).catch(() => {});
}
