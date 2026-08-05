const intro = document.querySelector("[data-intro]");
const site = document.querySelector("[data-site]");
const page = document.querySelector("[data-page]");
const skipButton = document.querySelector("[data-skip]");
const replayButton = document.querySelector("[data-replay]");

const introDuration = 5000;
const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
let finishTimer;
let wasSkipped = false;

function finishIntro() {
  if (!intro || !site || !page) {
    return;
  }

  window.clearTimeout(finishTimer);
  intro.classList.add("is_finished");
  site.classList.add("is_visible");
  page.classList.remove("is_intro_open");
  intro.setAttribute("aria-hidden", "true");
  window.dispatchEvent(new CustomEvent("linkworks:intro-finish"));
}

function startIntro() {
  if (!intro || !site || !page) {
    return;
  }

  window.clearTimeout(finishTimer);
  wasSkipped = false;

  // 同じアニメーションを確実に再開するため、一度DOMを作り直す。
  const stage = intro.querySelector(".intro_stage");
  if (stage) {
    const freshStage = stage.cloneNode(true);
    stage.replaceWith(freshStage);
  }

  window.scrollTo({ top: 0, behavior: "auto" });
  intro.classList.remove("is_finished");
  site.classList.remove("is_visible");
  page.classList.add("is_intro_open");
  intro.setAttribute("aria-hidden", "false");
  window.dispatchEvent(new CustomEvent("linkworks:intro-start"));
  finishTimer = window.setTimeout(finishIntro, introDuration);
}

function initializeIntro() {
  if (!intro || !site || !page) {
    return;
  }

  if (prefersReducedMotion) {
    finishIntro();
    return;
  }

  startIntro();
}

if (skipButton) {
  skipButton.addEventListener("click", () => {
    wasSkipped = true;
    finishIntro();
  });
}

if (replayButton) {
  replayButton.addEventListener("click", startIntro);
}

// 3Dライブラリの読み込み完了時に、HTMLの文字演出と時間軸をそろえ直す。
window.addEventListener("linkworks:scene-ready", () => {
  if (!wasSkipped && !prefersReducedMotion) {
    startIntro();
  }
});

initializeIntro();
