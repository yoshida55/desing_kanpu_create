// デザインストック クリッパー：ページ側の係。
// ・右クリックした要素を覚えておく
// ・「抜いて」と言われたら、その要素が属する section/header/footer を
//   自己完結HTML（効いているCSSを<style data-cepart>で内蔵・@scopeで部品内だけに効く）にして返す
// ・CSS抽出(partCss)はツール本体(viewer.py)の実装を移植したもの＝ツール内⭐保存と同じ品質
(function () {
  "use strict";

  let lastEl = null;
  document.addEventListener(
    "contextmenu",
    function (e) {
      lastEl = e.target;
    },
    true
  );

  // ===== ツール本体から移植したCSS抽出（partCss） =====
  function partCss(el) {
    // リンク先ページ(iframe内)の要素も扱えるよう、その要素が属するdocument/windowを使う
    const doc = el.ownerDocument || document,
      win = doc.defaultView || window;
    const els = [el].concat([].slice.call(el.querySelectorAll("*")));
    function hitAny(selText) {
      return selText.split(",").some(function (s) {
        // :hover/::before等は保存の瞬間は誰にも当たっていないので、疑似部分を外した本体で判定する
        s = s
          .replace(/::?[a-zA-Z-]+(\((?:[^()]|\([^()]*\))*\))?/g, "")
          .replace(/[>+~\s]+$/, "")
          .trim();
        if (!s) return true; // ::selection など疑似だけのセレクタは念のため持っていく
        for (let i = 0; i < els.length; i++) {
          try {
            if (els[i].matches(s)) return true;
          } catch (_) {}
        }
        return false;
      });
    }
    // @scope内のセレクタは暗黙で「:scopeの子孫」扱い → ルート自身/ルートを先祖に使う形は
    // :scopeへ書き換えたコピーも一緒に持っていく（ツール本体と同じ対策）
    function extraSels(selText) {
      const res = [];
      selText.split(",").forEach(function (s) {
        s = s.trim();
        if (!s) return;
        const m = s.match(/^(.*?)((?:::?[a-zA-Z-]+(?:\([^()]*\))?)*)$/);
        const base = m ? m[1] : s,
          pseudo = m ? m[2] : "";
        const probe = base
          .replace(/::?[a-zA-Z-]+(\((?:[^()]|\([^()]*\))*\))?/g, "")
          .replace(/[>+~\s]+$/, "")
          .trim();
        try {
          if (probe && el.matches(probe)) {
            res.push(":scope" + pseudo);
            return;
          }
        } catch (_) {}
        const lead = s.match(/^([^\s>+~]+)([\s>+~].*)$/);
        if (lead) {
          const lp = lead[1].replace(/::?[a-zA-Z-]+(\((?:[^()]|\([^()]*\))*\))?/g, "").trim();
          try {
            if (lp && el.matches(lp)) res.push(":scope" + lead[2]);
          } catch (_) {}
        }
      });
      return res;
    }
    const out = [],
      kf = [],
      hideSel = [];
    function scan(rules, mediaTxt) {
      [].slice.call(rules || []).forEach(function (r) {
        if (r.media && r.cssRules) {
          scan(r.cssRules, r.media.mediaText);
          return;
        } // @media
        if (r.name && r.cssRules) {
          kf.push(r.cssText);
          return;
        } // @keyframes は丸ごと（@scopeの外に置く）
        if (!r.selectorText) {
          if (r.cssRules) scan(r.cssRules, mediaTxt);
          return;
        } // @scope/@supports等は中身だけ
        if (!r.style) return;
        if (hitAny(r.selectorText)) {
          // ★元サイトがJSで表示する作り（CSSでは opacity:0 で隠す）だと、部品にした時にJSが無く
          //   永久に見えない（実例：インタビューのカードが真っ白）。隠す指定を控えておき、
          //   あとで「保存の瞬間に実際は見えていたもの」だけ表示に戻す。
          try {
            if (parseFloat(r.style.opacity) === 0 || r.style.visibility === "hidden") hideSel.push(r.selectorText);
          } catch (_) {}
          out.push(mediaTxt ? "@media " + mediaTxt + "{" + r.cssText + "}" : r.cssText);
          const rs = extraSels(r.selectorText);
          if (rs.length) {
            const rule = rs.join(",") + "{" + r.style.cssText + "}";
            out.push(mediaTxt ? "@media " + mediaTxt + "{" + rule + "}" : rule);
          }
        }
      });
    }
    [].slice.call(doc.styleSheets).forEach(function (ss) {
      let rr;
      try {
        rr = ss.cssRules; // 他ドメインのCSS(Googleフォント等)は読めないので黙ってスキップ
      } catch (_) {
        return;
      }
      // ★<link media="(max-width:767px)">のようなシートまるごとの幅条件を無視すると、
      //   スマホ専用CSSが無条件で紛れ込む（実例：overflow-x:scrollでPC表示にスクロールバーが出た）。
      //   シート側のmedia条件も@mediaとして引き継ぐ。
      let mt = "";
      try { mt = (ss.media && ss.media.mediaText) || ""; } catch (_) {}
      scan(rr, mt || undefined);
    });
    // 護身用：入れ替え先の「浮く絶対配置」に被されないよう、staticの部品はrelative+z-index:1に
    if (win.getComputedStyle(el).position === "static") out.push(":scope{position:relative;z-index:1}");
    // 隠す指定のうち、保存の瞬間に「実際は見えていた」ものだけ表示に戻す（JS前提の演出対策）
    hideSel.forEach(function (sel) {
      let vis = false;
      for (let i = 0; i < els.length; i++) {
        try {
          if (els[i].matches(sel) && parseFloat(win.getComputedStyle(els[i]).opacity) >= 0.99) { vis = true; break; }
        } catch (_) {}
      }
      if (vis) out.push(sel + "{opacity:1!important;visibility:visible!important}");
    });
    // ★rem対策（ツール本体と同じ）：元サイトが html{font-size:clamp(100vw/1440)} 等で
    //   1rem を 1px 未満に縮めていると、貼り先のカンプ（1rem=16px）で寸法が十数倍に膨張する
    //   （実例：width:300rem が 267px のはずが 4800px になった）。保存時に px へ焼き直す。
    let rootPx = parseFloat(win.getComputedStyle(doc.documentElement).fontSize) || 16;
    if (Math.abs(rootPx - 16) < 0.01) {
      // ページ側で指定が効いていない場合は、CSSに書かれている指定を実際に計算して本来の値を得る
      let pv = "";
      [].slice.call(doc.styleSheets).forEach(function (ss) {
        let rr;
        try { rr = ss.cssRules; } catch (_) { return; }
        [].slice.call(rr || []).forEach(function (r) {
          if (!r.selectorText || !r.style) return;
          if (!/(^|,)\s*(html|:root)\s*(,|$)/.test(r.selectorText)) return;
          const v = r.style.getPropertyValue("font-size");
          if (v) pv = v;
        });
      });
      if (pv) {
        try {
          const p = doc.createElement("div");
          p.style.cssText = "position:absolute;left:-9999px;top:0;visibility:hidden;font-size:" + pv;
          doc.documentElement.appendChild(p);
          const px = parseFloat(win.getComputedStyle(p).fontSize) || 0;
          p.remove();
          if (px > 0 && Math.abs(px - 16) >= 0.01) rootPx = px;
        } catch (_) {}
      }
    }
    let css = kf.join("\n") + "\n@scope{\n" + out.join("\n") + "\n}";
    if (Math.abs(rootPx - 16) >= 0.01) {
      css = css.replace(/(-?\d*\.?\d+)rem\b/g, function (_m, n) {
        return Math.round(parseFloat(n) * rootPx * 1000) / 1000 + "px";
      });
    }
    return css;
  }

  // ===== 部品HTMLの掃除：script除去・画像URLを絶対化（サイトを閉じても表示できるように） =====
  function cleanPart(orig, inclFixed) {
    const doc = orig.ownerDocument || document,
      win = doc.defaultView || window;
    const clone = orig.cloneNode(true);
    // ★浮いている要素(position:absolute)は、元ページの「この部品の外にある祖先」を位置の基準に
    //   していることがある。部品として切り出すとその基準が消え、代わりに部品自身が基準になって
    //   位置が総崩れになる（実例：見出しが別の見出しに重なった）。
    //   保存の瞬間に「部品の左上から見た位置(px)」を実測して焼き込み、どこに貼っても同じ見た目にする。
    try {
      const baseRect = orig.getBoundingClientRect();
      const srcEls = orig.querySelectorAll("*");
      const dstEls = clone.querySelectorAll("*");
      for (let i = 0; i < srcEls.length; i++) {
        const s = srcEls[i], dnode = dstEls[i];
        if (!dnode) continue;
        let cs;
        try { cs = win.getComputedStyle(s); } catch (_) { continue; }
        const fx = cs.position === "fixed";
        if (cs.position !== "absolute" && !(inclFixed && fx)) continue;
        // ★位置の基準（一番近いpositionedな祖先）が部品の「中」にある要素は焼き込まない。
        //   元のCSS座標がそのまま正しく、部品全体基準の座標で上書きすると枠の外へ飛んで消える
        //   （実例：カード写真がfigure基準なのに部品基準のtop/leftを焼かれてoverflow:hiddenの外へ）。
        //   焼き込むのは、基準が部品の「外」にあって切り出すと座標が狂う要素だけ。
        if (!fx) {
          let anc = s.parentElement, refInside = false;
          while (anc) {
            let ap = "static";
            try { ap = win.getComputedStyle(anc).position; } catch (_) {}
            if (ap !== "static") { refInside = orig.contains(anc); break; }
            anc = anc.parentElement;
          }
          if (refInside) continue;
        }
        const r = s.getBoundingClientRect();
        if (!r.width && !r.height) continue;
        // モーダル同梱時：fixed(画面かぶせ)は流し込み先で画面全体を覆ってしまうのでabsoluteに落とす
        if (fx) dnode.style.setProperty("position", "absolute", "important");
        dnode.style.setProperty("top", r.top - baseRect.top + "px", "important");
        dnode.style.setProperty("left", r.left - baseRect.left + "px", "important");
        // ★right/bottom を auto にすると、left+right の組で幅を作っていた要素が潰れて消える
        //   （実例：カードの写真が空っぽの枠になった）。実測した幅・高さも一緒に焼き込む。
        if (r.width) dnode.style.setProperty("width", Math.round(r.width) + "px", "important");
        if (r.height) dnode.style.setProperty("height", Math.round(r.height) + "px", "important");
        dnode.style.setProperty("right", "auto", "important");
        dnode.style.setProperty("bottom", "auto", "important");
      }
    } catch (_) {}
    [].slice.call(clone.querySelectorAll("script,noscript,iframe")).forEach(function (n) {
      n.remove();
    });
    // 画像：実際に表示中のURL(currentSrc)を絶対URLで焼き込み、srcset/lazy属性は外す
    const origImgs = [].slice.call(orig.querySelectorAll("img"));
    const cloneImgs = [].slice.call(clone.querySelectorAll("img"));
    cloneImgs.forEach(function (im, i) {
      const src = (origImgs[i] && (origImgs[i].currentSrc || origImgs[i].src)) || im.src;
      if (src) im.setAttribute("src", src);
      im.removeAttribute("srcset");
      im.removeAttribute("sizes");
      im.removeAttribute("loading");
      im.removeAttribute("data-src");
    });
    // <picture>の<source>はsrcset頼みなので外す（imgのsrcで表示させる）
    [].slice.call(clone.querySelectorAll("picture source")).forEach(function (n) {
      n.remove();
    });
    // リンクは絶対URL化（押しても元サイトに飛べるように）
    const origAs = [].slice.call(orig.querySelectorAll("a"));
    [].slice.call(clone.querySelectorAll("a")).forEach(function (a, i) {
      if (origAs[i] && origAs[i].href) a.setAttribute("href", origAs[i].href);
    });
    // ★色とフォントを自己完結させる（ツール本体と同じ対策）。
    //   元サイトの色は :root の変数、書体や既定の文字色は html/body 側の指定で決まっていることが多い。
    //   部品のCSSは「部品の中の要素に当たるルール」しか持って行かないので、これらを足さないと
    //   貼り先で色が黒・書体が別物になる（実例：緑の見出しが黒＋別フォントになった）。
    try {
      const names = {};
      const re = /(--[\w-]+)\s*:/g;
      [].slice.call(doc.styleSheets).forEach(function (ss) {
        let rr;
        try { rr = ss.cssRules; } catch (_) { return; }
        [].slice.call(rr || []).forEach(function (r) {
          if (!r.style || !r.cssText) return;
          let m;
          while ((m = re.exec(r.cssText))) names[m[1]] = 1;
        });
      });
      const rcs = win.getComputedStyle(doc.documentElement);
      Object.keys(names).forEach(function (n) {
        const v = rcs.getPropertyValue(n);
        if (v && v.trim()) clone.style.setProperty(n, v.trim());
      });
      // 継承で効いていた基本の文字設定（書体・色）も焼き込む。
      // ★line-height は焼き込まない：計算値はpxで返るため、rem基準サイトだと極端に詰まった行間が
      //   そのまま持ち込まれる（実測 14.2px）。行間は各要素側のルールに任せる。
      const bcs = win.getComputedStyle(orig);
      ["font-family", "color"].forEach(function (p) {
        const v = bcs.getPropertyValue(p);
        if (v && v.trim() && !clone.style.getPropertyValue(p)) clone.style.setProperty(p, v.trim());
      });
    } catch (_) {}
    return clone;
  }

  // ===== モーダル同梱：部品内に「モーダルを開くボタン」がある場合、別置きされた本体を探す =====
  // モーダル本体はセクションの外（兄弟やbody直下）に display:none で置かれ、JSクリックで開く作りが
  // 定番。部品1個をcloneする保存方式では構造的に入らないので、ここで探して付録として同梱する。
  function findModals(part) {
    let trig = null;
    try {
      trig = part.querySelector(
        '[class*="modal" i],[id*="modal" i],[data-modal],[data-toggle="modal"],[data-bs-toggle="modal"],[data-micromodal-trigger],[data-remodal-target]'
      );
    } catch (_) {}
    if (!trig) return [];
    let cands;
    try { cands = document.querySelectorAll('[class*="modal" i],[id*="modal" i],dialog'); } catch (_) { return []; }
    const seen = [];
    [].slice.call(cands).forEach(function (n) {
      if (n === part || part.contains(n) || n.contains(part)) return;
      let cs;
      try { cs = getComputedStyle(n); } catch (_) { return; }
      // 「隠れている入れ物」だけが対象（見えているトリガーボタン等は除外）
      if (cs.display !== "none" && cs.visibility !== "hidden" && parseFloat(cs.opacity) !== 0) return;
      if (!(n.textContent || "").trim() && !n.querySelector("img")) return; // 空の入れ物は除外
      // 入れ子は一番外側だけ残す
      for (let i = seen.length - 1; i >= 0; i--) {
        if (seen[i].contains(n)) return;
        if (n.contains(seen[i])) seen.splice(i, 1);
      }
      seen.push(n);
    });
    // トリガーとの対応付け：モーダルのクラス/id名が部品側のマークアップに出てくるものを優先
    // （例：本体.modal-recruit ⇔ ボタンbtn-modal-recruit）。対応が取れなければ、候補が1個の時だけ採用。
    const partHtml = part.innerHTML;
    const matched = seen.filter(function (n) {
      const cls = typeof n.className === "string" ? n.className : (n.className && n.className.baseVal) || "";
      return (cls + " " + (n.id || "")).split(/\s+/).some(function (t) {
        return /modal|dialog|popup/i.test(t) && t.length >= 8 && partHtml.indexOf(t) !== -1;
      });
    });
    return matched.length ? matched : seen.length === 1 ? seen : [];
  }

  // モーダルを一瞬だけ「開いた状態」にして実測できるようにする（戻し関数を返す）
  function revealTemp(el) {
    const touched = [];
    function force(n, m) {
      touched.push([n, n.getAttribute("style")]);
      Object.keys(m).forEach(function (p) { n.style.setProperty(p, m[p], "important"); });
    }
    force(el, { display: "block", opacity: "1", visibility: "visible", "pointer-events": "auto" });
    [].slice.call(el.querySelectorAll("*")).forEach(function (n) {
      let cs;
      try { cs = getComputedStyle(n); } catch (_) { return; }
      const m = {};
      if (cs.display === "none") m.display = "block";
      if (cs.visibility === "hidden") m.visibility = "visible";
      if (parseFloat(cs.opacity) === 0) m.opacity = "1";
      if (Object.keys(m).length) force(n, m);
    });
    return function () {
      touched.forEach(function (t) {
        if (t[1] === null) t[0].removeAttribute("style");
        else t[0].setAttribute("style", t[1]);
      });
    };
  }

  // ===== 付録の共通処理：別要素（モーダル本体・リンク先の中身）を部品の末尾に足す =====
  // 表示できる状態（開いた/読み込んだ状態）で呼ぶこと（実測に依存するため）。
  function appendAppendix(el, clone) {
    const mr = el.getBoundingClientRect();
    let ch = mr.height;
    [].slice.call(el.querySelectorAll("*")).forEach(function (n) {
      try {
        const r = n.getBoundingClientRect();
        if (r.bottom - mr.top > ch) ch = r.bottom - mr.top;
      } catch (_) {}
    });
    const mcss = partCss(el);
    let mclone = cleanPart(el, true);
    // iframe内の要素だった場合は、保存する側のdocumentへ引き取る（iframeはこの後消えるため）
    try { mclone = clone.ownerDocument.adoptNode(mclone); } catch (_) {}
    // 画面全体かぶせ(fixed)や隠し状態をやめて、部品の下に流し込みで表示する形に直す
    [["display", "block"], ["position", "relative"], ["top", "auto"], ["left", "auto"],
     ["right", "auto"], ["bottom", "auto"], ["transform", "none"], ["opacity", "1"],
     ["visibility", "visible"], ["pointer-events", "auto"], ["width", "auto"],
     ["height", "auto"], ["min-height", Math.round(ch) + "px"], ["margin", "24px 0 0"],
     ["z-index", "auto"]
    ].forEach(function (p) { mclone.style.setProperty(p[0], p[1], "important"); });
    const mst = clone.ownerDocument.createElement("style");
    mst.setAttribute("data-cepart", "1");
    mst.textContent = mcss;
    mclone.insertBefore(mst, mclone.firstChild);
    clone.appendChild(mclone);
  }

  // ===== 別ページ型モーダル対応：部品内リンクのうち「今のページの子ページ」だけ裏で読み込んで同梱 =====
  // 例：/recruit のインタビューカード → /recruit/interview01〜03。モーダル風に開くが実体は別ページで、
  // 今のページのHTML内に中身が無いタイプ。同一オリジンの子ページに限定するので、ナビ等の無関係な
  // リンクは拾わない（トップページでは全リンクが子ページ扱いになってしまうため発動させない）。
  function detailLinks(part) {
    if (location.pathname === "/" || location.pathname === "") return [];
    const base = location.origin + location.pathname.replace(/\/+$/, "");
    const seen = {},
      out = [];
    [].slice.call(part.querySelectorAll("a[href]")).forEach(function (a) {
      let u;
      try { u = new URL(a.getAttribute("href"), location.href); } catch (_) { return; }
      if (u.origin !== location.origin) return;
      const p = u.origin + u.pathname.replace(/\/+$/, "");
      if (p === base || p.indexOf(base + "/") !== 0) return;
      if (seen[p]) return;
      seen[p] = 1;
      out.push(p);
    });
    return out.slice(0, 6); // 念のため上限（大量リンクの誤爆防止）
  }

  // 裏で1ページ読み込む（画面外の大きなiframe＝レイアウト・遅延読み込み・出現アニメを本物同様に発火させる）
  function loadInIframe(url) {
    return new Promise(function (resolve, reject) {
      const f = document.createElement("iframe");
      f.style.cssText =
        "position:fixed;left:-100000px;top:0;width:1440px;height:900px;border:0;pointer-events:none";
      const to = setTimeout(function () {
        f.remove();
        reject(new Error("読み込みタイムアウト"));
      }, 15000);
      f.onload = function () {
        clearTimeout(to);
        // ページ全体を「画面内」にして、スクロール出現・lazy画像を発火させてから測る
        try {
          const d = f.contentDocument;
          f.style.height = Math.min((d && d.documentElement.scrollHeight) || 900, 20000) + "px";
        } catch (_) {}
        setTimeout(function () { resolve(f); }, 1500);
      };
      document.documentElement.appendChild(f);
      f.src = url;
    });
  }

  // 読み込んだページの「中身」を選ぶ：<main>優先、無ければbody直下で一番大きい塊（ヘッダー等は除く）
  function pickMain(doc) {
    const m = doc.querySelector("main");
    if (m) return m;
    let best = null,
      bh = 0;
    [].slice.call(doc.body ? doc.body.children : []).forEach(function (n) {
      if (/^(HEADER|FOOTER|NAV|SCRIPT|STYLE|LINK)$/.test(n.tagName)) return;
      let h = 0;
      try { h = n.getBoundingClientRect().height; } catch (_) {}
      if (h > bh) { bh = h; best = n; }
    });
    return best || doc.body;
  }

  // リンク先を順番に読み込んで付録として同梱（1件失敗しても他は続行）
  function fetchLinkedParts(part, clone) {
    const links = detailLinks(part);
    if (!links.length) return Promise.resolve(0);
    // 勝手に大量のページを抱き込まないよう、取り込むかは毎回ユーザーに聞く
    if (!window.confirm(
      "このセクションのリンク先（子ページ）" + links.length + "件の中身も一緒に取り込みますか？\n" +
      "OK＝取り込む（保存に十数秒かかります）／キャンセル＝このセクションだけ保存"
    )) return Promise.resolve(0);
    showToast(true, "🔗 リンク先 " + links.length + "件の中身も取り込み中…（十数秒かかることがあります）");
    let cnt = 0;
    return links
      .reduce(function (pr, url) {
        return pr.then(function () {
          return loadInIframe(url)
            .then(function (f) {
              try {
                const d = f.contentDocument;
                if (d && d.body && (d.body.textContent || "").trim()) {
                  const el = pickMain(d);
                  if (el) {
                    appendAppendix(el, clone);
                    cnt++;
                  }
                }
              } catch (_) {}
              f.remove();
            })
            .catch(function () {});
        });
      }, Promise.resolve())
      .then(function () { return cnt; });
  }

  function extractPart() {
    if (!lastEl) return { ok: false, msg: "先にページ内で右クリックしてください" };
    // ★<section>を素直に辿ると、ページ全体を1つのsectionで包んでいるサイトでは「丸ごと」掴んでしまう
    //   （実例：インタビュー欄を保存したのに、採用ページ全体が入って名前もページ見出しになった）。
    //   ページの8割を超える塊は「セクション」とみなさず、クリック位置に近い手頃な塊を選び直す。
    const docH = Math.max(
      document.documentElement.scrollHeight || 0,
      document.body ? document.body.scrollHeight : 0,
      1
    );
    const tooBig = function (n) {
      try { return n.getBoundingClientRect().height > docH * 0.8; } catch (_) { return false; }
    };
    let part = lastEl.closest ? lastEl.closest("section,header,footer") : null;
    let kind = "section";
    let wrap = false;
    if (part && !tooBig(part)) {
      kind = part.tagName.toLowerCase();
      if (kind !== "header" && kind !== "footer") kind = "section";
    } else {
      // section が無い（divだけのページ）／大きすぎる → クリック位置から上へたどって
      // 「高さ150px以上・幅300px以上・ページの8割未満」で一番外側の塊を選ぶ
      let best = null,
        n = lastEl;
      for (let i = 0; i < 8 && n && n !== document.body; i++, n = n.parentElement) {
        let r;
        try { r = n.getBoundingClientRect(); } catch (_) { break; }
        if (r.height >= 150 && r.width >= 300 && r.height <= docH * 0.8) best = n;
      }
      part = best || part || lastEl;
      const tg = (part.tagName || "").toLowerCase();
      if (tg === "header" || tg === "footer") kind = tg;
      else { kind = "section"; wrap = tg !== "section"; }
    }
    const kindJp = kind === "header" ? "ヘッダー" : kind === "footer" ? "フッター" : "セクション";
    const defName =
      ((part.querySelector("h1,h2,h3,h4") || {}).textContent || document.title || "")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 20) || "無名の部品";
    const _pr = part.getBoundingClientRect();
    const name = window.prompt(
      "この" + kindJp + "を部品として保存します。\n" +
        "保存する範囲：幅" + Math.round(_pr.width) + "×高" + Math.round(_pr.height) + "px" +
        "（違う範囲なら、キャンセルして狙いの中身の上で右クリックし直してください）\n名前をどうぞ：",
      defName + "（" + location.hostname + "）"
    );
    if (name === null) return { ok: false, msg: "" }; // キャンセル

    const css = partCss(part);
    const clone = cleanPart(part);
    const styleEl = document.createElement("style");
    styleEl.setAttribute("data-cepart", "1");
    styleEl.textContent = css;
    clone.insertBefore(styleEl, clone.firstChild);

    // ★ページ内モーダル同梱：本体を開いた見た目で実測→部品の末尾に付録として足す
    //   （clone済みインラインstyleは!important付き＝保存先でもCSSのdisplay:noneに負けない）
    try {
      findModals(part).forEach(function (mo) {
        const undo = revealTemp(mo);
        try {
          void mo.offsetHeight; // 開いた状態で再レイアウトさせてから測る
          appendAppendix(mo, clone);
        } catch (_) {}
        undo();
      });
    } catch (_) {}

    // ★別ページ型モーダル同梱：子ページへのリンクを裏で読み込んで付録に（非同期）
    return fetchLinkedParts(part, clone).then(function () {
      let html = clone.outerHTML;
      if (wrap) html = "<section>" + html + "</section>";
      return { ok: true, html: html, name: name.trim() || defName, kind: kind };
    });
  }

  // ===== 右下トースト（結果のお知らせ） =====
  function showToast(ok, msg) {
    const t = document.createElement("div");
    t.textContent = msg;
    t.style.cssText =
      "position:fixed;right:18px;bottom:18px;z-index:2147483647;max-width:340px;padding:12px 16px;" +
      "border-radius:10px;font-size:13px;line-height:1.6;white-space:pre-wrap;font-family:system-ui,sans-serif;" +
      "box-shadow:0 8px 30px rgba(0,0,0,.25);color:#fff;background:" +
      (ok ? "#1a7f37" : "#c0392b");
    document.documentElement.appendChild(t);
    setTimeout(function () {
      t.remove();
    }, 5000);
  }

  chrome.runtime.onMessage.addListener(function (req, _sender, sendResponse) {
    if (req && req.type === "extract_part") {
      // extractPartはリンク先取り込みがあるため非同期（Promise）。sendResponseは後から呼ぶ。
      try {
        Promise.resolve(extractPart()).then(sendResponse, function (e) {
          sendResponse({ ok: false, msg: "抜き出しに失敗：" + (e && e.message) });
        });
      } catch (e) {
        sendResponse({ ok: false, msg: "抜き出しに失敗：" + e.message });
      }
      return true;
    }
    if (req && req.type === "toast") {
      showToast(!!req.ok, req.msg || "");
    }
  });
})();
