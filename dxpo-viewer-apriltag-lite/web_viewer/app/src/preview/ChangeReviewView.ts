import { api } from "./PreviewApiClient";
import { button, field, section, selectField, statusBadge } from "./PreviewUi";

interface SiteRecord { site_id: string; name: string }
interface SceneVersion { scene_version_id: string; label: string; captured_at?: string | null }
interface SiteDetail extends SiteRecord { scene_versions: SceneVersion[] }
interface Candidate {
  change_candidate_id: string;
  asset_id: string;
  change_type: string;
  distance_m?: number | null;
  status: string;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  reviews?: Array<{ decision: string; reason: string; actor_id: string }>;
}

export interface ChangeReviewViewOptions {
  siteId?: string;
  embedded?: boolean;
}

const CHANGE_TYPE_LABELS: Record<string, string> = {
  appeared: "新規出現",
  disappeared: "消失",
  moved: "移動",
  unchanged: "変化なし",
};

function fixedSiteField(siteId: string): HTMLElement {
  const wrapper = document.createElement("div");
  const input = document.createElement("input");
  input.type = "hidden";
  input.value = siteId;
  input.dataset.field = "site_id";
  wrapper.appendChild(input);
  return wrapper;
}

function value(host: ParentNode, key: string): string {
  return host.querySelector<HTMLInputElement | HTMLSelectElement>(`[data-field="${key}"]`)?.value.trim() ?? "";
}

