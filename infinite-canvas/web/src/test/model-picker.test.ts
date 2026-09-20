import { describe, expect, it } from "vitest";

import { firstSelectableModel, modelOptionDisplayName, parameterControls } from "@/components/model-picker";
import type { ModelSpec } from "@/api/contracts";


describe("parameterControls", () => {
    it("turns an x-ark-size string property into a preset control", () => {
        const controls = parameterControls({
            type: "object",
            properties: {
                size: {
                    type: "string",
                    default: "2K",
                    title: "尺寸档位",
                    "x-ark-size": { presets: ["1K", "1.5K", "2K"], min_pixels: 921600, max_pixels: 4624220, min_ratio: 0.0625, max_ratio: 16 },
                },
            },
            additionalProperties: false,
        });
        expect(controls).toEqual([{ name: "size", type: "preset", required: false, presets: ["1K", "1.5K", "2K"], default: "2K", title: "尺寸档位" }]);
    });

    it("falls back to a plain string control when x-ark-size presets are malformed", () => {
        const controls = parameterControls({
            type: "object",
            properties: { size: { type: "string", "x-ark-size": { presets: [] } } },
            additionalProperties: false,
        });
        expect(controls).toEqual([{ name: "size", type: "string", required: false }]);
    });

    it("keeps enum controls for the ratio property", () => {
        const controls = parameterControls({
            type: "object",
            properties: { ratio: { type: "string", enum: ["1:1", "16:9"], default: "1:1", title: "比例" } },
            additionalProperties: false,
        });
        expect(controls[0]).toMatchObject({ name: "ratio", type: "enum", default: "1:1", title: "比例" });
    });
});

describe("model availability", () => {
    const base: ModelSpec = {
        model_id: "enabled", service_id: "demo", display_name: "Enabled",
        operations: ["image.generate"], input_media: ["text"], parameter_schema: {},
    };

    it("selects the first enabled model when disabled entries are present", () => {
        const disabled = { ...base, model_id: "disabled", display_name: "Disabled", disabled: true };
        expect(firstSelectableModel([disabled, base])).toBe(base);
    });

    it("labels disabled model options with the shared failure suffix", () => {
        expect(modelOptionDisplayName({ ...base, model_id: "disabled", disabled: true })).toBe("Enabled（已失效）");
    });
});