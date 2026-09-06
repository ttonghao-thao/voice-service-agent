import { test, expect } from "../../apps/web/node_modules/@playwright/test";
test("中文文字闭环、证据、刷新历史与窄屏布局", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "让每一次对话，都有回应" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "新建会话" }).click();
  await page
    .getByRole("textbox", { name: "输入您的问题" })
    .fill("张先生，请查询联调示例。");
  await page.getByRole("button", { name: "发送问题" }).click();
  await expect(page.locator(".assistant-message")).toContainText(
    "合成联调资料",
  );
  await expect(page.locator(".citation-title")).toContainText("C1");
  await page.reload();
  await expect(page.locator(".assistant-message")).toContainText(
    "合成联调资料",
  );
  await page.screenshot({
    path: "../../artifacts/portal-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "会话列表" })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../../artifacts/portal-mobile.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
test("未命中知识不能显示为真实业务成功", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话" }).click();
  await page
    .getByRole("textbox", { name: "输入您的问题" })
    .fill("请告诉我不存在的退款政策");
  await page.getByRole("button", { name: "发送问题" }).click();
  await expect(page.locator(".assistant-message")).toContainText("依据不足");
});
test("麦克风不可用时仍能文字提问", async ({ page }) => {
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      throw new DOMException("麦克风权限被拒绝", "NotAllowedError");
    };
  });
  await page.goto("/");
  await page.getByRole("button", { name: "开始语音", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "输入您的问题" }),
  ).toBeEnabled();
});

test("管理员查看服务状态、停用和恢复工具", async ({ page, request }) => {
  const identity = await (await request.get("/api/v1/auth/me")).json();
  test.skip(
    !identity.scopes.includes("tools:admin"),
    "此项需要 DEV_ADMIN=true 或真实管理员身份",
  );
  await page.goto("/");
  await page.getByRole("button", { name: "工具管理" }).click();
  await expect(page.getByText("语音服务：演示模式")).toBeVisible();
  const toggle = page.getByRole("switch", { name: "启用search_knowledge" });
  await expect(toggle).toBeChecked();
  try {
    await toggle.click();
    await expect(toggle).not.toBeChecked();
    const tools = await (await request.get("/api/v1/admin/tools")).json();
    expect(
      tools.items.find((t: { name: string }) => t.name === "search_knowledge")
        .enabled,
    ).toBe(false);
  } finally {
    await request.patch("/api/v1/admin/tools/search_knowledge", {
      data: { enabled: true },
    });
  }
});
