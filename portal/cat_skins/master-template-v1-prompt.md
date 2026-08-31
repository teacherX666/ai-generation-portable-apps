# Master Template V1 — AI 坐标设计契约

此文件是后续视觉模型/多模态模型的输入契约。最终权威数据为 `master-template-v1.json`，经典黑猫母版为 `classic-black-v1.json`。

## 职责边界

程序先固定并记录：

- `seed`
- `rarity`
- `pattern_family`
- `base_fur_role`
- `secondary_fur_role`
- `accent_role`
- `pattern_density`
- `symmetry_bias`
- `accessory_family`

AI只负责：

- 在 `pattern_allowed=true` 的已有猫像素中安排具体花纹；
- 在本次稀有度允许的 `overlay_zones` 中安排装饰；
- 返回坐标操作，不返回PNG，不返回完整256字符矩阵；
- 不得创造未被模板授权的基础像素。

## 修改规则

- `locked_occupancy`：不得删除、移动或变透明；只能使用该部位允许的颜色角色。
- `color_only`：不得改变占用状态，只能改变颜色角色。
- `patternable`：不得改变占用状态，可在主毛色、副毛色、点缀毛色之间选择。
- `shape_optional`：只有服务端明确开放高稀有轮廓变化时才能增删；否则按 `patternable` 处理。
- `transparent_only`：基础猫层必须保持透明；仅当坐标属于本次获准的装饰区时，装饰层才能使用。
- `final_face_foreground=true`：眼睛、瞳孔、鼻子、嘴和下巴最后重绘，任何面罩或头饰都不能遮掉。

## 建议请求结构

```json
{
  "template_id": "classic-black-master-v1",
  "seed": 123456,
  "rarity": "rare",
  "design_gene": {
    "pattern_family": "calico",
    "base_fur_role": "cream",
    "secondary_fur_role": "orange",
    "accent_role": "dark_brown",
    "pattern_density": "medium",
    "symmetry_bias": "controlled_asymmetry",
    "accessory_family": "none"
  },
  "editable_cells": "从 master-template-v1.json 提取本次允许的坐标",
  "forbidden_cells": "从 master-template-v1.json 提取 locked/face_foreground 坐标"
}
```

## AI唯一允许的响应结构

```json
{
  "name_suggestion": "三花示例猫",
  "palette_roles": {
    "outline": "#2A211D",
    "fur_base": "#F4E1C1",
    "fur_secondary": "#D97732",
    "fur_accent": "#4A332B",
    "iris": "#62C9D9",
    "pupil": "#171717",
    "nose": "#D96B79"
  },
  "pattern_operations": [
    {"x": 8, "y": 3, "role": "fur_secondary"},
    {"x": 9, "y": 3, "role": "fur_secondary"}
  ],
  "accessory_operations": [],
  "floating_regions": [],
  "design_notes": ["脸部橘色块与背部深色块形成受控不对称"]
}
```

## 服务端必须拒绝

- 坐标不在0..15；
- 重复坐标给出冲突颜色角色；
- 花纹操作落在非 `pattern_allowed` 格；
- 普通/稀有皮肤返回大型装饰；
- 装饰超出对应 `overlay_zones.allowed`；
- 翅膀未连接背部或与尾巴共用像素；
- 非法悬浮像素；
- 超过稀有度色板上限；
- 任何导致经典五官、下巴、臀部或脚底关键点消失的操作。

## 第一轮固定测试

1. 普通橘色条纹猫：无装饰，只验证条纹连续和方向。
2. 稀有三花猫：无大型装饰，只验证2–4个连通色块和受控不对称。
3. 史诗天使猫：验证左上翅膀、顶部光环、图层覆盖与五官前景重绘。
