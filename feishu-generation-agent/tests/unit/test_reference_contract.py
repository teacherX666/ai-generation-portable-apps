from feishu_generation_agent.domain.plan import ImageReference, SEEDANCE_PROMPT_MAX_CHARS
from feishu_generation_agent.domain.reference_contract import (
    canonicalize_references,
    dangling_reference_mentions,
    duration_consistency_issues,
    reference_removal_leaves_holes,
    reference_tokens,
    remap_prompt_references,
    single_whole_clip_timeline_hint,
    validate_seedance_prompt,
)


def _reference(
    asset_id: str,
    order: int,
    role: str = "reference_image",
) -> ImageReference:
    return ImageReference(asset_id=asset_id, role=role, order=order)


def test_reference_removal_leaves_holes_detects_the_production_hole() -> None:
    """生产实测（刮太空沙，2026-10-08）：摘 `image-1` 会留下 `只以为唯一基准`。

    原文 `成片主体、造型、配色与材质只以@图片1为唯一基准，两者冲突时以@图片1为准`
    —— token 前后都是中文，删掉必然断句。
    """
    mime_types = {"image-1": "image/png", "video-1": "video/mp4"}
    prompt = (
        "@视频1：参考匀速刨削的节奏；成片主体、造型、配色与材质只以"
        "@图片1为唯一基准，两者冲突时以@图片1为准，不要水印。"
    )

    assert reference_removal_leaves_holes(
        prompt,
        [_reference("image-1", 1), _reference("video-1", 2, "reference_video")],
        [_reference("video-1", 2, "reference_video")],
        mime_types,
    )


def test_reference_removal_leaves_holes_false_at_clause_boundaries() -> None:
    """token 落在子句边界（后面是冒号/标点）时删掉不留空洞。"""
    mime_types = {"image-1": "image/png", "image-2": "image/png"}
    prompt = "@图片2：参考桌面。@图片1：参考碗。"

    assert not reference_removal_leaves_holes(
        prompt,
        [_reference("image-1", 1), _reference("image-2", 2)],
        [_reference("image-2", 2)],
        mime_types,
    )


def test_colon_binding_counts_as_a_concrete_reference() -> None:
    """`@图片1：参考…` 是规划器最常写的形式，必须算「具体绑定」。

    2026-10-08 实测：不认冒号 → 整张图被判「只罗列没绑定」→ 被摘下 →
    提示词里留下 `只以为唯一基准`（素材语义的落点被删成空洞）。
    """
    task = {
        "prompt": (
            "@图片1：参考紫白相间条纹太空沙立方体的造型与配色、底部原木托盘。"
            "参考 @视频1：匀速顺滑的刨削节奏与固定机位。"
            "画面稳定不变形、连贯，无水印，不要出现任何 logo。"
        ),
        "reference_images": [
            {"asset_id": "image-1", "role": "reference_image", "order": 1},
            {"asset_id": "video-1", "role": "reference_video", "order": 2},
        ],
    }

    issues = validate_seedance_prompt(
        task,
        {"image-1": "image/png", "video-1": "video/mp4"},
        require_storyboard=True,
    )

    assert not [
        issue
        for issue in issues
        if "必须绑定具体主体" in issue or "缺少素材引用" in issue
    ]


def test_drop_reference_mention_leaves_no_dangling_subject() -> None:
    """删 token 必须把「参考/中/的」这套语法外壳一起收掉，不留 `参考  中…`。"""
    mime_types = {"image-a": "image/png", "image-b": "image/png", "image-c": "image/png"}
    old = [_reference("image-a", 1), _reference("image-b", 2), _reference("image-c", 3)]

    result = remap_prompt_references(
        "@图片1：参考锅；参考 @图片2 中连续动作加速浓缩的连贯节奏；@图片3 的碗",
        old,
        [_reference("image-a", 1), _reference("image-c", 2)],
        mime_types,
    )

    assert dangling_reference_mentions(result) == []
    assert "参考  中" not in result
    assert "参考 @" not in result


def test_canonicalize_references_closes_middle_gap() -> None:
    result = canonicalize_references(
        [
            _reference("asset-a", 1),
            _reference("asset-c", 3),
        ]
    )

    assert [
        (reference.asset_id, reference.order) for reference in result
    ] == [("asset-a", 1), ("asset-c", 2)]


