/** 平台感知的快捷键提示文本（右键菜单展示用）。 */

export const isMacPlatform = () => typeof navigator !== "undefined" && /mac/i.test(navigator.platform || "");

export const undoShortcutLabel = () => (isMacPlatform() ? "⌘Z" : "Ctrl+Z");

export const redoShortcutLabel = () => (isMacPlatform() ? "⇧⌘Z" : "Ctrl+Shift+Z");
