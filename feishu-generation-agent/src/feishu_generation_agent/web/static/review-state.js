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
      // **run_id 必须在身份里**：用户反复重跑同一条文档时，两条运行的
      // status / revision / tasks 可能完全一样；少了 run_id 就会被判成「服务端视图
      // 没变」→ 继续用上一条运行的本地草稿 → 切换任务记录时看到的是**旧运行的分镜图**
      //（用户 2026-09-23：「我点另外一条任务记录就会出现这个问题」）。
      run_id: view?.run_id || null,
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

  /**
   * 批准 payload 里的任务：分镜关键帧只回传**标量字段**。
   *
   * 视图里的 `storyboard_frames[].candidates` 是带 preview_url 的对象（给界面渲染
   * 用），而任务模型里 `candidates` 是 asset_id 字符串数组 —— 原样回传会校验失败。
   * 候选图服务端自己存着（按 index 合并），这里只需要告诉它用户选了哪张。
   */
  function approvalTask(task) {
    const frames = Array.isArray(task.storyboard_frames)
      ? task.storyboard_frames
      : [];
    if (!frames.length) return task;
    return {
      ...task,
      storyboard_frames: frames.map((frame) => ({
        index: frame.index,
        start: frame.start,
        end: frame.end,
        prompt: frame.prompt || "",
        selected: frame.selected
          || frame.candidates?.find((item) => item.selected)?.asset_id
          || null,
      })),
    };
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
      tasks: draftTasks(state).map(approvalTask),
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

  //: ── 素材编号：界面必须和**提示词里的编号**一致 ───────────────────────────────
  //:
  //: 用户 2026-09-30：「前端图片、视频这种类型不同的素材，可以分别 123 的顺序来，
  //: 图片1 和 视频1 自然区分开了」。这和后端契约本来就一致：规划器拿到的素材目录是
  //: **按类型各从 1 起**编号的（`planner.py` 的「图片、视频、音频按实际提交顺序**分别
  //: 1 起**编号」，提示词里写 `@图片N / @视频N / @音频N`）；而 `order` 字段只是**全局
  //: 提交顺序**，后端要求它恰好是 1..N 连续（`integrations/seedance.py` 的
  //: `order_mismatch` 校验），两者不是一回事。
  //:
  //: 所以：界面显示按类型的号（图片1 / 视频1…），`order` 继续做全局序号 —— 改号时只在
  //: **该类型内部**重排，然后把全局 order 重编成 1..N（类型之间的槽位保持不变）。
  const KIND_OF_ROLE = {
    reference_video: "video",
    reference_audio: "audio",
    first_frame: "first_frame",
    last_frame: "last_frame",
  };
  const KIND_LABEL = { image: "图片", video: "视频", audio: "音频" };

  function referenceKind(reference) {
    return KIND_OF_ROLE[reference?.role] || "image";
  }

  /** asset_id → 界面标签：`图片1` / `图片2` / `视频1` / `音频1` / `首帧` / `尾帧`。 */
  function referenceLabels(references) {
    const labels = new Map();
    const counters = new Map();
    [...(references || [])]
      .sort((left, right) => left.order - right.order)
      .forEach((reference) => {
        const kind = referenceKind(reference);
        if (kind === "first_frame" || kind === "last_frame") {
          labels.set(reference.asset_id, kind === "first_frame" ? "首帧" : "尾帧");
          return;
        }
        const next = (counters.get(kind) || 0) + 1;
        counters.set(kind, next);
        labels.set(reference.asset_id, `${KIND_LABEL[kind]}${next}`);
      });
    return labels;
  }

  /** 该素材在**自己类型里**的序号（1 起）；首帧/尾帧返回 0（不参与编号）。 */
  function referenceTypeIndex(references, assetId) {
    const list = [...(references || [])].sort((left, right) => left.order - right.order);
    const target = list.find((reference) => reference.asset_id === assetId);
    if (!target) return 0;
    const kind = referenceKind(target);
    if (kind === "first_frame" || kind === "last_frame") return 0;
    return list.filter((reference) => referenceKind(reference) === kind)
      .findIndex((reference) => reference.asset_id === assetId) + 1;
  }

  /** 改「同类型内的序号」：在该类型内部重排，再把全局 order 重编成 1..N。
   *
   *  类型之间的**槽位交错保持不变**（图片占它原来的槽位、视频占它原来的槽位），于是：
   *   - 后端要求的 `order == 1..N 连续` 永远成立（不会再报 order_mismatch）；
   *   - 界面上的「图片1 / 视频1」与提示词里的 `@图片1 / @视频1` 一一对应。
   */
  function reorderReferenceType(references, assetId, nextIndex) {
    const list = [...(references || [])].sort((left, right) => left.order - right.order);
    const target = list.find((reference) => reference.asset_id === assetId);
    if (!target) return references;
    const kind = referenceKind(target);
    if (kind === "first_frame" || kind === "last_frame") return references;
    const slots = list
      .map((reference, index) => ({ reference, index }))
      .filter((item) => referenceKind(item.reference) === kind);
    const current = slots.findIndex((item) => item.reference.asset_id === assetId);
    const wanted = Math.min(Math.max(1, Number(nextIndex) || 1), slots.length) - 1;
    if (current < 0 || wanted === current) return references;
    const ordered = slots.map((item) => item.reference);
    const [moved] = ordered.splice(current, 1);
    ordered.splice(wanted, 0, moved);
    const next = [...list];
    slots.forEach((item, index) => {
      next[item.index] = ordered[index];
    });
    return next.map((reference, index) => ({ ...reference, order: index + 1 }));
  }

  return {
    CONFLICT_MESSAGE,
    beginApprovalSubmit,
    referenceKind,
    referenceLabels,
    referenceTypeIndex,
    reorderReferenceType,
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