def test_canonicalize_references_rejects_ambiguous_duplicate_order() -> None:
    try:
        canonicalize_references(
            [
                _reference("asset-a", 1),
                _reference("asset-b", 1),
            ]
        )
    except ValueError as exc:
        assert str(exc) == "reference orders must be unique"
    else:
        raise AssertionError("duplicate reference order must be rejected")


def test_reference_tokens_number_each_media_type_separately() -> None:
    references = [
        _reference("image-a", 1),
        _reference("video-a", 2, "reference_video"),
        _reference("image-b", 3),
        _reference("audio-a", 4, "reference_audio"),
    ]
    mime_types = {
        "image-a": "image/png",
        "video-a": "video/mp4",
        "image-b": "image/jpeg",
        "audio-a": "audio/mpeg",
    }

    assert reference_tokens(references, mime_types) == {
        "image-a": "@图片1",
        "video-a": "@视频1",
        "image-b": "@图片2",
        "audio-a": "@音频1",
    }


def test_remap_prompt_references_preserves_surviving_asset_identity() -> None:
    old = [
        _reference("asset-a", 1),
        _reference("asset-b", 2),
        _reference("asset-c", 3),
    ]
    new = [
        _reference("asset-a", 1),
        _reference("asset-c", 2),
    ]
    mime_types = {
        "asset-a": "image/png",
        "asset-b": "image/png",
        "asset-c": "image/png",
    }

    result = remap_prompt_references(
        "@图片1 中的锅；@图片2 中的碗；@图片3 中的桌面",
        old,
        new,
        mime_types,
    )

    assert result == "@图片1 中的锅；碗；@图片2 中的桌面"


def test_remap_prompt_references_avoids_cascading_number_replacement() -> None:
    old = [
        _reference("asset-a", 1),
        _reference("asset-b", 2),
        _reference("asset-c", 3),
        _reference("asset-d", 4),
    ]
    new = [
        _reference("asset-a", 1),
        _reference("asset-c", 2),
        _reference("asset-d", 3),
    ]
    mime_types = {reference.asset_id: "image/png" for reference in old}

    result = remap_prompt_references(
        "参考图3的桌面延续到第4张参考图中的成品",
        old,
        new,
        mime_types,
    )

    assert result == "参考图2的桌面延续到第3张参考图中的成品"


def test_remap_prompt_references_migrates_legacy_gap_by_visible_order() -> None:
    old = [
        _reference("asset-a", 1),
        _reference("asset-c", 3),
    ]
    new = canonicalize_references(old)
    mime_types = {
        "asset-a": "image/png",
        "asset-c": "image/png",
    }

    result = remap_prompt_references(
        "@图片1 中的锅；@图片3 中的桌面",
        old,
        new,
        mime_types,
    )

    assert result == "@图片1 中的锅；@图片2 中的桌面"


def test_remap_prompt_references_rejects_ambiguous_legacy_gap() -> None:
    old = [
        _reference("asset-a", 1),
        _reference("asset-c", 3),
    ]
    new = canonicalize_references(old)
    mime_types = {
        "asset-a": "image/png",
        "asset-c": "image/png",
    }

    try:
        remap_prompt_references(
            "@图片1 中的锅；@图片2 中的食材；@图片3 中的桌面",
            old,
            new,
            mime_types,
        )
    except ValueError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("ambiguous legacy reference gap must be rejected")


def test_remap_prompt_references_rejects_mixed_media_legacy_gap() -> None:
    old = [
        _reference("image-a", 1),
        _reference("video-b", 3, "reference_video"),
        _reference("image-c", 4),
    ]
    new = canonicalize_references(old)
    mime_types = {
        "image-a": "image/png",
        "video-b": "video/mp4",
        "image-c": "image/png",
    }

    try:
        remap_prompt_references(
            "@图片1 中的锅；@视频1 中的旧动作；"
            "@视频2 中的新动作；@图片2 中的桌面",
            old,
            new,
            mime_types,
        )
    except ValueError as exc:
        assert "混合媒体" in str(exc)
    else:
        raise AssertionError("mixed-media legacy gap must be rejected")


def test_remap_prompt_references_supports_cross_media_replacement() -> None:
    old = [
        _reference("image-a", 1),
        _reference("image-b", 2),
    ]
    new = [
        _reference("video-a", 1, "reference_video"),
        _reference("image-b", 2),
    ]
    mime_types = {
        "image-a": "image/png",
        "image-b": "image/png",
        "video-a": "video/mp4",
    }

    result = remap_prompt_references(
        "@图片1 中的开场动作；@图片2 中的角色造型",
        old,
        new,
        mime_types,
        replacement_asset_ids={"image-a": "video-a"},
    )

    assert result == "@视频1 中的开场动作；@图片1 中的角色造型"


