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
            // 存量用户的 dark 持久化主题一次性迁移回浅色（浅色化后组件按浅色
            // 硬编码，暗色下继承文字会落在浅色面板上隐身——theme-migration.test）
            version: 1,
            migrate: () => ({ theme: "light" as ThemeName }),
        },
    ),
);
