/* Display aids use only pixels from an explicitly requested, fresh preview frame. */
window.AutopilotWorkflows = {
  healthRows(health, origin) {
    const finite = value => Number.isFinite(value);
    const batteries = Array.isArray(health.battery) ? health.battery : [];
    const temperatures = Array.isArray(health.temperature) ? health.temperature : [];
    const orientation = health.orientation;
    const radio = health.radio;
    const supportedBands = radio?.probe?.supported_bands || radio?.supported_bands;
    return [
      ['Controller', origin],
      ['Platform', health.platform || 'Unavailable'],
      ['Free storage', finite(health.free_bytes) ? (health.free_bytes / 1024 ** 3).toFixed(1) + ' GiB' : 'Unavailable'],
      ['Battery', batteries.length ? batteries.map(item => `${item.name || 'Battery'}: ${finite(item.percent) ? item.percent + '%' : 'level unavailable'}${item.status ? ' · ' + item.status : ''}`).join('; ') : 'Unavailable — no battery telemetry'],
      ['Temperature', temperatures.length ? temperatures.map(item => `${item.name || 'Sensor'}: ${finite(item.celsius) ? item.celsius.toFixed(1) + ' °C' : 'unavailable'}`).join('; ') : 'Unavailable — no temperature telemetry'],
      ['Level / orientation', orientation?.available === true && finite(orientation.roll_deg) && finite(orientation.pitch_deg)
        ? `Roll ${orientation.roll_deg.toFixed(1)}° · pitch ${orientation.pitch_deg.toFixed(1)}° · ${orientation.source || 'source unspecified'}${orientation.calibrated === true ? ' · calibrated' : ' · calibration unverified'}`
        : 'Unavailable — no measured orientation. The screen does not assume the camera is level.'],
      ['Radio capabilities', Array.isArray(supportedBands) && supportedBands.length ? supportedBands.join(', ') + ' (reported supported bands)' : 'Unavailable — no supported band report'],
      ['Configured radio band', radio?.configuration?.status==='configured_only' ? radio.configuration.band+' · configuration candidate only' : Array.isArray(supportedBands) && supportedBands.includes(radio.configured_band) ? radio.configured_band : 'Unavailable'],
      ['Access point', radio?.operating_verified === true ? (radio.operating === true ? 'Operating, verified by controller' : 'Not operating, verified by controller') : 'Operation unverified'],
      ['Physical endurance', health.physical_endurance_verified === true ? 'Verified in supplied evidence' : 'Not verified by these telemetry readings']
    ];
  },
  async downloadDevelopment(apiFetch, path) {
    if (!/^\/development\/[a-f0-9]{32}\/(linear\.tiff|output\.dng)$/.test(path)) throw new Error('Invalid development artifact path.');
    const filename = path.split('/').pop();
    const handle = window.showSaveFilePicker ? await window.showSaveFilePicker({suggestedName: filename}) : null;
    const response = await apiFetch(path);
    if (!response.ok) throw new Error('The saved development artifact is unavailable.');
    if (handle) {
      const output = await handle.createWritable();
      await response.body.pipeTo(output);
      return;
    }
    const limit = 256 * 1024 * 1024;
    if (Number(response.headers.get('Content-Length')) > limit) {
      await response.body.cancel();
      throw new Error('This output requires a desktop browser with streaming file downloads.');
    }
    const reader = response.body.getReader(), chunks = [];
    let bytes = 0;
    while (true) {
      const item = await reader.read();
      if (item.done) break;
      bytes += item.value.length;
      if (bytes > limit) {await reader.cancel();throw new Error('Development artifact exceeds the browser download budget.');}
      chunks.push(item.value);
    }
    const url = URL.createObjectURL(new Blob(chunks, {type: response.headers.get('Content-Type') || 'application/octet-stream'}));
    const link = document.createElement('a');link.href = url;link.download = filename;link.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  },
  analyzePixels(pixels) {
    const histogram = Array(256).fill(0);
    let clipped = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      const value = Math.round(.2126 * pixels[i] + .7152 * pixels[i + 1] + .0722 * pixels[i + 2]);
      histogram[value]++;
      if (Math.max(pixels[i], pixels[i + 1], pixels[i + 2]) >= 250) clipped++;
    }
    return {histogram, clippedPercent: pixels.length ? clipped * 400 / pixels.length : 0};
  },
  // Rule-of-thirds lines, or nothing. Exact fractions live here, not in copy.
  thirdsGridFractions() { return [1 / 3, 2 / 3]; },
  // `scale` is device pixels per CSS pixel, so stripes keep a 12 CSS px period on dense screens.
  zebraAt(x, y, r, g, b, threshold = 250, scale = 1) {
    const period = Math.max(2, Math.round(12 * scale));
    return Math.max(r, g, b) >= threshold && ((x + y) % period) < period / 2;
  },
  // Backing-store pixels per CSS pixel for a canvas: the screen's devicePixelRatio, capped at 3
  // (a 390x844 box at 3x is already ~3 MP of getImageData per repaint).
  canvasPixelRatio(ratio) {
    const value = Number(ratio);
    return Number.isFinite(value) && value > 1 ? Math.min(value, 3) : 1;
  },
  // Total backing-store budget for a contained preview. paintPreview also allocates the pixel read,
  // the overlay and a scratch layer at the photo's size, so a large HiDPI viewport (3840x2160 CSS
  // at 2x = 33 MP) must not follow the ratio. 4 MP keeps a 390x844 box at a full 3x (3.0 MP).
  PREVIEW_MAX_PIXELS: 4000000,
  // Backing-store pixels per CSS pixel for a box: the capped screen ratio, lowered (below 1 if
  // needed) until the canvas fits PREVIEW_MAX_PIXELS.
  boxPixelRatio(boxWidth, boxHeight, ratio) {
    const wanted = this.canvasPixelRatio(ratio);
    const area = boxWidth * boxHeight;
    return area > 0 ? Math.min(wanted, Math.sqrt(this.PREVIEW_MAX_PIXELS / area)) : wanted;
  },
  displaySize(frame, maxEdge) {
    const sourceWidth = Number(frame && frame.naturalWidth) || 0;
    const sourceHeight = Number(frame && frame.naturalHeight) || 0;
    if (!(sourceWidth > 0 && sourceHeight > 0)) return {width: 0, height: 0, scale: 1};
    const longest = Math.max(sourceWidth, sourceHeight);
    const cap = Number(maxEdge);
    const scale = Number.isFinite(cap) && cap > 0 && longest > cap ? cap / longest : 1;
    return {width: Math.max(1, Math.round(sourceWidth * scale)), height: Math.max(1, Math.round(sourceHeight * scale)), scale};
  },
  // Letterbox a photo into a canvas box. Overlays must use this rect, not the full canvas.
  containedDrawRect(boxWidth, boxHeight, sourceWidth, sourceHeight) {
    const boxW = Number(boxWidth) || 0;
    const boxH = Number(boxHeight) || 0;
    const srcW = Number(sourceWidth) || 0;
    const srcH = Number(sourceHeight) || 0;
    if (!(boxW > 0 && boxH > 0 && srcW > 0 && srcH > 0)) return {x: 0, y: 0, width: 0, height: 0, scale: 0};
    const scale = Math.min(boxW / srcW, boxH / srcH);
    const width = Math.max(1, Math.round(srcW * scale));
    const height = Math.max(1, Math.round(srcH * scale));
    return {x: Math.round((boxW - width) / 2), y: Math.round((boxH - height) / 2), width, height, scale};
  },
  lastFrameAidsCaption(measured) {
    const width = measured && measured.width, height = measured && measured.height;
    const clipped = Number(measured && measured.clippedPercent);
    const size = Number.isFinite(width) && Number.isFinite(height) ? `${width} × ${height}` : 'size unknown';
    const clip = Number.isFinite(clipped) ? `${clipped.toFixed(1)}% near-clipped JPEG pixels` : 'clipping unmeasured';
    return `Last captured frame — not a live view. ${size} · ${clip}. Histogram and zebra are from this JPEG copy, not RAW headroom.`;
  },
  cardFileViewable(name) {
    return /\.(jpe?g|png)$/i.test(String(name || ''));
  },
  paintPreview(canvas, histogramCanvas, frame, options = {}) {
    const size = this.displaySize(frame, options.maxEdge);
    const boxWidth = Number(options.boxWidth);
    const boxHeight = Number(options.boxHeight);
    const fitContain = options.fit === 'contain' && boxWidth > 0 && boxHeight > 0;
    // A contained canvas keeps its CSS box (stylesheet) and gets a device-pixel backing store, so
    // a 3x phone is not shown a 1x bitmap upscaled. Histogram and zebra read these device pixels.
    // Within PREVIEW_MAX_PIXELS; a larger box gets fewer canvas pixels than device pixels.
    const ratio = fitContain ? this.boxPixelRatio(boxWidth, boxHeight, options.pixelRatio) : 1;
    let dest;
    if (fitContain) {
      canvas.width = Math.max(1, Math.floor(boxWidth * ratio + 1e-9));
      canvas.height = Math.max(1, Math.floor(boxHeight * ratio + 1e-9));
      dest = this.containedDrawRect(canvas.width, canvas.height, frame.naturalWidth, frame.naturalHeight);
    } else {
      canvas.width = size.width;
      canvas.height = size.height;
      dest = {x: 0, y: 0, width: size.width, height: size.height, scale: size.scale};
    }
    const context = canvas.getContext('2d', {willReadFrequently: true});
    context.fillStyle = '#000';
    context.fillRect(0, 0, canvas.width, canvas.height);
    if (!(dest.width > 0 && dest.height > 0)) {
      return {histogram: Array(256).fill(0), clippedPercent: 0, dest};
    }
    context.drawImage(frame, dest.x, dest.y, dest.width, dest.height);
    const pixels = context.getImageData(dest.x, dest.y, dest.width, dest.height);
    const measured = this.analyzePixels(pixels.data);
    const overlay = context.createImageData(dest.width, dest.height);
    const width = dest.width;
    for (let y = 1; y < dest.height - 1; y++) {
      for (let x = 1; x < width - 1; x++) {
        const i = (y * width + x) * 4;
        const p = pixels.data;
        if (options.zebra && this.zebraAt(x, y, p[i], p[i + 1], p[i + 2], 250, ratio)) {
          overlay.data.set([255, 80, 80, 190], i);
        } else if (options.peaking) {
          const horizontal = Math.max(Math.abs(p[i - 4] - p[i + 4]), Math.abs(p[i - 3] - p[i + 5]), Math.abs(p[i - 2] - p[i + 6]));
          const vertical = Math.max(Math.abs(p[i - width * 4] - p[i + width * 4]), Math.abs(p[i - width * 4 + 1] - p[i + width * 4 + 1]), Math.abs(p[i - width * 4 + 2] - p[i + width * 4 + 2]));
          if (horizontal + vertical > 90) overlay.data.set([80, 255, 100, 200], i);
        }
      }
    }
    const layer = document.createElement('canvas');
    layer.width = dest.width; layer.height = dest.height;
    layer.getContext('2d').putImageData(overlay, 0, 0);
    context.drawImage(layer, dest.x, dest.y);
    if (options.grid) {
      context.save();
      context.beginPath();
      context.rect(dest.x, dest.y, dest.width, dest.height);
      context.clip();
      // Contained (device-pixel) canvas: one device pixel on a pixel centre, so the line is crisp.
      // A natural-size canvas is scaled by CSS, so its line width follows the image instead.
      context.strokeStyle = '#ffffffaa'; context.lineWidth = fitContain ? 1 : Math.max(1, dest.width / 500);
      const snap = value => fitContain ? Math.floor(value) + 0.5 : value;
      context.beginPath();
      this.thirdsGridFractions().forEach(fraction => {
        const gx = dest.x + snap(dest.width * fraction);
        const gy = dest.y + snap(dest.height * fraction);
        context.moveTo(gx, dest.y); context.lineTo(gx, dest.y + dest.height);
        context.moveTo(dest.x, gy); context.lineTo(dest.x + dest.width, gy);
      });
      context.stroke();
      context.restore();
    }
    const graph = histogramCanvas.getContext('2d');
    // 256 bins across the histogram's own device-pixel width (256 x 80 when not scaled).
    const graphWidth = ratio > 1 && histogramCanvas.clientWidth > 0 ? Math.round(histogramCanvas.clientWidth * ratio) : 256;
    const graphHeight = Math.round(80 * ratio);
    histogramCanvas.width = graphWidth; histogramCanvas.height = graphHeight;
    graph.fillStyle = '#15151a'; graph.fillRect(0, 0, graphWidth, graphHeight);
    graph.fillStyle = '#e8b84b';
    const peak = Math.max(1, ...measured.histogram);
    measured.histogram.forEach((count, bin) => {
      const left = Math.floor(bin * graphWidth / 256), right = Math.floor((bin + 1) * graphWidth / 256);
      graph.fillRect(left, graphHeight - graphHeight * count / peak, right - left, graphHeight * count / peak);
    });
    return Object.assign(measured, {dest, pixelRatio: ratio});
  },
  paintLoupe(canvas, frame, point = {x: .5, y: .5}) {
    canvas.width = 240; canvas.height = 180;
    const width = Math.min(frame.naturalWidth, 240);
    const height = Math.min(frame.naturalHeight, 180);
    const x = Math.max(0, Math.min(frame.naturalWidth - width, point.x * frame.naturalWidth - width / 2));
    const y = Math.max(0, Math.min(frame.naturalHeight - height, point.y * frame.naturalHeight - height / 2));
    canvas.getContext('2d').drawImage(frame, x, y, width, height, 0, 0, 240, 180);
  },
  // Server-rendered mood previews. The image name travels as a query parameter; the token
  // rides the authenticated header (apiFetch), never a URL. Returns {previews, recipes} where
  // each preview is a self-contained data: URL, so no second per-swatch image request is made.
  async fetchMoodPreviews(apiFetch, imageName) {
    const res = await apiFetch('/moods/preview?image=' + encodeURIComponent(imageName));
    if (!res.ok) throw new Error('Mood previews unavailable for this shot.');
    const data = await res.json();
    return {previews: data.previews || {}, recipes: data.recipes || {}};
  },
  // A stack policy and refinement cannot combine: the backend rejects stack+refine, because the
  // camera cannot both stack fixed frames and re-meter a critique loop. This decides which control
  // to disable and the one-line reason to show.
  // The body keeps whatever exposure the last /shoot left on it. A computational session starts
  // from that without metering; a Fixed stack meters first ('metered') unless the owner keeps the
  // body's exposure ('current'). Say which, and show the last values this app knows.
  inheritedBaselineText(baseline, stackBaseline) {
    const metered = stackBaseline === 'metered';
    const lead = metered
      ? 'A Fixed stack meters first: one meter photo (up to 2 more if it reads black or white) sets the exposure the frames bracket around. The exposure on the body now is not used; the stack restores it afterwards.'
      : stackBaseline === 'current'
        ? 'Keep current exposure: the Fixed stack starts from whatever exposure is on the body now — it does not meter first.'
        : 'A computational session starts from whatever exposure is on the body now — it does not meter first.';
    const settings = baseline && baseline.settings && typeof baseline.settings === 'object' ? baseline.settings : null;
    if (!settings) {
      return lead + (metered
        ? ' This app has not taken a photo yet, so the exposure on the body is unknown here.'
        : ' This app has not taken a photo yet, so the ISO, aperture and shutter it will inherit are unknown here; check the camera before starting.');
    }
    const known = [['ISO', settings.iso], ['aperture', settings.aperture], ['shutter', settings.shutter]]
      .filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== '')
      .map(([label, value]) => `${label} ${value}`);
    const when = Number.isFinite(baseline.ts) ? new Date(baseline.ts).toLocaleTimeString() : null;
    const readback = baseline.readback === 'verified' ? 'readback verified' : 'readback not verified';
    const values = known.length ? known.join(' · ') : 'no exposure values were reported';
    return lead + ` The last photo from this app${when ? ' at ' + when : ''} left ${values} (${readback}).` +
      (metered ? '' : ' Anything changed on the camera since then is the real starting point.');
  },
  // A computational session meters first when Start is pressed (POST /sessions/meter-plan), unless
  // the owner keeps the body's exposure ('current'); a Night Assist plan carries its own exposure.
  sessionBaselineText(baseline, sessionBaseline, nightAssist) {
    if (nightAssist) {
      return 'Night Assist: every frame uses the planned exposure, metered from your last shot. No meter photo; the session restores the camera\'s settings afterwards.';
    }
    if (sessionBaseline === 'current') return this.inheritedBaselineText(baseline);
    const lead = 'Start meters first: one meter photo (up to 2 more if it reads black or white) sets the starting exposure — the first timelapse frame, or the exposure a bracket or stack is built around. The camera\'s exposure is put back after the meter photo, and the session restores its settings afterwards.';
    const settings = baseline && baseline.settings && typeof baseline.settings === 'object' ? baseline.settings : null;
    if (!settings) return lead + ' This app has not taken a photo yet, so the exposure on the body is unknown here.';
    const known = [['ISO', settings.iso], ['aperture', settings.aperture], ['shutter', settings.shutter]]
      .filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== '')
      .map(([label, value]) => `${label} ${value}`);
    return lead + ` The last photo from this app left ${known.length ? known.join(' · ') : 'no exposure values'}.`;
  },
  // The session exposure budget shown before Start; the meter photo counts.
  sessionBudgetText(frames, sessionBaseline, nightAssist, delaySeconds) {
    if (nightAssist) return `Session: ${frames} physical photos at the Night Assist exposure. No meter photo.`;
    if (sessionBaseline === 'current') {
      return `Session: ${frames} physical photos, starting from the camera's current exposure. No meter photo.`;
    }
    const budget = `Session: up to ${frames + 3} physical photos — 1 meter photo (up to 2 more if it reads black or white), then ${frames} session frames from the metered exposure.`;
    // The meter photo is taken when Start is pressed, so it cannot describe a session that starts later.
    return delaySeconds > 0
      ? budget + ' Meter first needs Start delay 0: a delayed session would be metered now, not when it starts. Set the delay to 0 or keep the camera\'s current exposure.'
      : budget;
  },
  // A session created with start settings: where they came from.
  sessionStartText(record) {
    if (!record || typeof record !== 'object' || !record.start_settings) return '';
    const exposure = values => values && typeof values === 'object'
      ? ['ISO ' + values.iso, values.aperture, values.shutter].join(' · ') : 'unknown';
    if (record.source === 'meter_plan') {
      return `Started from the metered exposure ${exposure(record.start_settings)} (the body held ${exposure(record.inherited)} before metering).`;
    }
    return `Started from the requested exposure ${exposure(record.start_settings)}.`;
  },
  // Which stack controls apply: the baseline choice and its text describe a Fixed stack only
  // (Auto follows its own ordinary photo and never sends a baseline).
  stackPanelVisibility(policy) {
    return {settings: policy === 'auto' || policy === 'fixed', fixedBaseline: policy === 'fixed'};
  },
  // The Fixed stack exposure budget shown before capture; the meter frame counts.
  fixedStackBudgetText(frames, stackBaseline) {
    return stackBaseline === 'current'
      ? `Fixed stack: ${frames} physical photos from the camera's current exposure. No meter photo.`
      : `Fixed stack: up to ${frames + 3} physical photos — 1 meter photo (up to 2 more if it reads black or white), then ${frames} stack frames around the metered exposure.`;
  },
  // After a Fixed stack: which exposure it bracketed around, and what the body held before.
  stackBaselineOutcomeText(baseline) {
    if (!baseline || typeof baseline !== 'object') return '';
    const exposure = values => values && typeof values === 'object'
      ? ['ISO ' + values.iso, values.aperture, values.shutter].join(' · ') : 'unknown';
    if (baseline.source === 'metered') {
      return `Baseline metered: ${exposure(baseline.metered)} (the body held ${exposure(baseline.inherited)} before metering).`;
    }
    if (baseline.source === 'inherited') {
      return `Baseline kept from the body: ${exposure(baseline.inherited)}. No meter photo.`;
    }
    return '';
  },
  stackRefineExclusivity(stackPolicy, refineChecked) {
    const stackOn = Boolean(stackPolicy) && stackPolicy !== 'off';
    return {
      stackOn,
      refine: stackOn ? false : Boolean(refineChecked),
      refineDisabled: stackOn,
      note: stackOn
        ? 'Refine is off while a stack policy is selected — the camera cannot both stack and re-meter.'
        : (refineChecked ? 'Refine on: up to 4 exposures (meter + final, at most one improvement attempt).' : '')
    };
  },
  // Compact "critique → adjust → reshoot" trail rows from a shot's evidence. Returns an array of
  // plain row objects; when refinement was disabled a single "off" row is returned.
  critiqueTrailRows(evidence) {
    const refinement = evidence?.refinement || {};
    const trail = Array.isArray(evidence?.critique_trail) ? evidence.critique_trail : [];
    if (refinement.enabled === false || !trail.length) {
      return [{disabled: true, text: 'Refine was off (1 exposure)'}];
    }
    return trail.map(entry => {
      const critique = entry.critique || {};
      const correction = entry.correction_applied || entry.correction || null;
      const mean = Number.isFinite(critique.mean_brightness) ? Math.round(critique.mean_brightness) : null;
      const bits = [];
      if (critique.verdict) bits.push(String(critique.verdict));
      if (mean !== null) bits.push('mean ' + mean);
      if (Array.isArray(critique.issues) && critique.issues.length) bits.push(critique.issues.join(', '));
      const adjust = correction && (correction.reason || Number.isFinite(correction.delta))
        ? 'adjust ' + (Number.isFinite(correction.delta) ? (correction.delta > 0 ? '+' : '') + correction.delta + ' EV ' : '') + (correction.reason || '')
        : (entry.stop_reason ? 'stop: ' + entry.stop_reason : 'delivered');
      return {
        disabled: false,
        index: entry.attempt_index,
        target: entry.target_brightness_requested,
        critique: bits.join(' · ') || 'not critiqued',
        adjust: adjust.trim(),
        stopReason: entry.stop_reason || null
      };
    });
  },
  // Human summary of an incremental panorama preview: frame count, coverage and per-frame overlap.
  panoramaPreviewSummary(preview) {
    if (!preview) return '';
    const parts = ['Panorama preview: ' + (preview.frame_count ?? '?') + ' frames'];
    const receipt = preview.receipt || {};
    const coverage = receipt.coverage?.valid_fraction;
    if (Number.isFinite(coverage)) parts.push((coverage * 100).toFixed(0) + '% coverage');
    const overlaps = (Array.isArray(receipt.alignment) ? receipt.alignment : [])
      .map(a => a.estimated_overlap_fraction).filter(Number.isFinite);
    if (overlaps.length) {
      parts.push('overlap ' + (Math.min(...overlaps) * 100).toFixed(0) + '–' + (Math.max(...overlaps) * 100).toFixed(0) + '%');
    }
    const output = receipt.output;
    if (output?.width && output?.height) parts.push(output.width + '×' + output.height);
    return parts.join(' · ') + '. Overlap and coverage are measured from the accepted frames.';
  },
  // Human summary of a live crowd clean-plate preview: frames so far and the share already clear.
  crowdPreviewSummary(preview) {
    if (!preview) return '';
    const cleared = preview.cleared_share;
    if (!Number.isFinite(cleared)) return '';
    const pct = value => (value * 100).toFixed(1).replace(/\.0$/, '') + '%';
    const floor = preview.visibility_floor_frames;
    return 'Crowd preview from ' + (preview.accepted_frames ?? '?') + ' frames: ' + pct(cleared) + ' clear, '
      + pct(1 - cleared) + ' not yet cleared (tinted)'
      + (Number.isFinite(floor) ? '; a spot counts as clear once its background shows in ' + floor + '+ frames (25% rule)' : '')
      + '. Measured on a small copy; the final merge reports its own share. Anyone who never moves stays in the photo.'
      + this.crowdRegionsNote(preview.regions);
  },
  // The automatic stop's spatial gate: every spot someone crossed must be seen clear again.
  crowdRegionsNote(regions) {
    if (!regions || typeof regions.all_seen_clear !== 'boolean') return '';
    if (regions.all_seen_clear) return ' Every spot people crossed has been seen clear again.';
    const share = Number.isFinite(regions.pending_share)
      ? (regions.pending_share * 100).toFixed(1).replace(/\.0$/, '') + '% of the frame' : 'part of the frame';
    return ' The automatic stop waits: ' + share + ' that someone crossed or sat in has not been seen clear '
      + 'in the newest frames yet.';
  },
  // Describe how a crowd session ended early, if it did.
  crowdStopSummary(stopped) {
    if (!stopped) return '';
    const how = stopped.reason === 'cleared_share_threshold'
      ? 'automatically at ' + Math.round((stopped.threshold || 0) * 100) + '% clear'
      : 'when you pressed Stop';
    return 'Stopped ' + how + ' after ' + stopped.accepted_frames + ' of ' + stopped.requested_frames
      + ' frames; only those frames were merged.';
  },
  // A timelapse blocked by an ambiguous frame may encode its good frames as a partial movie
  // (owner decision 2026-10-02). Offered only then, never while one is made or after it exists.
  partialMovieOffer(state) {
    if (!state || state.mode !== 'timelapse' || state.status !== 'needs_reconciliation') return null;
    if (['completed', 'queued', 'running'].includes(state.partial_video?.status)) return null;
    const count = (state.frames || []).filter(frame => frame.status === 'accepted').length;
    return count >= 2 ? {count} : null;
  },
  partialMovieSummary(partial) {
    if (!partial) return '';
    if (partial.status === 'completed')
      return 'Partial movie made from ' + partial.frame_count + ' good frames; the ambiguous frame is left out and nothing was shot.';
    if (['queued', 'running'].includes(partial.status))
      return 'Partial movie ' + partial.status + ' in the processing service; no camera is used.';
    if (partial.status === 'unavailable')
      return 'Partial movie unavailable: ' + (partial.reason || 'no working encoder') + ' The saved frames remain.';
    if (partial.status === 'failed')
      return 'Partial movie failed: ' + (partial.error || 'unknown error') + '. The saved frames remain; you can try again.';
    return '';
  },
  // POST /sessions and POST /sessions/{id}/jobs carry `host_camera_warning` while an ImageCapture
  // client (Preview, Photos, Image Capture) runs (#129). The server's sentence, verbatim; '' when absent.
  cameraClientWarning(response) {
    const line = response ? response.host_camera_warning : null;
    return typeof line === 'string' && line.trim() ? line : '';
  },
  // Identity of one published crowd preview: a different session or a new snapshot never
  // matches, so the page refetches exactly when the image it shows is not this one.
  crowdPreviewKey(sessionId, preview) {
    return preview && preview.sha256 ? sessionId + ':' + preview.sha256 : '';
  },
  async crowdPreviewObjectUrl(apiFetch, sessionId) {
    const res = await apiFetch('/sessions/' + encodeURIComponent(sessionId) + '/crowd-preview');
    if (!res.ok) throw new Error('No crowd preview yet.');
    return URL.createObjectURL(await res.blob());
  },
  async panoramaPreviewObjectUrl(apiFetch, sessionId) {
    const res = await apiFetch('/sessions/' + encodeURIComponent(sessionId) + '/panorama-preview');
    if (!res.ok) throw new Error('No panorama preview yet.');
    return URL.createObjectURL(await res.blob());
  },
  // Handheld (body-shutter) session: the host never fires the shutter.
  async startHandheld(apiFetch, body) {
    const res = await apiFetch('/handheld', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (!res.ok) throw new Error('Handheld session could not start.');
    return res.json();
  },
  async handheldState(apiFetch, id) {
    const res = await apiFetch('/handheld/' + encodeURIComponent(id));
    if (!res.ok) throw new Error('Handheld status unavailable.');
    return res.json();
  },
  async cancelHandheld(apiFetch, id) {
    const res = await apiFetch('/handheld/' + encodeURIComponent(id) + '/cancel', {method: 'POST'});
    if (!res.ok) throw new Error('Handheld cancel was not confirmed.');
    return res.json();
  },
  handheldTerminal(status) {
    return status === 'stopped' || status === 'error';
  },
  // Night assist: pick the newest saved shot (gallery is newest-first) and ask the server to plan a
  // night stack from it. The image name travels in the JSON body; the token rides apiFetch headers.
  async nightAssistPlan(apiFetch, camera) {
    const gallery = await apiFetch('/gallery');
    if (!gallery.ok) throw new Error('Could not read the gallery to pick a reference shot.');
    const shots = await gallery.json();
    const latest = Array.isArray(shots) && shots.length ? shots[0].image : null;
    if (!latest) throw new Error('No saved shot yet — take one photo first.');
    const image = String(latest).split('/').pop();
    const res = await apiFetch('/night-assist/plan', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({image, camera_model: camera.camera_model, camera_port: camera.camera_port,
        camera_serial: camera.camera_serial || null})
    });
    if (!res.ok) throw new Error('Night assist could not plan from this shot.');
    return {image, plan: await res.json()};
  }
};
