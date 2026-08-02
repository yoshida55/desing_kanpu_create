# Codex project instructions

## Scope

This file applies to the entire repository.

## Required knowledge guide

At the start of every new Codex thread, after the required repository confirmation
below and before other task work, read `D:\50_knowledge\01_Ai_Brain\00_AI_案内.md`
completely. Use it as the map for locating relevant knowledge, but open only the
linked files needed for the current task rather than loading every linked document.
If the guide requires its own read confirmation, place that confirmation after the
repository confirmation required below.

## Required session-start confirmation

On the first user-facing response of every new Codex thread started for this
repository, before any other commentary or task work, output this exact Japanese
sentence once:

`共同編集仕様を確認しました。ツールとCodex／Claude Codeは同時編集せず、交互に作業します。`

Do not repeat this confirmation on later turns in the same thread. Creating an empty
thread cannot trigger an unsolicited message; show it at the beginning of the first
response after the user sends a message.

## Codex and visual-editor co-editing

When a task concerns any of the following, read
`docs/Codexと編集ツールの共同編集仕様書.md` completely before inspecting,
editing, or generating implementation files:

- camp HTML files or pages served from `/camp/`
- the visual editor, edit bar, DOM selection, movement, resizing, or transforms
- layout cleanup, text positioning, image replacement, or visual corrections by Codex
- saving, loading, duplicating, or reusing editable sections
- the Codex-to-tool or tool-to-Codex workflow
- stable element IDs, patch files, patch APIs, or patch CLI commands

The specification above is the source of truth for this co-editing workflow.
If it conflicts with the current implementation, inspect the current code and report
the difference before changing behavior. Do not silently weaken the safety rules.

## Mandatory uncertainty disclosure

- Never fill gaps in understanding with a confident guess. If the specification,
  current implementation, supported operation, or round-trip safety is not fully
  understood or verified, explicitly tell the user `分かりません` or `未確認です`
  before proceeding.
- Clearly distinguish facts confirmed from the specification or code, inferences,
  and unknowns. Rendering correctly in a browser is not evidence that the visual
  editor can select, move, save, and reopen the same structure safely.
- Before changing a canonical camp HTML file, classify the proposed change as a
  validated patch operation, an explicitly authorized risky direct edit, or an
  unknown/unsupported operation. Stop on unknown/unsupported operations unless the
  user knowingly authorizes the exact risky direct edit.
- Do not claim that a preview can be promoted, that the editor will understand it,
  or that saving is safe without concrete verification. When verification has not
  been completed, say so plainly even if that pauses the task.

## Save handoff communication

- Never leave the user to infer whether the visual editor may save. At every
  Codex/tool handoff, explicitly say either `まだ保存しないでください` or
  `いま「変更を保存」を押してOKです`.
- While Codex is inspecting, editing, generating, or validating a camp patch, tell
  the user not to save from the visual editor.
- After a patch has been validated and Codex has finished its turn of editing, give
  the exact `/camp/` URL as plain, unmasked text and explicitly tell the user to
  review it and press `変更を保存` once if it looks correct.
- Do not describe a pending patch as adopted into the canonical HTML until the user
  has saved it successfully. Before starting the next Codex edit, check patch/save
  status again.
- Write local preview URLs directly in chat so the user can copy and paste them;
  do not provide only a Markdown link with a substituted label.

## Durable user design preferences

- Before proposing or changing a design comp, read
  `D:\50_knowledge\各種資料\03_デザイン\design_ルールブック.md` and use its
  recorded preferences and process.
- The user has authorized Codex to record reusable design preferences as they emerge.
  When the user repeatedly approves, rejects, or explains the reason for a visual
  choice, append a concise dated note to that design rulebook without asking again.
- Record durable tendencies and the reason behind them, not every temporary choice
  made for one page. Keep detailed preferences in the design rulebook and keep this
  `AGENTS.md` limited to the recording workflow.

## Design-comp purpose and review viewport

- Pages served from `/camp/` are editable design comps, not the final artifacts
  delivered to customers.
- The expected delivery workflow is that another person uses a camp page as the
  visual reference and rebuilds or simplifies it into ordinary HTML for delivery.
- A sample camp page may depict a fictional or assumed client business, such as a
  type-B continuous-employment support office. Its real sales purpose is to show
  prospective web-design clients what kinds of websites the user's company can
  create and to win website-production work. Do not mistake the depicted business's
  customers for the audience of the user's own sales activity.
- Review such a sample both as a credible website for the depicted organization and
  as a portfolio piece demonstrating the user's design and implementation ability.
- Unless the user explicitly requests responsive or mobile work, inspect, review,
  and improve camp pages for the desktop viewport only. Do not spend task time on
  mobile layout, responsive breakpoints, or mobile-specific recommendations.
- When advising on implementation, distinguish ideas that belong in the design comp
  from engineering choices that should be made in the later delivery HTML.

## Current implementation status

The co-editing specification is a design document until its implementation and all
acceptance tests are complete. Do not claim that round-trip editing is safe merely
because the specification or `codex_test/layout-fix.js` exists.

Until the patch workflow is implemented:

- Do not directly modify a production camp HTML file for a Codex visual cleanup
  unless the user explicitly requests that exact direct edit and accepts that the
  visual editor may no longer understand it.
- Prefer a copied test directory or other isolated preview for experiments.
- Treat `codex_test/layout-fix.js` only as a prototype, not as the production
  co-editing mechanism.

After the patch workflow is implemented:

- Codex must use the validated camp patch CLI or API for supported visual edits.
- Codex must not directly rewrite camp HTML, arbitrary DOM structure, `data-ce*`
  state, or editor JavaScript for an individual page.
- Inspect stable `data-ceid` targets before creating operations.
- Validate the patch and report missing targets or rejected operations.
- Structural insertion, deletion, reparenting, or arbitrary `innerHTML` changes are
  unsupported unless the editor provides a typed, validated operation for them.
- A tool save must adopt the applied patch into the canonical HTML and prevent the
  patch from being applied twice.
- Hash or revision conflicts must stop the save; never force an overwrite.

## Implementation requirements

When implementing this feature:

1. Follow the phases and acceptance criteria in the specification in order.
2. Preserve the current no-patch behavior for existing pages.
3. Keep stable element identity separate from transform state such as `data-cetx`,
   `data-cety`, `data-cesx`, `data-cesy`, `data-cero`, and `data-cebt`.
4. Ensure duplicated elements and inserted favorite sections receive fresh IDs.
5. Apply patches before the editor initializes.
6. Preserve applied visual results during `cleanHtml()` while removing patch-loader
   and editor-only temporary UI.
7. Save HTML atomically, clear or archive a patch only after the HTML save succeeds,
   and reject stale revisions with an explicit conflict response.
8. Run the mandatory tool -> Codex -> tool round-trip test before declaring the
   feature complete.

## Repository safety

- Preserve unrelated user changes, including existing edits in `CLAUDE.md`.
- Do not overwrite or remove original camp files when creating a test copy.
- Do not bulk-migrate existing camp HTML files without explicit user approval.
- Keep implementation history and detailed design notes in `docs/`; keep this file
  concise and limited to durable Codex instructions.