def _video_task(
    prompt: str,
    references: list[ImageReference] | None = None,
) -> dict:
    return {
        "task_id": "task-hotpot",
        "task_type": "image_to_video",
        "prompt": prompt,
        "reference_images": [
            reference.model_dump(mode="json")
            for reference in (
                references
                or [
                    _reference("image-pot", 1),
                    _reference("image-food", 2),
                ]
            )
        ],
    }


def test_seedance_prompt_rejects_hotpot_plan_without_reference_binding() -> None:
    issues = validate_seedance_prompt(
        _video_task(
            "0-3秒：展示空锅。3-8秒：食材入锅。"
            "8-12秒：俯拍成品。"
        ),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=True,
    )

    assert any("@图片1" in issue for issue in issues)
    # 2026-09-22：不再要求「每个素材都出现在某个镜头段里」—— 接入 Deepwhite
    # 导演技能后，素材说明是单独一段（写明参考什么/不参考什么），按旧规则
    # 会把合法格式判死。底线改成「素材必须真的出现在 prompt 里」。
    assert not any("镜头 1" in issue and "素材" in issue for issue in issues)
    assert not any("时间轴" in issue for issue in issues)


def test_seedance_multishot_prompt_requires_contiguous_timeline() -> None:
    """多分镜 prompt 必须带整数秒时间轴：从 0 开始、首尾相接。

    2026-09-22：Seedance 2.5 官方提示词指南「用时间戳或镜头 N 切分，逐段描述」，
    时间戳是 2.5 最主要的新增控制手段；旧契约「禁止绝对秒数」已废除。
    """
    references = {"image-pot": "image/png", "image-food": "image/png"}
    binding = (
        "参考 @图片1 中的黄铜毛毡空锅，参考 @图片2 中的毛毡食材。\n"
        "高清，物体稳定不变形，不要生成水印，不要生成 Logo。"
    )

    def issues_for(shots: str) -> list[str]:
        return validate_seedance_prompt(
            _video_task(shots + "\n" + binding),
            references,
            require_storyboard=True,
        )

    # 合法：0 起、相接
    assert not any(
        "时间轴" in issue
        for issue in issues_for(
            "镜头 1（0-2s）：展示 @图片1 中的空锅。\n"
            "镜头 2（2-5s）：@图片2 中的食材落入 @图片1 中的锅。"
        )
    )
    # 完全没写时间轴
    assert any(
        "必须按整数秒时间轴分段" in issue
        for issue in issues_for(
            "镜头 1：展示 @图片1 中的空锅。\n"
            "镜头 2：@图片2 中的食材落入 @图片1 中的锅。"
        )
    )
    # 不从 0 开始
    assert any(
        "必须从 0s 开始" in issue
        for issue in issues_for(
            "镜头 1（1-3s）：展示 @图片1 中的空锅。\n"
            "镜头 2（3-5s）：@图片2 中的食材落入 @图片1 中的锅。"
        )
    )
    # 中间断开（官方明确要避免「0-3 秒…5-6 秒…」）
    assert any(
        "必须首尾相接" in issue
        for issue in issues_for(
            "镜头 1（0-3s）：展示 @图片1 中的空锅。\n"
            "镜头 2（5-8s）：@图片2 中的食材落入 @图片1 中的锅。"
        )
    )


def test_seedance_prompt_accepts_a_separate_material_block() -> None:
    prompt = (
        "参考 @图片1 中的黄铜毛毡空锅；"
        "参考 @图片2 中的毛毡食材盘。\n"
        "镜头 1（0-3s）：固定镜头展示空锅。\n"
        "镜头 2（3-6s）：近景展示食材入锅。\n"
        "高清，物体稳定不变形，不要生成水印，不要生成 Logo。"
    )

    issues = validate_seedance_prompt(
        _video_task(prompt),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=True,
    )

    # 素材说明单独成段 + 各镜头复用素材 = Deepwhite 技能的合法格式。
    assert issues == []


