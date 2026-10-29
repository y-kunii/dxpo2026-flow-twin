import {
  SPATIAL_APP_CATEGORIES,
  SPATIAL_APP_DEFINITIONS,
  SPATIAL_APP_ORDER,
  type SpatialAppAudience,
  type SpatialAppDefinition,
  type SpatialAppId,
} from "./AppPackageContract";

const AUDIENCE_LABELS: Readonly<Record<SpatialAppAudience, string>> = {
  basic: "基本",
  operator: "運用",
  administrator: "管理者",
  advanced: "分析",
};

const WORKFLOW: ReadonlyArray<{ id: SpatialAppId; step: string; action: string }> = [
  { id: "colmap", step: "1", action: "動画から3D再構成" },
  { id: "spz-import", step: "2", action: "学習済みSPZを登録" },
  { id: "scene-manager", step: "3", action: "データを確認・整理" },
  { id: "viewer", step: "4", action: "3Dシーンを閲覧" },
];

function appHref(app: SpatialAppDefinition): string {
  if (app.id === "viewer") return "/?preview=packages&intent=viewer";
  return app.previewMode ? `/?preview=${encodeURIComponent(app.previewMode)}` : "/";
}

function appCard(app: SpatialAppDefinition): HTMLAnchorElement {
  const card = document.createElement("a");
  card.className = "spatial-launcher-card";
  card.dataset.appId = app.id;
  card.dataset.previewMode = app.previewMode ?? "";
  card.dataset.search = `${app.label} ${app.description} ${app.useWhen} ${app.id}`.toLocaleLowerCase("ja");
  card.href = appHref(app);

  const header = document.createElement("div");
  header.className = "spatial-launcher-card-header";
  const label = document.createElement("h3");
  label.textContent = app.label;
  const audience = document.createElement("span");
  audience.className = `spatial-launcher-audience is-${app.audience}`;
  audience.textContent = AUDIENCE_LABELS[app.audience];
  header.append(label, audience);

  const description = document.createElement("p");
  description.textContent = app.description;
  const useWhen = document.createElement("small");
  useWhen.textContent = `使う場面: ${app.useWhen}`;
  const action = document.createElement("strong");
  action.textContent = "開く →";
  card.append(header, description, useWhen, action);
  return card;
}

export function mountApplicationLauncher(host: HTMLElement): void {
  document.title = "Spatial 3DGS — アプリ選択";
  document.body.dataset.spatialApp = "suite-home";

  const shell = document.createElement("div");
  shell.className = "spatial-launcher";
  const header = document.createElement("header");
  header.className = "spatial-launcher-header";
  const brand = document.createElement("div");
  brand.innerHTML = "<strong>Spatial 3DGS</strong><span>統合運用TOP</span>";
  const packages = document.createElement("a");
  packages.href = "/?preview=packages";
  packages.textContent = "シーンデータ管理";
  header.append(brand, packages);

  const main = document.createElement("main");
  main.className = "spatial-launcher-main";
  const hero = document.createElement("section");
  hero.className = "spatial-launcher-hero";
  hero.innerHTML = "<p>Spatial 3DGS System</p><h1>目的からアプリを選ぶ</h1><span>基本の作業順またはカテゴリから選択できます。アプリIDは配布・API互換のため変更していません。</span>";
  main.appendChild(hero);

  const workflow = document.createElement("section");
  workflow.className = "spatial-launcher-workflow";
  const workflowTitle = document.createElement("div");
  workflowTitle.innerHTML = "<h2>基本の作業順</h2><p>新しい撮影データを3Dシーンとして閲覧するまで</p>";
  const workflowSteps = document.createElement("div");
  workflowSteps.className = "spatial-launcher-workflow-steps";
  for (const item of WORKFLOW) {
    const app = SPATIAL_APP_DEFINITIONS[item.id];
    const link = document.createElement("a");
    link.href = appHref(app);
    link.dataset.appId = app.id;
    link.innerHTML = `<b>${item.step}</b><span>${item.action}</span><small>${app.label}</small>`;
    workflowSteps.appendChild(link);
  }
  workflow.append(workflowTitle, workflowSteps);
  main.appendChild(workflow);

  const searchLabel = document.createElement("label");
  searchLabel.className = "spatial-launcher-search";
  const searchCaption = document.createElement("span");
  searchCaption.textContent = "アプリを絞り込む";
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "例: 3Dを見る、設備、AI、配布";
  searchLabel.append(searchCaption, search);
  main.appendChild(searchLabel);

  const sections: HTMLElement[] = [];
  const cards: HTMLAnchorElement[] = [];
  for (const category of SPATIAL_APP_CATEGORIES) {
    const section = document.createElement("section");
    section.className = "spatial-launcher-section";
    section.dataset.category = category.id;
    const heading = document.createElement("div");
    heading.className = "spatial-launcher-section-heading";
    const title = document.createElement("h2");
    title.textContent = category.label;
    const description = document.createElement("p");
    description.textContent = category.description;
    heading.append(title, description);
    const grid = document.createElement("div");
    grid.className = "spatial-launcher-grid";
    for (const id of SPATIAL_APP_ORDER) {
      const app = SPATIAL_APP_DEFINITIONS[id];
      if (app.category !== category.id) continue;
      const card = appCard(app);
      cards.push(card);
      grid.appendChild(card);
    }
    section.append(heading, grid);
    sections.push(section);
    main.appendChild(section);
  }

  const empty = document.createElement("p");
  empty.className = "spatial-launcher-empty";
  empty.textContent = "条件に合うアプリはありません。検索語を短くしてください。";
  empty.hidden = true;
  main.appendChild(empty);

  search.addEventListener("input", () => {
    const term = search.value.trim().toLocaleLowerCase("ja");
    let visibleCount = 0;
    for (const card of cards) {
      card.hidden = Boolean(term) && !(card.dataset.search ?? "").includes(term);
      if (!card.hidden) visibleCount += 1;
    }
    for (const section of sections) {
      section.hidden = !Array.from(section.querySelectorAll<HTMLAnchorElement>(".spatial-launcher-card"))
        .some((card) => !card.hidden);
    }
    workflow.hidden = Boolean(term);
    empty.hidden = visibleCount !== 0;
  });

  const footer = document.createElement("footer");
  footer.className = "spatial-launcher-footer";
  footer.textContent = "アプリ別ガイド: docs/apps/ ・ 統合起動: scripts/start_app.sh all";
  shell.append(header, main, footer);
  host.appendChild(shell);
}
