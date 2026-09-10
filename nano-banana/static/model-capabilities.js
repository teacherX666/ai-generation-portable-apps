/* Unified per-model capability helper. Shared across nano-banana / seedance / volcengine-portrait.
 *
 * Canonical shape (models[].capabilities in providers.json):
 * {
 *   resolution: ["480p","720p"],            // video / portrait
 *   ratio:      ["16:9","9:16"],            // video / portrait
 *   duration:   { min: 4, max: 15, allow_auto: true }, // video / portrait
 *   image_size:   ["1K","2K","4K"],         // image
 *   aspect_ratio: ["1:1","4:3"],            // image
 *   max_reference_images: 14,
 *   supports_seed: true
 * }
 *
 * Legacy keys (duration_range / resolutions / ratios / maxDuration) are normalized
 * into the canonical shape so old config keeps working without migration.
 */
(function () {
  'use strict';

  function norm(model) {
    if (!model || typeof model !== 'object') return null;
    var cap = model.capabilities || {};
    var dur = cap.duration;
    if (!dur && Array.isArray(model.duration_range) && model.duration_range.length === 2) {
      dur = { min: Number(model.duration_range[0]), max: Number(model.duration_range[1]), allow_auto: true };
    } else if (!dur && model.maxDuration != null) {
      dur = { min: 1, max: Number(model.maxDuration), allow_auto: true };
    }
    return {
      resolution: cap.resolution || (Array.isArray(model.resolutions) ? model.resolutions : null),
      ratio: cap.ratio || (Array.isArray(model.ratios) ? model.ratios : null),
      duration: dur || null,
      image_size: cap.image_size || (Array.isArray(model.image_size) ? model.image_size : null),
      aspect_ratio: cap.aspect_ratio || (Array.isArray(model.aspect_ratios) ? model.aspect_ratios : null),
      max_reference_images: cap.max_reference_images != null ? cap.max_reference_images : (model.max_reference_images != null ? model.max_reference_images : null),
      supports_seed: cap.supports_seed != null ? cap.supports_seed : (model.supports_seed != null ? model.supports_seed : true),
    };
  }

  function capabilitiesFor(providers, provider, modelId) {
    var p = (providers && providers[provider]) || {};
    var models = Array.isArray(p.models) ? p.models : [];
    var m = models.find(function (x) { return x && (x.id === modelId || x.id === modelId); }) || {};
    var c = norm(m);
    if (!c) return null;
    if (!c.image_size && Array.isArray(p.image_size_options)) c.image_size = p.image_size_options.slice();
    if (c.max_reference_images == null && p.max_reference_images != null) c.max_reference_images = p.max_reference_images;
    if (c.supports_seed === true && p.supports_seed === false) c.supports_seed = false;
    return c;
  }

  function inList(list, value) {
    return Array.isArray(list) && list.indexOf(String(value == null ? '' : value)) >= 0;
  }

  // Return { ok, value, message } for a single field.
  function constrain(cap, field, value, label) {
    label = label || field;
    if (field === 'duration') {
      if (!cap.duration) return { ok: true, value: value };
      var n = Number(value);
      if (Number.isNaN(n)) return { ok: false, value: value, message: label + ' 必须是数字' };
      if (n === -1 && cap.duration.allow_auto !== false) return { ok: true, value: -1 };
      var min = Number(cap.duration.min);
      var max = Number(cap.duration.max);
      if (n < min || n > max) {
        return { ok: false, value: Math.min(max, Math.max(min, n)), message: label + ' 仅支持 ' + min + '-' + max + ' 秒' };
      }
      return { ok: true, value: n };
    }
    var opts = null;
    if (field === 'resolution') opts = cap.resolution;
    else if (field === 'ratio') opts = cap.ratio;
    else if (field === 'image_size') opts = cap.image_size;
    else if (field === 'aspect_ratio') opts = cap.aspect_ratio;
    if (!opts) return { ok: true, value: value };
    if (value === 'auto' || value === 'adaptive') return { ok: true, value: value };
    if (inList(opts, value)) return { ok: true, value: value };
    return { ok: false, value: opts[0], message: label + ' 不支持「' + value + '」，可选：' + opts.join(' / ') };
  }

  window.ModelCapabilities = {
    capabilitiesFor: capabilitiesFor,
    constrain: constrain,
  };
})();