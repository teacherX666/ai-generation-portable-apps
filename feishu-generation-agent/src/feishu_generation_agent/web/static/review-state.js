(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ReviewState = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const CONFLICT_MESSAGE = "服务端计划已更新，请确认/刷新";

  function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function stableValue(value) {
    if (Array.isArray(value)) return value.map(stableValue);
    if (value && typeof value === "object") {
      return Object.keys(value).sort().reduce((result, key) => {
        result[key] = stableValue(value[key]);
        return result;
      }, {});
    }
    return value;
  }

  function serverIdentity(view) {
    const approval = view?.approval || {};
    return JSON.stringify(stableValue({
      status: view?.status || null,
      revision: approval.revision ?? null,
      tasks: approval.tasks || [],
      document_summary: approval.document_summary || "",
      media_assets: approval.media_assets || [],
      excluded_assets: approval.excluded_assets || [],
      coverage: approval.coverage || null,
      ingest_issue_records: approval.ingest_issue_records || [],
      ingest_issues: approval.ingest_issues || [],
      blocking_ingest_issues: approval.blocking_ingest_issues || [],
      asset_ingest_issues: approval.asset_ingest_issues || [],
      vision_issues: approval.vision_issues || [],
      selected_task_ids: approval.selected_task_ids || [],
    }));
  }

  function taskIds(view) {
    return (view?.approval?.tasks || [])
      .map((task) => task?.task_id)
      .filter((taskId) => typeof taskId === "string");
  }

  function initialSelection(view) {
    const known = new Set(taskIds(view));
    const serverSelected = Array.isArray(view?.approval?.selected_task_ids)
      ? view.approval.selected_task_ids.filter((taskId) => known.has(taskId))
      : [];
    if (view?.status === "waiting_approval" && serverSelected.length === 0) {
      return [...known];
    }
    return serverSelected;
  }

  function createReviewState() {
    return {
      serverView: null,
      serverIdentity: null,
      editsByTaskId: {},
      selectedTaskIds: [],
      selectionDirty: false,
      conflict: "",
      pendingServerView: null,
      submitting: false,
      submitSnapshot: null,
    };
  }

  function adoptServerView(view) {
    const serverView = clone(view);
    return {
      ...createReviewState(),
      serverView,
      serverIdentity: serverIdentity(serverView),
      selectedTaskIds: initialSelection(serverView),
    };
  }

  function hasDirty(state) {
    return Boolean(state.selectionDirty || Object.keys(state.editsByTaskId).length);
  }

  function taskById(view, taskId) {
    return (view?.approval?.tasks || []).find((task) => task?.task_id === taskId);
  }

  function localEditSyncState(field, localValue, serverValue) {
    if (field === "prompt") {
      if (typeof localValue !== "string" || typeof serverValue !== "string") {
        return localValue === serverValue ? "synced" : "different";
      }
      const local = localValue.trim();
      const server = serverValue.trim();
      if (!local && !server) return "synced";
      if (local === server || (local && server.includes(local))) return "synced";
      if (server && local.startsWith(server)) return "pending";
      return "different";
    }
    return JSON.stringify(stableValue(localValue)) === JSON.stringify(stableValue(serverValue))
      ? "synced"
      : "different";
  }

  function reconcileEditsByTaskId(state, incoming) {
    const incomingTaskIds = new Set(taskIds(incoming));
    const reconciled = {};
    let changed = false;

    for (const [taskId, patch] of Object.entries(state.editsByTaskId || {})) {
      const serverTask = incomingTaskIds.has(taskId) ? taskById(incoming, taskId) : null;
      const remaining = {};
      for (const [field, localValue] of Object.entries(patch || {})) {
        const syncState = (
          serverTask
          && Object.prototype.hasOwnProperty.call(serverTask, field)
        ) ? localEditSyncState(field, localValue, serverTask[field]) : "different";
        if (syncState === "synced") {
          changed = true;
        } else if (syncState === "pending") {
          changed = true;
          remaining[field] = localValue;
        } else {
          remaining[field] = localValue;
        }
      }
      if (Object.keys(remaining).length) {
        reconciled[taskId] = remaining;
      } else {
        changed = true;
      }
    }

    return { editsByTaskId: reconciled, changed };
  }

  function mergeServerView(state, view) {
    if (!state.serverView) return adoptServerView(view);
    const incoming = clone(view);
    const incomingIdentity = serverIdentity(incoming);
    if (incomingIdentity === state.serverIdentity) {
      // 页面在规划阶段就打开时，进入等待审批时选择列表还是空的。
      // 此时用户没动过勾选就默认全选，避免「批准所选任务」因空选择被拒。
      const reachedApproval = (
        incoming?.status === "waiting_approval"
        && (state.serverView?.status || "") !== "waiting_approval"
      );
      const shouldAutoSelect = (
        reachedApproval
        && !state.selectionDirty
        && state.selectedTaskIds.length === 0
      );
      return {
        ...state,
        serverView: incoming,
        ...(shouldAutoSelect
          ? { selectedTaskIds: taskIds(incoming) }
          : {}),
      };
    }
    const reconciled = reconcileEditsByTaskId(state, incoming);
    if (Object.keys(state.editsByTaskId || {}).length && reconciled.changed) {
      return {
        ...state,
        serverView: incoming,
        serverIdentity: incomingIdentity,
        editsByTaskId: reconciled.editsByTaskId,
        selectedTaskIds: state.selectionDirty
          ? [...state.selectedTaskIds]
          : initialSelection(incoming),
        conflict: "",
        pendingServerView: null,
      };
    }
    if (hasDirty(state) || state.submitting) {
      return {
        ...state,
        conflict: CONFLICT_MESSAGE,
        pendingServerView: incoming,
      };
    }
    return adoptServerView(incoming);
  }

  function assertEditable(state) {
    if (state.submitting) throw new Error("审批提交中，不能继续修改");
  }

  function setTaskSelected(state, taskId, selected) {
    assertEditable(state);
    if (!taskIds(state.serverView).includes(taskId)) {
      throw new Error(`未知任务：${taskId}`);
    }
    const selectedIds = new Set(state.selectedTaskIds);
    if (selected) selectedIds.add(taskId);
    else selectedIds.delete(taskId);
    return {
      ...state,
      selectedTaskIds: taskIds(state.serverView).filter((id) => selectedIds.has(id)),
      selectionDirty: true,
    };
  }

  function patchTask(state, taskId, patch) {
    assertEditable(state);
    if (!taskIds(state.serverView).includes(taskId)) {
      throw new Error(`未知任务：${taskId}`);
    }
    const safePatch = clone(patch || {});
    delete safePatch.task_id;
    return {
      ...state,
      editsByTaskId: {
        ...state.editsByTaskId,
        [taskId]: {
          ...(state.editsByTaskId[taskId] || {}),
          ...safePatch,
        },
      },
    };
  }

  function setReferenceMode(state, taskId, referenceMode) {
    assertEditable(state);
    if (!taskIds(state.serverView).includes(taskId)) {
      throw new Error(`未知任务：${taskId}`);
    }
    if (!['multi_reference', 'first_last_frame'].includes(referenceMode)) {
      throw new Error('参考模式无效');
    }
    const task = draftTasks(state).find((item) => item.task_id === taskId);
    if (!task) throw new Error(`未知任务：${taskId}`);
    const references = [...(task.reference_images || [])]
      .sort((left, right) => left.order - right.order);
    if (referenceMode === 'first_last_frame') {
      if (task.task_type !== 'image_to_video') {
        throw new Error('图生图只能使用多参考模式');
      }
      if (references.length !== 2) {
        throw new Error('首尾帧模式需要恰好两张图片');
      }
      if (references.some((reference) => reference.role === 'reference_video' || reference.role === 'reference_audio')) {
        throw new Error('首尾帧模式不支持参考视频或参考音频');
      }
      return patchTask(state, taskId, {
        reference_mode: referenceMode,
        reference_images: references.map((reference, index) => ({
          ...reference,
          role: index === 0 ? 'first_frame' : 'last_frame',
        })),
      });
    }
    return patchTask(state, taskId, {
      reference_mode: referenceMode,
      reference_images: references.map((reference) => ({
        ...reference,
        role: reference.role === 'reference_video' || reference.role === 'reference_audio'
          ? reference.role
          : 'reference_image',
      })),
    });
  }

  function draftTasks(state) {
    return (state.serverView?.approval?.tasks || []).map((task) => ({
      ...clone(task),
      ...clone(state.editsByTaskId[task.task_id] || {}),
      task_id: task.task_id,
    }));
  }

  function draftView(state) {
    if (!state.serverView) return null;
    const view = clone(state.serverView);
    view.approval = view.approval || {};
    view.approval.tasks = draftTasks(state);
    view.approval.selected_task_ids = [...state.selectedTaskIds];
    return view;
  }

  function selectedTaskIds(state) {
    return [...state.selectedTaskIds];
  }

  function assetCoverage(view) {
    const approval = view?.approval || {};
    const successfulIds = new Set(
      (approval.media_assets || [])
        .filter((asset) => asset?.download_failed !== true)
        .map((asset) => asset?.asset_id)
        .filter((assetId) => typeof assetId === "string"),
    );
    const failedIds = new Set(
      (approval.media_assets || [])
        .filter((asset) => asset?.download_failed === true)
        .map((asset) => asset?.asset_id)
        .filter((assetId) => typeof assetId === "string"),
    );
    const referencedIds = new Set(
      (approval.tasks || []).flatMap((task) => (
        (task?.reference_images || []).map((reference) => reference?.asset_id)
      )).filter((assetId) => successfulIds.has(assetId)),
    );
    const excludedIds = new Set(
      (approval.excluded_assets || [])
        .map((item) => item?.asset_id)
        .filter((assetId) => (
          successfulIds.has(assetId) && !referencedIds.has(assetId)
        )),
    );
    return {
      successful_total: successfulIds.size,
      referenced_count: referencedIds.size,
      excluded_count: excludedIds.size,
      uncovered_count: [...successfulIds].filter((assetId) => (
        !referencedIds.has(assetId) && !excludedIds.has(assetId)
      )).length,
      failed_count: failedIds.size,
    };
  }

  function coverageLabel(view) {
    const coverage = assetCoverage(view);
    return `已使用 ${coverage.referenced_count} / 共 ${coverage.successful_total} 张`;
  }

  function mediaKind(mimeType) {
    if (typeof mimeType !== "string") return "file";
    if (mimeType.startsWith("image/")) return "image";
    if (mimeType.startsWith("video/")) return "video";
    if (mimeType.startsWith("audio/")) return "audio";
    return "file";
  }

  function excludedAssetRows(view) {
    const approval = view?.approval || {};
    const referencedIds = new Set(
      (approval.tasks || []).flatMap((task) => (
        (task?.reference_images || []).map((reference) => reference?.asset_id)
      )),
    );
    const assets = new Map(
      (approval.media_assets || []).map((asset) => [asset?.asset_id, asset]),
    );
    return (approval.excluded_assets || [])
      .filter((item) => !referencedIds.has(item?.asset_id))
      .map((item) => {
        const asset = assets.get(item.asset_id);
        const mimeType = asset?.mime_type || "";
        return {
          asset_id: item.asset_id,
          reason: item.reason,
          preview_url: asset?.preview_url || null,
          mime_type: mimeType,
          media_kind: mediaKind(mimeType),
        };
      });
  }

  /**
   * **没被任何任务引用、也没被排除**的素材。
   *
   * 覆盖门要求「每个素材要么被引用、要么被排除」，而审批页过去没有排除入口 ——
   * 未使用的素材只能靠手改提示词绕开（2026-09-17 超级大床：批准按钮一直灰着）。
   * 这里把它们列出来给用户一个「排除」按钮。
   */
  function uncoveredAssetRows(view) {
    const approval = view?.approval || {};
    const successful = (approval.media_assets || []).filter(
      (asset) => asset?.download_failed !== true,
    );
    const referencedIds = new Set(
      (approval.tasks || []).flatMap((task) => (
        (task?.reference_images || []).map((reference) => reference?.asset_id)
      )),
    );
    const excludedIds = new Set(
      (approval.excluded_assets || []).map((item) => item?.asset_id),
    );
    // 视频抽帧素材（video-1-frame）算它原视频的别名：引用帧就等于引用了视频。
    const isCovered = (assetId) => {
      if (referencedIds.has(assetId) || excludedIds.has(assetId)) return true;
      if (!String(assetId).endsWith("-frame")) return false;
      const base = String(assetId).slice(0, -"-frame".length);
      return referencedIds.has(base) || excludedIds.has(base);
    };
    return successful
      .filter((asset) => !isCovered(asset.asset_id))
      .map((asset) => ({
        asset_id: asset.asset_id,
        preview_url: asset.preview_url || null,
        mime_type: asset.mime_type || "",
        media_kind: mediaKind(asset.mime_type || ""),
      }));
  }

  function canApprove(state) {
    const view = draftView(state);
    return Boolean(
      state.serverView?.status === "waiting_approval"
      && !state.conflict
      && !state.submitting
      && state.selectedTaskIds.length
      && assetCoverage(view).uncovered_count === 0
    );
  }

  function buildApprovalPayload(state) {
    if (state.conflict) throw new Error(state.conflict);
    if (state.submitting) throw new Error("审批提交中，请勿重复提交");
    if (state.serverView?.status !== "waiting_approval") {
      throw new Error("当前运行不在等待审批状态");
    }
    if (state.selectedTaskIds.length === 0) {
      throw new Error("批准时必须选择至少一个任务");
    }
    if (assetCoverage(draftView(state)).uncovered_count !== 0) {
      throw new Error("素材覆盖不完整，请先处理未使用素材");
    }
    return {
      action: "approve",
      selected_task_ids: [...state.selectedTaskIds],
      tasks: draftTasks(state),
    };
  }

  function deepFreeze(value) {
    if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
    Object.values(value).forEach(deepFreeze);
    return Object.freeze(value);
  }

  function beginApprovalSubmit(state) {
    const payload = deepFreeze(clone(buildApprovalPayload(state)));
    return {
      state: { ...state, submitting: true, submitSnapshot: payload },
      payload,
    };
  }

  function failApprovalSubmit(state) {
    return { ...state, submitting: false, submitSnapshot: null };
  }

  function completeApprovalSubmit(state) {
    if (state.pendingServerView) return adoptServerView(state.pendingServerView);
    return adoptServerView(state.serverView);
  }

  function discardLocalChanges(state) {
    return adoptServerView(state.pendingServerView || state.serverView);
  }

  function conflictMessage(state) {
    return state.conflict || "";
  }

  function isSubmitting(state) {
    return Boolean(state.submitting);
  }

  function canSaveReferences(state, taskId) {
    // 任务字段与参考图都是热修改（改动即时持久化到服务端），本地草稿只是
    // 乐观更新镜像；这里只剩提交中/冲突两种硬阻塞。
    if (state.submitting || state.conflict) return false;
    return true;
  }

  function referenceMutationDirective(state, taskId) {
    if (!hasDirty(state)) return "proceed";
    return canSaveReferences(state, taskId) ? "save_then_proceed" : "blocked";
  }

  function taskEditorIdentity(view) {
    if (!view) return "";
    const approval = view.approval || {};
    return JSON.stringify(stableValue({
      tasks: approval.tasks || [],
      selected_task_ids: approval.selected_task_ids || [],
    }));
  }

  /**
   * 采纳「**自己刚保存的**」服务端视图。
   *
   * 热保存链路：打字（600ms 防抖）→ PATCH 到服务端 → 立刻 poll()。这一步会让服务端
   * 计划"变"一次 —— 如果不认领，下一秒轮询就把它当成**别人改了**：弹
   * 「服务端计划已更新」并重建任务列表，用户正在敲的字和光标一起被冲掉。
   * 用户 2026-09-18：「怎么一直服务端计划更新啊，我改提示词一直被打断」就是这个。
   *
   * 只对齐 `serverView` / `serverIdentity`，**保留**本地草稿与已选任务。
   */
  function adoptSelfSavedView(state, view) {
    if (!state || !state.serverView) return adoptServerView(view);
    return {
      ...state,
      serverView: clone(view),
      serverIdentity: serverIdentity(view),
      conflict: "",
      pendingServerView: null,
    };
  }

  function shouldRefreshTaskEditor(
    previousState,
    nextState,
    hasRenderedTasks,
  ) {
    if (!hasRenderedTasks) return true;
    // 用户有未保存修改时**绝不重建**编辑器 —— 重建会连同输入内容和光标一起冲掉。
    // （旧行为靠 draftView 比对"恰好"覆盖了冲突场景，但代价是用户一敲字就判定为
    // "变了"，配合每秒轮询就变成"改提示词一直被刷新打断"。）
    if (hasDirty(nextState)) return false;
    // 只比**服务端**数据：以前比 draftView（含本地草稿），本地任何改动都会触发重建。
    return taskEditorIdentity(previousState?.serverView)
      !== taskEditorIdentity(nextState?.serverView);
  }

  return {
    CONFLICT_MESSAGE,
    beginApprovalSubmit,
    buildApprovalPayload,
    assetCoverage,
    canApprove,
    canSaveReferences,
    completeApprovalSubmit,
    conflictMessage,
    createReviewState,
    discardLocalChanges,
    draftView,
    excludedAssetRows,
    uncoveredAssetRows,
    failApprovalSubmit,
    hasDirty,
    isSubmitting,
    mergeServerView,
    adoptSelfSavedView,
    patchTask,
    referenceMutationDirective,
    selectedTaskIds,
    shouldRefreshTaskEditor,
    coverageLabel,
    setReferenceMode,
    setTaskSelected,
  };
});
