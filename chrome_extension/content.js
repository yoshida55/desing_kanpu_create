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
      kf = [];
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
          out.push(mediaTxt ? "@media " + mediaTxt + "{" + r.cssText + "}" : r.cssText);
          const rs = extraSels(r.selectorText);
          if (rs.length) {
            const rule = rs.join(",") + "{" + r.style.cssText + "}";
            out.push(mediaTxt ? "@media " + mediaTxt + "{" + rule + "}" : rule);
          }
        }
      });
    }
    [].slice.call(document.styleSheets).forEach(function (ss) {
      let rr;
      try {
        rr = ss.cssRules; // 他ドメインのCSS(Googleフォント等)は読めないので黙ってスキップ
      } catch (_) {
        return;
      }
      scan(rr);
    });
    // 護身用：入れ替え先の「浮く絶対配置」に被されないよう、staticの部品はrelative+z-index:1に
    if (getComputedStyle(el).position === "static") out.push(":scope{position:relative;z-index:1}");
    // ★rem対策（ツール本体と同じ）：元サイトが html{font-size:clamp(100vw/1440)} 等で
    //   1rem を 1px 未満に縮めていると、貼り先のカンプ（1rem=16px）で寸法が十数倍に膨張する
    //   （実例：width:300rem が 267px のはずが 4800px になった）。保存時に px へ焼き直す。
    let rootPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    if (Math.abs(rootPx - 16) < 0.01) {
      // ページ側で指定が効いていない場合は、CSSに書かれている指定を実際に計算して本来の値を得る
      let pv = "";
      [].slice.call(document.styleSheets).forEach(function (ss) {
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
          const p = document.createElement("div");
          p.style.cssText = "position:absolute;left:-9999px;top:0;visibility:hidden;font-size:" + pv;
          document.documentElement.appendChild(p);
          const px = parseFloat(getComputedStyle(p).fontSize) || 0;
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
  function cleanPart(orig) {
    const clone = orig.cloneNode(true);
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
    return clone;
  }

  function extractPart() {
    if (!lastEl) return { ok: false, msg: "先にページ内で右クリックしてください" };
    let part = lastEl.closest ? lastEl.closest("section,header,footer") : null;
    let kind = "section";
    let wrap = false;
    if (part) {
      kind = part.tagName.toLowerCase();
      if (kind !== "header" && kind !== "footer") kind = "section";
    } else {
      // section等の外（divだけのページ）＝右クリックした要素の大きめの親を<section>で包んで保存
      part = lastEl;
      for (let i = 0; i < 4 && part.parentElement && part.parentElement !== document.body; i++) {
        part = part.parentElement;
      }
      wrap = true;
    }
    const kindJp = kind === "header" ? "ヘッダー" : kind === "footer" ? "フッター" : "セクション";
    const defName =
      ((part.querySelector("h1,h2,h3") || {}).textContent || document.title || "")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 20) || "無名の部品";
    const name = window.prompt(
      "この" + kindJp + "を部品として保存します。名前をどうぞ：",
      defName + "（" + location.hostname + "）"
    );
    if (name === null) return { ok: false, msg: "" }; // キャンセル

    const css = partCss(part);
    const clone = cleanPart(part);
    const styleEl = document.createElement("style");
    styleEl.setAttribute("data-cepart", "1");
    styleEl.textContent = css;
    clone.insertBefore(styleEl, clone.firstChild);

    let html = clone.outerHTML;
    if (wrap) html = "<section>" + html + "</section>";
    return { ok: true, html: html, name: name.trim() || defName, kind: kind };
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
      try {
        sendResponse(extractPart());
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
