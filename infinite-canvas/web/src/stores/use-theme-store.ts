import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ThemeName = "light" | "dark";

type ThemeStore = {
    theme: ThemeName;
    setTheme: (theme: ThemeName) => void;
};

export const useThemeStore = create<ThemeStore>()(
    persist(
        (set) => ({
            // 默认浅色：画布挂在 Portal 的蓝白界面里，深色会显得割裂。
            theme: "light",
            setTheme: (theme) => set({ theme }),
        }),
        {
            name: "infinite-canvas:theme_store",
            // v1：PR #7 全站浅色化后，组件按浅色硬编码；存量用户的 dark
            // 持久化状态会让继承文字（--foreground=浅色）落在浅色面板上隐身。
            // 一次性迁移：旧状态一律回浅色。
            version: 1,
            migrate: () => ({ theme: "light" as ThemeName }),
        },
    ),
);