def test_seedance_prompt_rejects_a_material_that_never_appears() -> None:
    """素材一个 token 都没出现在 prompt 里 → 仍然要拦（防整张图凭空丢失）。"""
    prompt = (
        "镜头 1（0-3s）：固定镜头展示空锅。\n"
        "镜头 2（3-6s）：近景展示食材入锅。\n"
        "高清，物体稳定不变形，不要生成水印，不要生成 Logo。"
    )

    issues = validate_seedance_prompt(
        _video_task(prompt),
        # 这个 fixture 带两张参考图（image-pot / image-food），mime 表必须给全，
        # 否则 reference_tokens 直接抛错、根本走不到「素材没出现」这条规则。
        {"image-pot": "image/png", "image-food": "image/png"},
        require_storyboard=True,
    )

    assert any("@图片1" in issue and "完全没有出现" in issue for issue in issues)
    assert any("@图片2" in issue and "完全没有出现" in issue for issue in issues)


def test_seedance_prompt_accepts_equivalent_concrete_binding_phrases() -> None:
    prompt = (
        "镜头 1（0-5s）：使用 @图片1 作为黄铜毛毡空锅的造型参考。\n"
        "镜头 2（5-10s）：参考 @图片2 展示绿色青菜和红色肥牛落入锅中。\n"
        "高清，物体稳定不变形，不要生成水印，不要生成 Logo。"
    )

    assert validate_seedance_prompt(
        _video_task(prompt),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=True,
    ) == []


def test_seedance_prompt_accepts_pure_text_to_video_without_references() -> None:
    prompt = (
        "镜头 1（0-5s）：固定镜头展示空锅。\n"
        "镜头 2（5-10s）：近景展示食材入锅。\n"
        "高清，物体稳定不变形，不要生成水印，不要生成 Logo。"
    )

    issues = validate_seedance_prompt(
        {
            "task_id": "task-pure-text",
            "task_type": "image_to_video",
            "prompt": prompt,
            "reference_images": [],
        },
        {},
        require_storyboard=True,
    )

    assert issues == []


