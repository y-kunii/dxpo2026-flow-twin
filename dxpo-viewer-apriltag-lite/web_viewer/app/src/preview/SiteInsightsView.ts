import { api } from "./PreviewApiClient";
import { section } from "./PreviewUi";
import { mountSpatialSearchView } from "./SpatialSearchView";
import { mountEvidenceAiView } from "./EvidenceAiView";
import { mountChangeReviewView } from "./ChangeReviewView";

interface SiteRecord {
  site_id: string;
  name: string;
}

type WorkflowId = "search" | "ask" | "change";

interface WorkflowDefinition {
  id: WorkflowId;
  label: string;
  description: string;
}

const WORKFLOWS: readonly WorkflowDefinition[] = [
  {
    id: "search",
    label: "1. 記録を探す",
    description: "設備・文書・観測・登録画像を横断し、該当する3D位置まで確認します。",
  },
  {
    id: "ask",
    label: "2. 根拠から問う",
    description: "検索結果を固定した根拠としてAIに質問し、回答と引用を履歴へ残します。",
  },
  {
    id: "change",
    label: "3. 変化を確定する",
    description: "SceneVersion間の候補を比較し、人の判断と理由を監査可能な記録にします。",
  },
];

export async function mountSiteInsightsView(host: HTMLElement): Promise<() => void> {
  let disposed = false;
  let childCleanup: (() => void) | undefined;
  let renderSequence = 0;
  let activeWorkflow: WorkflowId = "search";

  host.replaceChildren();
  const heading = document.createElement("div");
  heading.className = "preview-heading";
  heading.innerHTML = [
    "<h1>現場インサイト</h1>",
    "<p>同じ現場を対象に、情報を探す・根拠付きで問う・変化を確定する作業を一つの画面で完結します。AIの出力は根拠または人の判断を伴わない限り、確定記録として扱いません。</p>",
  ].join("");
  host.appendChild(heading);

  let sites: SiteRecord[];
  try {
    sites = await api<SiteRecord[]>("/api/spatial-search/sites");
  } catch (error) {
    const failed = section("現場インサイトを開始できません");
    failed.append(`現場台帳または検索APIを確認してください: ${String(error)}`);
    host.appendChild(failed);
    return () => { disposed = true; };
  }

  if (!sites.length) {
    const empty = section("先に現場を登録してください");
    empty.append("検索・質問・変化確認は、現場・設備台帳に登録された現場を共通の対象として実行します。");
    host.appendChild(empty);
    return () => { disposed = true; };
  }

  const context = section("対象現場");
  context.classList.add("site-insights-context");
  const siteLabel = document.createElement("label");
  siteLabel.className = "preview-field";
  const siteLabelText = document.createElement("span");
  siteLabelText.textContent = "検索・質問・比較に共通して使用する現場";
  const siteSelect = document.createElement("select");
  siteSelect.setAttribute("aria-label", "現場インサイトの対象現場");
  for (const site of sites) {
    const option = document.createElement("option");
    option.value = site.site_id;
    option.textContent = `${site.name} — ${site.site_id}`;
    siteSelect.appendChild(option);
  }
  const contract = document.createElement("p");
  contract.className = "preview-help";
  contract.textContent = "この選択は3つの作業で共有されます。別現場の記録を根拠や比較対象へ混在させません。";
  siteLabel.append(siteLabelText, siteSelect);
  context.append(siteLabel, contract);
  host.appendChild(context);

  const workflowNav = document.createElement("div");
  workflowNav.className = "site-insights-tabs";
  workflowNav.setAttribute("role", "tablist");
  const buttons = new Map<WorkflowId, HTMLButtonElement>();
  for (const workflow of WORKFLOWS) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "site-insights-tab";
    tab.dataset.workflow = workflow.id;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", "site-insights-workspace");
    const label = document.createElement("strong");
    label.textContent = workflow.label;
    const description = document.createElement("span");
    description.textContent = workflow.description;
    tab.append(label, description);
    tab.addEventListener("click", () => {
      activeWorkflow = workflow.id;
      void renderWorkflow();
    });
    buttons.set(workflow.id, tab);
    workflowNav.appendChild(tab);
  }
  host.appendChild(workflowNav);

  const workspace = document.createElement("div");
  workspace.id = "site-insights-workspace";
  workspace.className = "site-insights-workspace";
  workspace.setAttribute("role", "tabpanel");
  host.appendChild(workspace);

  siteSelect.addEventListener("change", () => void renderWorkflow());
  await renderWorkflow();

  async function renderWorkflow(): Promise<void> {
    const sequence = ++renderSequence;
    childCleanup?.();
    childCleanup = undefined;
    for (const [id, tab] of buttons) {
      const selected = id === activeWorkflow;
      tab.classList.toggle("is-active", selected);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    }
    workspace.setAttribute("aria-label", WORKFLOWS.find((item) => item.id === activeWorkflow)?.label ?? "現場インサイト");
    const siteId = siteSelect.value;
    const panel = document.createElement("div");
    panel.className = "site-insights-workflow";
    let nextCleanup: (() => void) | undefined;
    if (activeWorkflow === "search") {
      await mountSpatialSearchView(panel, { siteId, embedded: true });
    } else if (activeWorkflow === "ask") {
      nextCleanup = await mountEvidenceAiView(panel, { siteId, embedded: true });
    } else {
      await mountChangeReviewView(panel, { siteId, embedded: true });
    }
    if (disposed || sequence !== renderSequence) {
      nextCleanup?.();
      return;
    }
    workspace.replaceChildren(panel);
    childCleanup = nextCleanup;
  }

  return () => {
    disposed = true;
    renderSequence += 1;
    childCleanup?.();
  };
}
