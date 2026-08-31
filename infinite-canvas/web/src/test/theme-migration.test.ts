import { expect, it, vi } from "vitest";

// 存量用户的 dark 持久化主题必须一次性迁移回浅色：
// 浅色化后组件按浅色硬编码，暗色下继承文字会落在浅色面板上隐身。
it("migrates a stored dark theme to light", async () => {
    localStorage.setItem(
        "infinite-canvas:theme_store",
        JSON.stringify({ state: { theme: "dark" }, version: 0 }),
    );
    vi.resetModules();
    const { useThemeStore } = await import("@/stores/use-theme-store");
    expect(useThemeStore.getState().theme).toBe("light");
});
