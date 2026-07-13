// 右クリック→「このサイトをクローン」→ 86ツール(127.0.0.1:5000)へURLを渡してクローン開始。
// 完成したら /camp/<ファイル名> を新しいタブで開いてアクティブにする。
// ⚠ツールのサーバー（起動.bat）が動いていないと失敗します（バッジに×が出る）。
const API = "http://127.0.0.1:5000";
let opened = false; // 完成タブの二重オープン防止（ポーリングとアラームが同時に完了を見た時用）

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "clone_safe", title: "📋 このサイトをクローン（安全版・JSなし）", contexts: ["page"] });
  chrome.contextMenus.create({ id: "clone_js", title: "🎬 このサイトをクローン（元のJSも残す）", contexts: ["page"] });
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  const url = info.pageUrl || "";
  if (!/^https?:/.test(url)) { badge("×"); return; }
  try {
    const r = await fetch(API + "/api/clone_site", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url, keep_js: info.menuItemId === "clone_js" }),
    });
    const d = await r.json();
    if (!d.ok) { badge("×"); console.log("クローン開始失敗:", d.message); return; }
    opened = false;
    badge("…");
    // service workerは途中で眠ることがあるので、短い連続ポーリング＋30秒ごとのアラームの二段構え
    chrome.alarms.create("clone_poll", { periodInMinutes: 0.5 });
    poll();
  } catch (e) {
    badge("×");
    console.log("ツールのサーバーに接続できません（起動.batで起動していますか？）", e);
  }
});

async function poll() {
  try {
    const d = await fetch(API + "/api/clone_site/status").then((r) => r.json());
    if (d.site_id) { setTimeout(poll, 2000); return; } // まだクローン中
    chrome.alarms.clear("clone_poll");
    if (d.file && !opened) {
      opened = true;
      badge("");
      // 完成したクローンを新しいタブで開いてアクティブにする（ユーザー要望）
      chrome.tabs.create({ url: API + "/camp/" + encodeURIComponent(d.file), active: true });
    } else if (d.error) {
      badge("×");
      console.log("クローン失敗:", d.error);
    }
  } catch (e) {
    setTimeout(poll, 3000);
  }
}

chrome.alarms.onAlarm.addListener((a) => { if (a.name === "clone_poll") poll(); });

function badge(t) { chrome.action.setBadgeText({ text: t }); }
