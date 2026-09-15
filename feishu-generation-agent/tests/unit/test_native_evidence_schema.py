import json

from feishu_generation_agent.domain.document import (
    NormalizedDocument,
    SourceType,
    TranscriptLine,
    VideoEvidence,
    VideoReferenceKind,
    VideoShot,
)


def _evidence() -> VideoEvidence:
    return VideoEvidence(
        asset_id="asset-video-1",
        engine_id="gemini_native",
        schema_version="native_v1",
        duration=18.08,
        shots=[
            VideoShot(
                start=0.0,
                end=2.8,
                shot_size="中景",
                action="女子跪在棺材边哭喊",
                camera="固定",
            )
        ],
        transcript=[TranscriptLine(t=11.2, text="Oh, Grandma?")],
        audio=["背景音乐", "环境音"],
        on_screen_text=[],
        representative_timestamp=2.8,
        summary="女子哭丧后棺盖被打开",
        kind=VideoReferenceKind.SCENE_STYLE,
        uncertainties=[],
    )


def test_video_evidence_round_trips_json():
    payload = _evidence().model_dump(mode="json")
    restored = VideoEvidence.model_validate(payload)
    assert restored == _evidence()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["duration"] == 18.08


def test_video_evidence_defaults_to_empty_on_normalized_document():
    document = NormalizedDocument(
        document_id="doc-1",
        title="t",
        revision=1,
        source_type=SourceType.DOCX,
        source_token="doc-1",
        blocks=[],
        text_view="",
        media_assets=[],
    )
    assert document.video_evidence == []
