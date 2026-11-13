/* Shared presentation/media helpers; all image bytes remain on the authenticated path. */
window.Autopilot = {
  // Display-only names for the camera picker. libgphoto2 reports "Sony Alpha-A7 III (PC Control)"
  // for one body and the bare code "Sony ILCE-7M2 (Control)" for the other, so a beginner cannot
  // tell which row is their A7 II. These names are a reading aid only: the exact driver string
  // remains the identity used for selection and gating, because a body that is asleep or not in
  // PC Remote can report the wrong model (.claude/lessons.md, Identification).
  CAMERA_MODEL_NAMES: {
    "Sony ILCE-7M2 (Control)": "Sony A7 II",
    "Sony Alpha-A7 III (PC Control)": "Sony A7 III",
  },
  friendlyCameraName(model) {
    const friendly = this.CAMERA_MODEL_NAMES[model];
    return friendly ? `${friendly} (${model})` : model;
  },
  // Picker grouping only: the controller discovers Sony bodies; anything else the OS lists over PTP
  // (an iPhone plugged into the Mac) is shown under "Other USB devices". Never used for identity.
  isSupportedCameraModel(model) {
    return /^Sony\b/i.test(String(model || ''));
  },

  // Device memory for the phone WebView. The API token and the chosen camera live in
  // localStorage: the WebView drops sessionStorage when the app is killed, and a cold launch
  // must not demand the 48-character token again (same-origin storage on the owner's
  // passcode-locked device). sessionStorage is mirrored so sibling pages in this run still find
  // the token where they look for it. Per-tab state (the active session id) stays in
  // sessionStorage only: a stale session id must not auto-select days later on a restarted
  // server; the sessions list is the recovery path. Every access is guarded because storage
  // throws in private or restricted contexts and must never take a page down. The token is
  // never logged or sent anywhere except the X-Camera-Autopilot-Token header.
  deviceStore: {
    TOKEN_KEY: 'camera-autopilot-token',
    CAMERA_KEY: 'camera-autopilot-selected-camera',
    ACTIVE_SESSION_KEY: 'camera-autopilot-active-session',
    BASELINE_KEY: 'camera-autopilot-body-baseline',
    AUTO_SAVE_PHOTOS_KEY: 'camera-autopilot-auto-save-photos',
    _stores(scope) {
      const stores = [];
      if (scope !== 'tab') { try { if (window.localStorage) stores.push(window.localStorage); } catch (_) {} }
      try { if (window.sessionStorage) stores.push(window.sessionStorage); } catch (_) {}
      return stores;
    },
    read(key, scope = 'device') {
      const stores = this._stores(scope);
      for (let index = 0; index < stores.length; index += 1) {
        let value = null;
        try { value = stores[index].getItem(key); } catch (_) { continue; }
        if (!value) continue;
        // Mirror a durable hit into the session store so pages that still read sessionStorage
        // (panorama, validation) find it in this run without their own change.
        for (const later of stores.slice(index + 1)) { try { later.setItem(key, value); } catch (_) {} }
        return String(value);
      }
      return '';
    },
    write(key, value, scope = 'device') {
      let stored = false;
      for (const store of this._stores(scope)) {
        try { store.setItem(key, String(value)); stored = true; } catch (_) {}
      }
      return stored;
    },
    remove(key, scope = 'device') {
      for (const store of this._stores(scope)) { try { store.removeItem(key); } catch (_) {} }
    },
    // "Forget this device": token, camera choice, active session, baseline and auto-save.
    forget() {
      const keys = [this.TOKEN_KEY, this.CAMERA_KEY, this.ACTIVE_SESSION_KEY, this.BASELINE_KEY, this.AUTO_SAVE_PHOTOS_KEY];
      keys.forEach(key => this.remove(key));
      return keys;
    }
  },
  // A save whose folder flush failed is kept, never reshot (#61), and must not stay buried in
  // receipts. Session state carries the rollup at the top level; a gallery record in evidence.
  deliveryWarning(record) {
    const durability = record?.delivery_durability || record?.evidence?.delivery_durability;
    if (durability?.status !== 'unconfirmed') return '';
    return durability.warning || `Warning: saving was not confirmed durable — ${durability.reason || 'no reason was recorded'}.`;
  },
  // The stored summary also carries that sentence (single shots since #61, sessions here); a page
  // that shows deliveryWarning on its own line shows the summary without it, so it reads once.
  summaryWithoutDeliveryWarning(record) {
    const summary = String(record?.summary || '');
    const warning = this.deliveryWarning(record);
    return warning && summary.includes(warning) ? summary.replace(warning, '').replace(/\s{2,}/g, ' ').trim() : summary;
  },
  whiteBalanceEvidence(shot) {
    const actual = shot.settings?.white_balance ?? 'unavailable';
    const evidence = shot.applied?.white_balance;
    const status = evidence?.verified === true ? 'White-balance control verified.' : 'White-balance control unverified.';
    return `Actual camera white balance: ${actual}. ${status}${evidence?.reason ? ' ' + evidence.reason : ''}`;
  },
  // Constraint cards: the axis, its value, a plain status word and the one-line reason show by
  // default; the technical sub-factors sit behind a "Details" disclosure (teaching is optional).
  CARD_AXIS_NAMES: {SHUTTER: 'Shutter speed', APERTURE: 'Aperture', ISO: 'ISO'},
  CARD_STATUS_WORDS: {ok: 'Good', warning: 'Check', limited: 'Limited', pending: 'Not measured yet', unverified: 'Unverified'},
  renderCards(container, cards) {
    container.replaceChildren();
    (Array.isArray(cards) ? cards : []).forEach(card => {
      const item = document.createElement('article');
      item.className = 'constraint-card';
      const statusValue = String(card.status || 'unverified');
      item.dataset.status = statusValue;
      const heading = document.createElement('strong');
      heading.textContent = `${this.CARD_AXIS_NAMES[card.axis] || card.axis || 'Decision'} · ${card.value || 'unavailable'}`;
      const reason = document.createElement('p');
      reason.textContent = card.reason || '';
      const status = document.createElement('small');
      status.textContent = this.CARD_STATUS_WORDS[statusValue] || statusValue;
      status.title = statusValue;
      item.append(heading, status, reason);
      const factors = card.sub_factors || card.subfactors || [];
      if (factors.length) {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = 'Details';
        details.append(summary);
        factors.forEach(factor => {
          const line = document.createElement('p');
          line.textContent = `${factor.name}: ${factor.readout || factor.value || ''} (${factor.status || 'unverified'})`;
          details.append(line);
        });
        item.append(details);
      }
      container.append(item);
    });
    container.hidden = !container.childElementCount;
  },
  nativeMedia() {
    if (!window.Capacitor?.isNativePlatform?.()) return null;
    const cap = window.Capacitor;
    if (cap.Plugins?.AutopilotMedia) return cap.Plugins.AutopilotMedia;
    if (cap.registerPlugin) return cap.registerPlugin('AutopilotMedia');
    return {safeArea: () => cap.nativePromise('AutopilotMedia', 'safeArea', {}), sharePhoto: options => cap.nativePromise('AutopilotMedia', 'sharePhoto', options),
      savePhoto: options => cap.nativePromise('AutopilotMedia', 'savePhoto', options)};
  },
  async downloadArtifact(apiFetch, path, filename) {
    if (!/^\/sessions\/[a-f0-9]{32}\/artifacts\/[A-Za-z0-9_.-]+$/.test(path)) {
      throw new Error('Invalid local session artifact path.');
    }
    // Stream when available; bound the fallback rather than buffering an arbitrarily large ZIP.
    const handle = window.showSaveFilePicker ? await window.showSaveFilePicker({suggestedName: filename}) : null;
    const response = await apiFetch(path);
    if (!response.ok) throw new Error('Could not read the saved session artifact.');
    if (handle) {
      const output = await handle.createWritable();
      await response.body.pipeTo(output);
      return {saved:true};
    }
    const limit = 128 * 1024 * 1024;
    if (Number(response.headers.get('Content-Length')) > limit) {
      await response.body.cancel();
      throw new Error('This sequence exceeds the browser download budget. Open it in a desktop browser with streaming file downloads.');
    }
    const reader=response.body.getReader();const chunks=[];let bytes=0;
    while(true){const {done,value}=await reader.read();if(done)break;bytes+=value.length;if(bytes>limit){await reader.cancel();throw new Error('Sequence exceeds the browser download budget.');}chunks.push(value);}
    const url=URL.createObjectURL(new Blob(chunks,{type:response.headers.get('Content-Type') || 'application/octet-stream'}));
    const link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
    return {downloaded:true};
  },
  async exportImage(apiFetch, image, title, saveToPhotos = false) {
    const filename = image.split('/').pop();
    const response = await apiFetch('/shots/' + encodeURIComponent(filename));
    if (!response.ok) throw new Error('Could not read the saved image.');
    const blob = await response.blob();
    if (blob.size > 32 * 1024 * 1024) throw new Error('Image exceeds the 32 MiB sharing limit.');
    const native = this.nativeMedia();
    if (native) {
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });
      return native[saveToPhotos ? 'savePhoto' : 'sharePhoto']({data: dataUrl.split(',')[1], filename});
    }
    const file = new File([blob], filename, {type: blob.type || 'image/jpeg'});
    if (!saveToPhotos && navigator.canShare?.({files:[file]})) {
      await navigator.share({files:[file], title});
      return {shared:true};
    }
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 30000);
    return {downloaded:true};
  },
  autoSavePhotosEnabled() {
    return this.deviceStore.read(this.deviceStore.AUTO_SAVE_PHOTOS_KEY) === '1';
  },
  setAutoSavePhotos(enabled) {
    if (enabled) this.deviceStore.write(this.deviceStore.AUTO_SAVE_PHOTOS_KEY, '1');
    else this.deviceStore.remove(this.deviceStore.AUTO_SAVE_PHOTOS_KEY);
    return this.autoSavePhotosEnabled();
  },
  shouldAttemptPhotosSave(enabled, native) {
    return Boolean(enabled) && Boolean(native);
  },
  photosSaveConfirmed(outcome) {
    return Boolean(outcome) && outcome.saved === true;
  },
  photosSaveStatusText(outcome) {
    return this.photosSaveConfirmed(outcome)
      ? 'Saved to Photos (plugin confirmed).'
      : 'Photo is on this backend. Photos save was not confirmed.';
  }
};

if (window.Capacitor?.isNativePlatform?.()) {
  const root = document.documentElement;
  // Initial portrait fallback until native safe-area measurements arrive.
  root.style.setProperty('--safe-top', '59px');
  root.style.setProperty('--safe-bottom', '34px');
  const updateInsets = () => Autopilot.nativeMedia().safeArea().then(insets => {
    if (Number(insets.top) > 0 || window.innerWidth > window.innerHeight) {
      root.style.setProperty('--safe-top', Number(insets.top) + 'px');
    }
    root.style.setProperty('--safe-bottom', Number(insets.bottom) + 'px');
  }).catch(() => {});
  updateInsets();
  window.addEventListener('resize', updateInsets);
}