export async function mountChangeReviewView(host: HTMLElement, options: ChangeReviewViewOptions = {}): Promise<void> {
  host.replaceChildren();
  if (!options.embedded) {
    const heading = document.createElement("div");
    heading.className = "preview-heading";
    heading.innerHTML = "<h1>変化の確認・確定</h1><p>2つのSceneVersionを比較して変化候補を作成し、人が理由付きで承認・修正・却下します。未確認候補は確定記録や検索結果になりません。</p>";
    host.appendChild(heading);
  }
  let sites: SiteRecord[] = [];
  if (!options.siteId) {
    try {
      sites = await api<SiteRecord[]>("/api/field-memory/sites");
    } catch (error) {
      const failed = section("現場を取得できません");
      failed.append(String(error));
      host.appendChild(failed);
      return;
    }
  }
  const controls = section("比較するSceneVersion");
  const siteSelector = options.siteId
    ? fixedSiteField(options.siteId)
    : selectField("現場", sites[0]?.site_id ?? "", "site_id", sites.map((site) => ({ value: site.site_id, label: site.name })));
  const versions = document.createElement("div");
  versions.className = "preview-form spatial-search-form";
  const threshold = field("移動判定しきい値 (m)", "0.10", "number", "threshold");
  const status = document.createElement("span");
  status.className = "preview-help";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const detect = button("変化候補を作成", () => void detectCandidates());
  const refresh = button("候補を更新", () => void refreshCandidates());
  const actions = document.createElement("div");
  actions.className = "preview-inline-actions";
  actions.append(detect, refresh, status);
  controls.append(siteSelector, versions, threshold, actions);
  host.appendChild(controls);
  const output = section("確認待ち・確認済み候補");
  const summary = document.createElement("div");
  summary.className = "site-insights-summary";
  const list = document.createElement("div");
  list.className = "field-memory-list";
  output.append(summary, list);
  host.appendChild(output);

  siteSelector.querySelector("select")?.addEventListener("change", () => void loadVersions());
  await loadVersions();

  async function loadVersions(): Promise<void> {
    versions.replaceChildren();
    list.replaceChildren();
    const siteId = value(siteSelector, "site_id");
    if (!siteId) return;
    try {
      const detail = await api<SiteDetail>(`/api/field-memory/sites/${encodeURIComponent(siteId)}`);
      const options = detail.scene_versions.map((item) => ({
        value: item.scene_version_id,
        label: `${item.label} — ${item.captured_at ?? item.scene_version_id}`,
      }));
      versions.append(
        selectField("変更前", options[0]?.value ?? "", "from_scene_version_id", options),
        selectField("変更後", options[1]?.value ?? options[0]?.value ?? "", "to_scene_version_id", options),
      );
      await refreshCandidates();
    } catch (error) {
      status.textContent = `SceneVersionを取得できません: ${String(error)}`;
    }
  }

  async function detectCandidates(): Promise<void> {
    detect.disabled = true;
    status.textContent = "候補を計算しています…";
    try {
      const result = await api<{ count: number }>("/api/change-review/detect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          site_id: value(siteSelector, "site_id"),
          from_scene_version_id: value(versions, "from_scene_version_id"),
          to_scene_version_id: value(versions, "to_scene_version_id"),
          movement_threshold_m: Number((threshold.querySelector("input") as HTMLInputElement | null)?.value || "0.10"),
        }),
      });
      status.textContent = `${result.count}件の候補を作成しました。`;
      await refreshCandidates();
    } catch (error) {
      status.textContent = `候補を作成できません: ${String(error)}`;
    } finally {
      detect.disabled = false;
    }
  }

  async function refreshCandidates(): Promise<void> {
    const siteId = value(siteSelector, "site_id");
    const fromId = value(versions, "from_scene_version_id");
    const toId = value(versions, "to_scene_version_id");
    if (!siteId || !fromId || !toId) return;
    try {
      const candidates = await api<Candidate[]>("/api/change-review/candidates/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site_id: siteId, from_scene_version_id: fromId, to_scene_version_id: toId }),
      });
      list.replaceChildren();
      summary.replaceChildren();
      if (candidates.length === 0) {
        list.textContent = "候補はまだありません。";
        return;
      }
      const pendingChanges = candidates.filter((item) => item.status === "pending" && item.change_type !== "unchanged").length;
      const pendingUnchanged = candidates.filter((item) => item.status === "pending" && item.change_type === "unchanged").length;
      const reviewed = candidates.filter((item) => item.status !== "pending").length;
      for (const [label, count] of [["要判断", pendingChanges], ["変化なし確認", pendingUnchanged], ["確認済み", reviewed]] as const) {
        const item = document.createElement("span");
        item.className = "preview-status";
        item.textContent = `${label} ${count}件`;
        summary.appendChild(item);
      }
      const ordered = [...candidates].sort((left, right) => {
        const rank = (item: Candidate): number => item.status === "pending" ? (item.change_type === "unchanged" ? 1 : 0) : 2;
        return rank(left) - rank(right);
      });
      list.append(...ordered.map(candidateCard));
    } catch (error) {
      status.textContent = `候補を取得できません: ${String(error)}`;
    }
  }

  function candidateCard(candidate: Candidate): HTMLElement {
    const card = document.createElement("article");
    card.className = "field-memory-card";
    const heading = document.createElement("h3");
    heading.textContent = `${candidate.asset_id}: ${CHANGE_TYPE_LABELS[candidate.change_type] ?? candidate.change_type}`;
    const detail = document.createElement("p");
    detail.className = "preview-help";
    detail.textContent = typeof candidate.distance_m === "number" ? `移動距離 ${candidate.distance_m.toFixed(3)} m` : candidate.change_candidate_id;
    card.append(heading, statusBadge(candidate.status), detail);
    if (candidate.status === "pending") {
      const reason = field("判断理由（必須）", "", "text", "reason");
      const correctedType = selectField("修正後の変化種別", candidate.change_type, "corrected_change_type", [
        { value: "appeared", label: CHANGE_TYPE_LABELS.appeared },
        { value: "disappeared", label: CHANGE_TYPE_LABELS.disappeared },
        { value: "moved", label: CHANGE_TYPE_LABELS.moved },
        { value: "unchanged", label: CHANGE_TYPE_LABELS.unchanged },
      ]);
      const actions = document.createElement("div");
      actions.className = "preview-inline-actions";
      actions.append(
        button("承認", () => void review(candidate.change_candidate_id, "approved", reason)),
        button("種別を修正して承認", () => void review(candidate.change_candidate_id, "modified", reason, candidate, correctedType)),
        button("却下", () => void review(candidate.change_candidate_id, "rejected", reason)),
      );
      card.append(reason, correctedType, actions);
    }
    return card;
  }

  async function review(
    candidateId: string,
    decision: "approved" | "rejected" | "modified",
    reasonField: HTMLElement,
    candidate?: Candidate,
    correctedTypeField?: HTMLElement,
  ): Promise<void> {
    const reason = (reasonField.querySelector("input") as HTMLInputElement | null)?.value.trim() ?? "";
    if (!reason) {
      status.textContent = "判断理由を入力してください。";
      return;
    }
    try {
      const payload: Record<string, unknown> = { decision, reason };
      if (decision === "modified" && candidate && correctedTypeField) {
        payload.change = {
          change_type: value(correctedTypeField, "corrected_change_type"),
          before: candidate.before ?? null,
          after: candidate.after ?? null,
          distance_m: candidate.distance_m ?? null,
        };
      }
      await api<Candidate>(`/api/change-review/candidates/${encodeURIComponent(candidateId)}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      status.textContent = decision === "approved"
        ? "承認し、確定変更記録を作成しました。"
        : decision === "modified"
          ? "変化種別を修正し、確定変更記録を作成しました。"
          : "却下しました。";
      await refreshCandidates();
    } catch (error) {
      status.textContent = `確認結果を保存できません: ${String(error)}`;
    }
  }
}