def test_seedance_prompt_rejects_prompt_over_hard_limit() -> None:
    prompt = "表情夸张、动作符合物理逻辑。" * (SEEDANCE_PROMPT_MAX_CHARS // 13 + 1)
    issues = validate_seedance_prompt(
        {"task_id": "task-long", "task_type": "image_to_video", "prompt": prompt, "reference_images": []},
        {},
        require_storyboard=False,
    )

    assert len(issues) == 1
    assert str(SEEDANCE_PROMPT_MAX_CHARS) in issues[0]
    assert str(len(prompt)) in issues[0]


def test_seedance_prompt_accepts_exactly_hard_limit() -> None:
    prompt = "稳" * SEEDANCE_PROMPT_MAX_CHARS
    assert validate_seedance_prompt(
        {"task_id": "task-limit", "task_type": "image_to_video", "prompt": prompt, "reference_images": []},
        {},
        require_storyboard=False,
    ) == []


def test_seedance_prompt_rejects_generic_generation_and_quality_phrases() -> None:
    prompt = (
        "镜头 1：根据 @图片1 生成视频，保持高清稳定不变形。\n"
        "镜头 2：结合 @图片2 完成制作，无水印，无 Logo。"
    )

    issues = validate_seedance_prompt(
        _video_task(prompt),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=True,
    )

    assert any("@图片1" in issue and "具体" in issue for issue in issues)
    assert any("@图片2" in issue and "具体" in issue for issue in issues)


def test_seedance_prompt_rejects_reference_only_action_templates() -> None:
    for phrase in (
        "以 @图片1 为参考",
        "把 @图片1 当作参考",
        "调用 @图片1",
        "读取 @图片1",
    ):
        issues = validate_seedance_prompt(
            _video_task(
                f"{phrase}，根据 @图片2 生成视频。",
            ),
            {
                "image-pot": "image/png",
                "image-food": "image/png",
            },
            require_storyboard=False,
        )

        assert any("@图片1" in issue and "具体" in issue for issue in issues)


def test_seedance_prompt_ignores_generic_words_before_reference_token() -> None:
    for phrase in (
        "请使用 @图片1",
        "仅参考 @图片1",
        "只参考 @图片1",
        "直接使用 @图片1",
        "选择 @图片1",
        "引用 @图片1",
        "通过 @图片1 生成",
    ):
        issues = validate_seedance_prompt(
            _video_task(
                f"{phrase}，根据 @图片2 生成视频。",
            ),
            {
                "image-pot": "image/png",
                "image-food": "image/png",
            },
            require_storyboard=False,
        )

        assert any("@图片1" in issue and "具体" in issue for issue in issues)


def test_seedance_prompt_rejects_generic_suffix_templates() -> None:
    for phrase in (
        "用 @图片1 来生成视频",
        "@图片1 请作为参考",
        "@图片1 优先使用",
        "@图片1 为准",
        "@图片1 进行处理",
    ):
        issues = validate_seedance_prompt(
            _video_task(
                f"{phrase}，根据 @图片2 生成视频。",
            ),
            {
                "image-pot": "image/png",
                "image-food": "image/png",
            },
            require_storyboard=False,
        )

        assert any("@图片1" in issue and "具体" in issue for issue in issues)


def test_seedance_prompt_rejects_generic_reference_descriptions() -> None:
    for phrase in (
        "@图片1 作为主要参考",
        "@图片1 作为核心素材",
        "@图片1 用于最终生成",
        "@图片1 用作辅助参考图",
        "@图片1 保持一致",
        "@图片1 作为重要参考",
    ):
        issues = validate_seedance_prompt(
            _video_task(
                f"{phrase}，参考 @图片2 中的绿色青菜。",
            ),
            {
                "image-pot": "image/png",
                "image-food": "image/png",
            },
            require_storyboard=False,
        )

        assert any("@图片1" in issue and "具体" in issue for issue in issues)


def test_seedance_prompt_preserves_concrete_nouns_containing_generic_words() -> None:
    prompt = (
        "参考 @图片1 中的视频制作人；"
        "参考 @图片2 中的稳定器。"
    )

    assert validate_seedance_prompt(
        _video_task(prompt),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=False,
    ) == []


def test_seedance_prompt_requires_unique_continuous_shot_numbers() -> None:
    prompt = (
        "参考 @图片1 中的黄铜毛毡空锅；"
        "参考 @图片2 中的毛毡食材盘。\n"
        "镜头 1：展示 @图片1 中的黄铜毛毡空锅。\n"
        "镜头 1：展示 @图片2 中的毛毡食材盘。\n"
        "高清，物体稳定不变形，不要生成水印，不要生成 Logo。"
    )

    issues = validate_seedance_prompt(
        _video_task(prompt),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=True,
    )

    assert any("镜头编号" in issue and "1…N" in issue for issue in issues)


def test_seedance_prompt_accepts_parenthesized_shot_duration_labels() -> None:
    # 2026-09-22 起，括号里的时间必须是**区间**（整数秒时间轴），不再是「约 2 秒」。
    prompt = (
        "镜头1（0-2s）：展示 @图片1 中的黄铜毛毡空锅。\n"
        "镜头2（2-4s）：展示 @图片2 中的绿色青菜和红色肥牛。\n"
        "高清，物体稳定不变形，不要生成水印，不要生成 Logo。"
    )

    assert validate_seedance_prompt(
        _video_task(prompt),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=True,
    ) == []


def test_seedance_prompt_accepts_understood_references_in_every_shot() -> None:
    prompt = (
        "参考 @图片1 中的黄铜毛毡空锅；"
        "参考 @图片2 中的毛毡食材盘。\n"
        "镜头 1（0-5s）：固定镜头，展示 @图片1 中的黄铜毛毡空锅。\n"
        "镜头 2（5-10s）：近景，@图片2 中的毛毡食材依次落入 "
        "@图片1 中的黄铜毛毡空锅。\n"
        "高清，物体稳定不变形，不要生成水印，不要生成 Logo。"
    )

    assert validate_seedance_prompt(
        _video_task(prompt),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=True,
    ) == []


def test_seedance_prompt_requires_semantics_for_every_multimodal_token() -> None:
    references = [
        _reference("image-pot", 1),
        _reference("video-motion", 2, "reference_video"),
        _reference("audio-mood", 3, "reference_audio"),
    ]
    prompt = (
        "@图片1，@视频1，@音频1。"
        "画面稳定，不要生成水印，不要生成 Logo。"
    )

    issues = validate_seedance_prompt(
        _video_task(prompt, references),
        {
            "image-pot": "image/png",
            "video-motion": "video/mp4",
            "audio-mood": "audio/mpeg",
        },
        require_storyboard=False,
    )

    assert any("@图片1" in issue and "具体" in issue for issue in issues)
    assert any("@视频1" in issue and "具体" in issue for issue in issues)
    assert any("@音频1" in issue and "具体" in issue for issue in issues)


def test_seedance_prompt_rejects_noncontinuous_order_and_internal_asset_id() -> None:
    references = [
        _reference("asset-pot", 1),
        _reference("asset-food", 3),
    ]
    issues = validate_seedance_prompt(
        _video_task(
            "参考 @图片1 中的毛毡空锅和 @图片2 中的食材，"
            "保持 asset-pot 的构图。",
            references,
        ),
        {
            "asset-pot": "image/png",
            "asset-food": "image/png",
        },
        require_storyboard=False,
    )

    assert any("1…N" in issue for issue in issues)
    assert any("内部素材 ID" in issue for issue in issues)

# --- ②-a（2026-10-10，P0）：时长一致性 ---------------------------------------
#
# 实测根因（REPORT_YARN §4.1 / §6.2 P0-a）：「毛线1」r5 把时长 5→8、只把最显眼的
# 表头「镜头 1（0—5秒）」手改成「0—8秒」，正文「预计5秒」原封不动就提交了 ——
# 应用不校验 → 成片前 4.08 秒画面完全冻结（49.5% 时长没动）。
#
# 下面这段是**真实语料回归集**（`data/checkpoints.sqlite3` 里最近 500 个 run 的
# 511 条 image_to_video 任务，用 `_quality_analysis/yarn-fix/sweep.txt` 逐条人工判读）：
# 命中的 15 条全部是真不一致，没有一条误伤。


def test_duration_consistency_issues_flags_the_production_r5_case() -> None:
    """r5（run 49cd5712）逐字形态：时长 8、正文「预计5秒」。"""
    prompt = (
        "…贴合的 @图片2 图案；正面近景、固定机位、单镜头一镜到底，"
        "预计5秒，突出解压的抽线手感。\n【时间轴（共 8 秒）】0—8秒：…"
    )

    issues = duration_consistency_issues(prompt, 8)

    assert issues, "正文自述 5 秒、任务时长 8 秒，必须报出来"
    assert "5" in issues[0] and "8" in issues[0]


def test_duration_consistency_issues_accepts_varied_writing_styles() -> None:
    """正文的不同写法都要能正确识别（不能因为写法不同就放过或误报）。"""
    for prompt, duration in [
        ("时间轴：0.0-0.5s 静止；0.5-8.0s 抽线；预计8秒", 8),
        ("共 8 秒；0—0.5秒 静止，0.5—8秒 抽线", 8),
        ("总计8秒，整段一气呵成", 8),
        ("全片时长 8 秒", 8),
        ("预计 8s 完成", 8),
        ("镜头 1（0—8秒）：匀速推进，预计8秒。", 8),
    ]:
        assert duration_consistency_issues(prompt, duration) == [], prompt

    # 反过来：写成 10 秒就是不一致（真实语料 run 728df316：时长 6、正文「共 10 秒」）
    assert duration_consistency_issues("【时间轴（共 10 秒）】0.0-0.5s：…", 6)


def test_duration_consistency_issues_does_not_misfire_on_per_shot_duration() -> None:
    """「镜头时长 2 秒」是**分镜级**时长，不是总时长 —— 收进来会误伤正常多段提示词。"""
    prompt = "镜头 1（0-2s）：每段时长 3 秒以内；单镜头时长 2 秒。共 5 秒。"

    assert duration_consistency_issues(prompt, 5) == []
    # 但「共 5 秒」写错成「共 6 秒」仍然要抓
    assert duration_consistency_issues("共 6 秒；每段时长 3 秒", 5)


def test_duration_consistency_issues_ignores_missing_or_invalid_duration() -> None:
    assert duration_consistency_issues("预计5秒", None) == []
    assert duration_consistency_issues("预计5秒", True) == []
    assert duration_consistency_issues(None, 8) == []
    assert duration_consistency_issues("没有任何时长表述", 8) == []


def test_single_whole_clip_timeline_hint_thresholds() -> None:
    """②-a-2：只在「长时长 + 时间轴恰好一整段」时提示，其它一律安静。"""
    # 命中：r5 的形态
    assert single_whole_clip_timeline_hint("镜头 1（0—8秒）：匀速推进。", 8)
    # 时长 < 6：不提示（正常单段短任务，用户口径明确要放过）
    assert single_whole_clip_timeline_hint("镜头 1（0—5秒）：匀速推进。", 5) is None
    # 已经分段：不提示
    assert single_whole_clip_timeline_hint(
        "0-2s：起势；2-6s：推进；6-8s：收尾。", 8
    ) is None
    # 完全没有时间轴：不提示（2026-09-22 的既定口径：单镜头 prompt 不带时间轴也合法）
    assert single_whole_clip_timeline_hint("单镜头一镜到底，匀速推进。", 8) is None
    # 没有秒数区间但提到了两段：仍不算分段
    assert single_whole_clip_timeline_hint("镜头 1（0—6秒）：第一段静止。", 6)
