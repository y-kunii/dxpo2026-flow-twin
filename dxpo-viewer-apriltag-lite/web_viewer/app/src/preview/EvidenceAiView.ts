import { api } from "./PreviewApiClient";
import { button, field, section, selectField, statusBadge } from "./PreviewUi";

interface SiteRecord { site_id: string; name: string }
interface Citation { record_type: string; record_id: string; excerpt?: string }
interface Inquiry {
  inquiry_id: string;
  site_id: string;
  question: string;
  status: "queued" | "running" | "completed" | "failed";
  answer?: string | null;
  is_unknown: boolean;
  model_id: string;
  model_revision: string;
  failure_reason?: string | null;
  citations: Citation[];
  created_at?: string;
}

export interface EvidenceAiViewOptions {
  siteId?: string;
  embedded?: boolean;
}

function value(host: ParentNode, key: string): string {
  return host.querySelector<HTMLInputElement | HTMLSelectElement>(`[data-field="${key}"]`)?.value.trim() ?? "";
}

function fixedSiteField(siteId: string): HTMLInputElement {
  const input = document.createElement("input");
  input.type = "hidden";
  input.value = siteId;
  input.dataset.field = "site_id";
  return input;
}

export async function mountEvidenceAiView(host: HTMLElement, options: EvidenceAiViewOptions = {}): Promise<() => void> {
  let stopped = false;
  let timer: number | undefined;
  host.replaceChildren();
  if (!options.embedded) {
    const heading = document.createElement("div");
    heading.className = "preview-heading";
    heading.innerHTML = "<h1>根拠付き質問</h1><p>指定した現場の検索結果だけを根拠としてローカルVLMが回答します。引用できる根拠がない場合は不明として返します。</p>";
    host.appendChild(heading);
  }
  let sites: SiteRecord[] = [];
  if (!options.siteId) {
    try {
      sites = await api<SiteRecord[]>("/api/spatial-search/sites");
    } catch (error) {
      const failed = section("現場を取得できません");
      failed.append(String(error));
      host.appendChild(failed);
      return () => { stopped = true; };
    }
  }
  const controls = section("質問と検索範囲");
  const form = document.createElement("div");
  form.className = "preview-form spatial-search-form";
  form.append(
    options.siteId
      ? fixedSiteField(options.siteId)
      : selectField("現場", sites[0]?.site_id ?? "", "site_id", sites.map((site) => ({ value: site.site_id, label: site.name }))),
    field("質問", "", "text", "question"),
    field("根拠検索キーワード", "", "search", "query"),
  );
  const status = document.createElement("span");
  status.className = "preview-help";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const output = section("回答");
  const ask = button("根拠を固定して質問", () => void createInquiry());
  controls.append(form, ask, status);
  const history = section("質問・回答履歴");
  const historyActions = document.createElement("div");
  historyActions.className = "preview-inline-actions";
  const historyStatus = document.createElement("span");
  historyStatus.className = "preview-help";
  const refreshHistoryButton = button("履歴を更新", () => void refreshHistory());
  const historyList = document.createElement("div");
  historyList.className = "field-memory-list evidence-inquiry-history";
  historyActions.append(refreshHistoryButton, historyStatus);
  history.append(historyActions, historyList);
  host.append(controls, output, history);
  await refreshHistory();

  async function createInquiry(): Promise<void> {
    const siteId = value(form, "site_id");
    const question = value(form, "question");
    if (!siteId || !question) {
      status.textContent = "現場と質問を入力してください。";
      return;
    }
    ask.disabled = true;
    status.textContent = "根拠候補を検索・固定しています…";
    try {
      const inquiry = await api<Inquiry>("/api/evidence-ai/inquiries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, search: { site_id: siteId, query: value(form, "query"), limit: 30 } }),
      });
      renderInquiry(inquiry);
      await refreshHistory();
      poll(inquiry.inquiry_id);
    } catch (error) {
      status.textContent = `質問を登録できません: ${String(error)}`;
      ask.disabled = false;
    }
  }

  function poll(inquiryId: string): void {
    if (stopped) return;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      void api<Inquiry>(`/api/evidence-ai/inquiries/${encodeURIComponent(inquiryId)}`).then((inquiry) => {
        renderInquiry(inquiry);
        if (inquiry.status === "queued" || inquiry.status === "running") poll(inquiryId);
        else void refreshHistory();
      }).catch((error) => {
        status.textContent = `処理状況を取得できません: ${String(error)}`;
        ask.disabled = false;
      });
    }, 1000);
  }

  function renderInquiry(inquiry: Inquiry): void {
    output.replaceChildren(output.querySelector("h2") ?? document.createElement("h2"));
    const title = output.querySelector("h2");
    if (title) title.textContent = "回答";
    output.append(statusBadge(inquiry.status));
    status.textContent = `${inquiry.status} / ${inquiry.model_id}@${inquiry.model_revision}`;
    if (inquiry.status === "completed") {
      const answer = document.createElement("p");
      answer.textContent = inquiry.answer || "根拠が不足しているため回答できません。";
      const grounding = document.createElement("p");
      grounding.className = "preview-help";
      grounding.textContent = inquiry.is_unknown ? "判定: 不明（根拠不足）" : `引用: ${inquiry.citations.length}件`;
      output.append(answer, grounding);
      for (const citation of inquiry.citations) {
        const item = document.createElement("p");
        item.className = "preview-help";
        item.textContent = `[${citation.record_type}/${citation.record_id}] ${citation.excerpt ?? ""}`;
        output.appendChild(item);
      }
      ask.disabled = false;
    } else if (inquiry.status === "failed") {
      output.append(inquiry.failure_reason || "解析に失敗しました。");
      ask.disabled = false;
    }
  }

  async function refreshHistory(): Promise<void> {
    const siteId = value(form, "site_id");
    if (!siteId || stopped) return;
    refreshHistoryButton.disabled = true;
    try {
      const inquiries = await api<Inquiry[]>("/api/evidence-ai/inquiries/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site_id: siteId }),
      });
      historyList.replaceChildren();
      historyStatus.textContent = `${inquiries.length}件`;
      if (!inquiries.length) {
        historyList.textContent = "この現場の質問履歴はまだありません。";
        return;
      }
      for (const inquiry of inquiries) {
        const card = document.createElement("article");
        card.className = "field-memory-card evidence-inquiry-card";
        const title = document.createElement("h3");
        title.textContent = inquiry.question;
        const meta = document.createElement("p");
        meta.className = "preview-help";
        meta.textContent = [inquiry.created_at, `${inquiry.model_id}@${inquiry.model_revision}`].filter(Boolean).join(" / ");
        const answer = document.createElement("p");
        answer.textContent = inquiry.status === "completed"
          ? (inquiry.answer || "根拠が不足しているため回答できません。")
          : inquiry.status === "failed"
            ? `失敗: ${inquiry.failure_reason ?? "原因不明"}`
            : "回答を生成しています。";
        const inspect = button("この回答を確認", () => renderInquiry(inquiry));
        inspect.className = "preview-secondary";
        card.append(title, statusBadge(inquiry.status), meta, answer, inspect);
        historyList.appendChild(card);
      }
    } catch (error) {
      historyStatus.textContent = `履歴を取得できません: ${String(error)}`;
    } finally {
      refreshHistoryButton.disabled = false;
    }
  }
  return () => {
    stopped = true;
    window.clearTimeout(timer);
  };
}
