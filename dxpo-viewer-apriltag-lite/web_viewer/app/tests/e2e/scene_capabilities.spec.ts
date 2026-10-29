import { expect, test } from "@playwright/test";

const bundle = {
  kind: "spatial_3dgs_viewer_distribution_index",
  packages: [{
    managed: true,
    published: true,
    status: "verified",
    scene_package_id: "example",
    scene_id: "example",
    viewer_url: "/?scene_package=example",
  }],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/scene_package/distribution_index.json", (route) => route.fulfill({ json: bundle }));
  await page.route("**/api/scene-packages", (route) => route.fulfill({ json: [] }));
});

test("read-only distribution shows the viewer without calling management APIs", async ({ page }) => {
  const managementRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/repository")) {
      managementRequests.push(request.url());
    }
  });
  await page.route("**/api/health", (route) => route.fulfill({
    json: { enabled_features: ["scene-reader", "apriltag-check"] },
  }));

  await page.goto("http://127.0.0.1:4174/");
  await expect(page.getByRole("heading", { name: "3Dシーン閲覧" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "example" })).toBeVisible();
  await expect(page.locator('option[value="packages"]')).toHaveCount(0);
  await expect(page.getByText("保存データの選択・整理")).toHaveCount(0);
  await expect(page.getByText("Repositoryデータ一覧を取得できません")).toHaveCount(0);
  expect(managementRequests).toEqual([]);
});

test("management view remains available when the API enables it", async ({ page }) => {
  await page.route("**/api/health", (route) => route.fulfill({
    json: { enabled_features: ["scene-manager"] },
  }));
  await page.route("**/api/repository", (route) => route.fulfill({
    json: { status: "verified", counts: {}, migration: {}, package_migration: {}, layout: {} },
  }));
  await page.route("**/api/repository/items", (route) => route.fulfill({
    json: { items: [], summary: { item_count: 0 } },
  }));

  await page.goto("http://127.0.0.1:4174/");
  await expect(page.getByRole("heading", { name: "シーンデータ管理" })).toBeVisible();
  await expect(page.getByText("保存データの選択・整理")).toBeVisible();
  await expect(page.getByText("管理対象 0件", { exact: false })).toBeVisible();
  await expect(page.locator('option[value="packages"]')).toHaveCount(1);
});

test("an unavailable API is reported instead of being called read-only", async ({ page }) => {
  await page.route("**/api/health", (route) => route.fulfill({
    status: 503, json: { failure_reasons: ["service_unavailable"] },
  }));
  await page.route("**/api/repository", (route) => route.fulfill({
    status: 503, json: { failure_reasons: ["service_unavailable"] },
  }));
  await page.route("**/api/repository/items", (route) => route.fulfill({
    status: 503, json: { failure_reasons: ["service_unavailable"] },
  }));

  await page.goto("http://127.0.0.1:4174/");
  await expect(page.getByText("Repositoryの状態を取得できません: Error: service_unavailable")).toBeVisible();
});
