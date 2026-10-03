import { defineConfig } from "../../apps/web/node_modules/@playwright/test";

export default defineConfig({
  testDir: "../e2e",
  testMatch: "simulated-flow.spec.ts",
  workers: 1,
  timeout: 30000,
  outputDir: "../../artifacts/simulation/browser-results",
  use: {
    baseURL: "http://localhost:5173",
    headless: true,
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    permissions: ["microphone"],
    launchOptions: {
      args: [
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
      ],
    },
  },
});
