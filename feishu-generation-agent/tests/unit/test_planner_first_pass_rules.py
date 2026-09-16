"""#4 首轮质量：把踩过的坑沉淀成硬规则。

这三条都是「第一版漏写、审核或返工才补回来」的易错点，对应真实返工记录：

- 运动类：用户返工「不要让红色衣服老头跑出去…停在原地」——第一版没写清
  哪个主体动、哪个主体不动，模型就让所有人都动了起来。
- 屏幕类：手机藏在石膏里，第一版没写清屏幕可见性，模型给屏幕编了内容，
  而屏幕画面是要后期接的。
- 眼神类：用户返工「眼睛坚毅但没让你发光啊」——「眼神坚毅」被模型放大成了
  发光特效。

契约是发给规划模型的 system prompt，改了就要让第一次规划直接写死这些点，
而不是等人工在审批页发现。这里锁住它们，防止后续编辑无意删掉。
"""

from feishu_generation_agent.integrations.planner import planner_system_prompt


def test_contract_pins_who_moves_and_who_stays_put() -> None:
    prompt = planner_system_prompt()

    assert "谁动" in prompt
    assert "谁不动" in prompt
    assert "停在原地" in prompt
    # 只写禁止项不够，必须同时给出正向的位置描述。
    assert "位移状态" in prompt


def test_contract_forbids_inventing_screen_content() -> None:
    prompt = planner_system_prompt()

    assert "屏幕内容不可见" in prompt
    assert "背面或边框" in prompt


def test_contract_forbids_turning_gaze_into_glow() -> None:
    prompt = planner_system_prompt()

    assert "眼神" in prompt
    assert "禁止使用「发光」" in prompt
    assert "眉眼神态" in prompt


def test_contract_states_the_general_rule_behind_all_three() -> None:
    """三条特例背后是同一条通则：容易被模型放大的描述要显式写禁止项。"""

    prompt = planner_system_prompt()

    assert "显式禁止项" in prompt
    assert "negative_constraints" in prompt