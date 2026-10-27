import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  // Keep generated browser artifacts out of the source tree. Docker-based E2E
  // runs can create root-owned files; placing the default below the repository
  // runtime root prevents those files from blocking a later host-side run.
  outputDir: process.env.PW_OUTPUT_DIR ?? "../../var/spatial/test-results/playwright",
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    permissions: ["camera"],
    launchOptions: {
      args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
    },
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4173",
      url: "http://127.0.0.1:4173",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4174 --mode custom",
      env: {
        SPATIAL_APP_MODES: "packages",
        SPATIAL_APP_LABEL: "E2E composed distribution",
      },
      url: "http://127.0.0.1:4174",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
