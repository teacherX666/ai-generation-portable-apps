"""Local AI Port image provider (Qwen-Image-Edit-2511 via local ComfyUI).

AI Port exposes an HTTP job API in front of a local ComfyUI instance:

* submit:  POST /api/image_local/jobs/json -> {job_id, status_url}
* poll:    GET  /api/image_local/jobs/{job_id} -> {status, results}
* download GET  /api/download/{token}

This provider maps an agent ``GenerationTask`` onto that contract and returns
downloaded bytes as ``ProviderResult(base64_data=...)`` so the local result can
be staged by the standard materialization path without a shared staging store.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from feishu_generation_agent.domain.artifact import (
    ProviderResult,
    ProviderSubmission,
)
from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import GenerationTask, TaskType


_DEFAULT_PROVIDER_NAME = "aiport"
_DEFAULT_MODEL_KIND = "qwen2511"
_DEFAULT_BASE_URL = "http://127.0.0.1:8801"
_DEFAULT_MAX_RESULT_BYTES = 32 * 1024 * 1024
_MAX_REFERENCE_IMAGES = 3

_PENDING_JOB_STATUSES = frozenset({"pending", "running", "submitted", "cancelling"})
_CANCELLED_JOB_STATUSES = frozenset({"cancelled", "canceled", "interrupted"})
_SUCCESS_JOB_STATUSES = frozenset({"succeeded", "success", "partial"})

_RESULT_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_DEFAULT_RESULT_MIME = "image/png"

# qwen2511 retains the source aspect ratio and uses width*height only as the
# target pixel area, so a square side is sufficient to express the size token.
_IMAGE_SIZE_SIDES = {"1k": 1024, "1.5k": 1536, "2k": 2048}


class AiPortImageGenerator:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str | None,
        model_kind: str = _DEFAULT_MODEL_KIND,
        provider_name: str = _DEFAULT_PROVIDER_NAME,
        extra_values: dict[str, Any] | None = None,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
    ) -> None:
        self._base_url = self._normalize_base_url(base_url)
        self._model_kind = (model_kind or _DEFAULT_MODEL_KIND).strip()
        self._provider_name = provider_name
        self._extra_values = dict(extra_values or {})
        if not self._model_kind:
            raise self._configuration_error("model_kind", "cause=empty")
        if not isinstance(max_result_bytes, int) or max_result_bytes <= 0:
            raise self._configuration_error("max_result_bytes", "cause=invalid")
        self._max_result_bytes = max_result_bytes
        self._http_client = http_client

    async def submit(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
        *,
        submission_id: str | None = None,
    ) -> ProviderSubmission:
        del submission_id
        if task.task_type is not TaskType.IMAGE_TO_IMAGE:
            raise self._provider_error(
                "AI Port 只支持图生图任务",
                f"operation=generate; task_type={task.task_type.value}",
            )

        payload = self._build_payload(task, assets)
        body = await self._request_json(
            "POST",
            "/api/image_local/jobs/json",
            json_body=payload,
            operation="generate",
        )
        job_id = body.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise self._provider_error(
                "AI Port 未返回任务 ID",
                "operation=generate; cause=missing_job_id",
            )
        return ProviderSubmission(
            provider=self._provider_name,
            provider_task_id=job_id,
            status="submitted",
        )

    async def poll(self, submission: ProviderSubmission) -> ProviderSubmission:
        if submission.provider != self._provider_name:
            raise self._validation_error(
                "AI Port 任务身份不一致",
                f"operation=poll; provider={submission.provider}",
            )
        job = await self._get_job(submission.provider_task_id)
        status = str(job.get("status") or "").strip().lower()
        if status in _PENDING_JOB_STATUSES:
            return self._submission(submission, status="running")
        if status in _CANCELLED_JOB_STATUSES:
            return self._submission(submission, status="cancelled")
        if status in _SUCCESS_JOB_STATUSES:
            results = job.get("results") or []
            if status == "partial":
                return self._submission(
                    submission,
                    status="failed",
                    error_message="AI Port 部分结果生成失败",
                )
            items: list[ProviderResult] = []
            for result in results:
                if not isinstance(result, dict) or not result.get("ok"):
                    return self._submission(
                        submission,
                        status="failed",
                        error_message="AI Port 存在失败结果",
                    )
                items.append(await self._materialize_result(result))
            return ProviderSubmission(
                provider=self._provider_name,
                provider_task_id=submission.provider_task_id,
                status="succeeded",
                result_items=items,
            )
        error = str(job.get("error") or "生成失败")
        return self._submission(submission, status="failed", error_message=error)

    def _build_payload(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
    ) -> dict[str, Any]:
        by_id = {asset.asset_id: asset for asset in assets}
        files: dict[str, Any] = {}
        ordered = sorted(task.reference_images, key=lambda item: item.order)
        if len(ordered) > _MAX_REFERENCE_IMAGES:
            raise self._provider_error(
                f"AI Port 最多支持 {_MAX_REFERENCE_IMAGES} 张参考图",
                f"operation=generate; reference_count={len(ordered)}",
            )
        for index, reference in enumerate(ordered, start=1):
            asset = by_id.get(reference.asset_id)
            if asset is None:
                raise self._provider_error(
                    "AI Port 参考图缺失",
                    f"operation=generate; asset_id={reference.asset_id}",
                )
            try:
                content = asset.local_path.read_bytes()
            except OSError as error:
                raise self._provider_error(
                    "AI Port 参考图读取失败",
                    f"operation=generate; asset_id={reference.asset_id}",
                ) from error
            encoded = base64.b64encode(content).decode("ascii")
            files[f"image_{index}"] = {
                "data_url": f"data:{asset.mime_type};base64,{encoded}",
                "filename": asset.local_path.name or f"reference-{index}.png",
            }

        side = self._image_side(task.image_size)
        values: dict[str, Any] = {
            "model_kind": self._model_kind,
            "prompt": task.prompt,
            "width": side,
            "height": side,
            "repeat_count": task.output_count,
        }
        values.update(self._extra_values)
        return {"values": values, "files": files}

    async def _materialize_result(self, result: dict[str, Any]) -> ProviderResult:
        download_url = result.get("download_url")
        if not isinstance(download_url, str) or not download_url.strip():
            raise self._provider_error(
                "AI Port 结果缺少下载地址",
                "operation=poll; cause=missing_download_url",
            )
        content = await self._download(self._absolute_url(download_url))
        mime_type = self._result_mime(result)
        return ProviderResult(
            base64_data=base64.b64encode(content).decode("ascii"),
            mime_type=mime_type,
        )

    async def _get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/api/image_local/jobs/{job_id}",
            json_body=None,
            operation="poll",
        )

    async def _download(self, url: str) -> bytes:
        try:
            response = await self._http_client.get(url)
        except httpx.HTTPError as error:
            raise self._transient_error(
                "AI Port 下载失败",
                f"operation=download; url={url}",
            ) from error
        if response.status_code != 200:
            raise self._http_error("download", response.status_code)
        content = response.content
        if len(content) > self._max_result_bytes:
            raise self._provider_error(
                "AI Port 结果超过大小限制",
                f"operation=download; size={len(content)}",
            )
        return content

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None,
        operation: str,
    ) -> dict[str, Any]:
        url = self._absolute_url(path)
        try:
            if method == "POST":
                response = await self._http_client.post(url, json=json_body)
            else:
                response = await self._http_client.get(url)
        except httpx.HTTPError as error:
            raise self._transient_error(
                "AI Port 服务不可用",
                f"operation={operation}; url={url}",
            ) from error
        if response.status_code == 404:
            raise self._transient_error(
                "AI Port 任务暂不可用",
                f"operation={operation}; status=404",
            )
        if response.status_code != 200:
            raise self._http_error(operation, response.status_code)
        try:
            body = response.json()
        except ValueError as error:
            raise self._provider_error(
                "AI Port 返回了无效 JSON",
                f"operation={operation}; cause=invalid_json",
            ) from error
        if not isinstance(body, dict):
            raise self._provider_error(
                "AI Port 返回结构无效",
                f"operation={operation}; cause=not_object",
            )
        return body

    @staticmethod
    def _image_side(image_size: str | None) -> int:
        token = (image_size or "2k").strip().lower()
        return _IMAGE_SIZE_SIDES.get(token, 2048)

    @staticmethod
    def _result_mime(result: dict[str, Any]) -> str:
        filename = result.get("filename") or result.get("local_path") or ""
        suffix = Path(str(filename)).suffix.lower()
        return _RESULT_MIME_BY_EXT.get(suffix, _DEFAULT_RESULT_MIME)

    @staticmethod
    def _normalize_base_url(base_url: str | None) -> str:
        raw = (base_url or _DEFAULT_BASE_URL).strip().rstrip("/")
        if not raw:
            raise ValueError("base_url 不能为空")
        try:
            parsed = urlsplit(raw)
        except ValueError as error:
            raise ValueError("base_url 无效") from error
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url 必须是 http(s) origin")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    def _absolute_url(self, path_or_url: str) -> str:
        value = str(path_or_url or "").strip()
        if value.startswith(("http://", "https://")):
            return value
        return f"{self._base_url}{value if value.startswith('/') else '/' + value}"

    def _submission(
        self,
        submission: ProviderSubmission,
        *,
        status: str,
        error_message: str | None = None,
    ) -> ProviderSubmission:
        return ProviderSubmission(
            provider=self._provider_name,
            provider_task_id=submission.provider_task_id,
            status=status,
            error_message=error_message,
        )

    @staticmethod
    def _configuration_error(field_name: str, cause: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.CONFIGURATION,
                message="AI Port 配置无效",
                technical_detail=f"field={field_name}; {cause}",
                retryable=False,
            )
        )

    @staticmethod
    def _provider_error(message: str, technical_detail: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message=message,
                technical_detail=technical_detail,
                retryable=False,
            )
        )

    @staticmethod
    def _validation_error(message: str, technical_detail: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.VALIDATION,
                message=message,
                technical_detail=technical_detail,
                retryable=False,
            )
        )

    @staticmethod
    def _transient_error(message: str, technical_detail: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.TRANSIENT,
                message=message,
                technical_detail=technical_detail,
                retryable=True,
            )
        )

    @staticmethod
    def _http_error(operation: str, status_code: int) -> AgentError:
        if status_code in {429, 500, 502, 503, 504}:
            return AiPortImageGenerator._transient_error(
                "AI Port 服务暂时不可用，请稍后重试",
                f"operation={operation}; status={status_code}",
            )
        return AiPortImageGenerator._provider_error(
            "AI Port 拒绝了请求",
            f"operation={operation}; status={status_code}",
        )