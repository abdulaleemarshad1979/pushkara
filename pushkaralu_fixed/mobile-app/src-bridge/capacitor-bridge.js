/* ─────────────────────────────────────────────────────────────────────────
 * Pushkaralu — Capacitor Native Bridge
 *
 * This script is auto-injected into user.html by scripts/sync-web.mjs.
 * It is a NO-OP when running in a regular web browser; the original PWA
 * keeps working unchanged at https://pushkara.vercel.app/user.
 *
 * What the bridge does on Android/iOS:
 *   1. Forces status bar style + colour to match the navy theme.
 *   2. Replaces navigator.geolocation with the Capacitor Geolocation plugin
 *      so Android's runtime permission flow is triggered correctly (the
 *      browser-style geolocation in WebView often silently fails).
 *   3. Replaces navigator.vibrate with Haptics for the safety-alert pulse.
 *   4. Replaces navigator.share with the native share-sheet.
 *   5. Registers for push notifications (FCM / APNs), reports the device
 *      token to the backend at /register_push, and forwards incoming
 *      messages into the existing safety-alert handler.
 *   6. Wires the Android hardware back button: closes any open modal,
 *      otherwise navigates back, otherwise exits.
 *   7. Handles `pushkaralu://` deep links and HTTPS app-links.
 *   8. Bridges Network online/offline events.
 *   9. Tags <html> with cap-native + cap-android / cap-ios so CSS can
 *      hide install-prompts that don't apply on a native app.
 * ────────────────────────────────────────────────────────────────────── */
(function initCapacitorBridge() {
  // Bail on plain web — keep the PWA behaving exactly as before.
  if (!window.Capacitor || typeof window.Capacitor.isNativePlatform !== 'function' || !window.Capacitor.isNativePlatform()) {
    return;
  }

  const C = window.Capacitor;
  const P = C.Plugins || {};
  const platform = C.getPlatform();
  const API_BASE = window.API_BASE || (window.__API_BASE) || 'https://pushkara.onrender.com';

  document.documentElement.classList.add('cap-native', 'cap-' + platform);
  window._isCapacitorApp = true;

  console.log('[CapBridge] init platform=' + platform + ' API=' + API_BASE);

  // ── 1. Status bar ──────────────────────────────────────────────────
  if (P.StatusBar) {
    P.StatusBar.setStyle({ style: 'LIGHT' }).catch(() => {});
    if (platform === 'android') {
      P.StatusBar.setBackgroundColor({ color: '#0B2E6E' }).catch(() => {});
    }
    P.StatusBar.setOverlaysWebView({ overlay: false }).catch(() => {});
  }

  // ── 2. Splash screen — hide promptly once shell is interactive ────
  if (P.SplashScreen) {
    window.addEventListener('load', () => {
      setTimeout(() => P.SplashScreen.hide().catch(() => {}), 800);
    });
  }

  // ── 3. Geolocation — replace navigator.geolocation with native ────
  // The native plugin handles Android's permission prompt correctly.
  // Web fallback is preserved in case Geolocation plugin fails to load.
  if (P.Geolocation && navigator.geolocation) {
    const _watchMap = new Map(); // web id -> native id
    let _nextWebId = 1;

    function _toWebPosition(pos) {
      return {
        coords: {
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
          accuracy: pos.coords.accuracy,
          altitude: pos.coords.altitude,
          altitudeAccuracy: pos.coords.altitudeAccuracy,
          heading: pos.coords.heading,
          speed: pos.coords.speed,
        },
        timestamp: pos.timestamp,
      };
    }

    function _toWebError(err) {
      return {
        code: 1, // PERMISSION_DENIED — closest analogue
        message: (err && err.message) || 'Geolocation failed',
        PERMISSION_DENIED: 1,
        POSITION_UNAVAILABLE: 2,
        TIMEOUT: 3,
      };
    }

    navigator.geolocation.getCurrentPosition = function (success, error, options) {
      P.Geolocation.requestPermissions().then(() => {
        return P.Geolocation.getCurrentPosition({
          enableHighAccuracy: !!(options && options.enableHighAccuracy),
          timeout: (options && options.timeout) || 10000,
          maximumAge: (options && options.maximumAge) || 0,
        });
      })
      .then((pos) => { if (success) success(_toWebPosition(pos)); })
      .catch((err) => { if (error) error(_toWebError(err)); });
    };

    navigator.geolocation.watchPosition = function (success, error, options) {
      const webId = _nextWebId++;
      P.Geolocation.requestPermissions().then(() => {
        return P.Geolocation.watchPosition({
          enableHighAccuracy: !!(options && options.enableHighAccuracy),
          timeout: (options && options.timeout) || 10000,
        }, (pos, err) => {
          if (err) { if (error) error(_toWebError(err)); return; }
          if (pos && success) success(_toWebPosition(pos));
        });
      })
      .then((nativeId) => _watchMap.set(webId, nativeId))
      .catch((err) => { if (error) error(_toWebError(err)); });
      return webId;
    };

    const _origClearWatch = navigator.geolocation.clearWatch.bind(navigator.geolocation);
    navigator.geolocation.clearWatch = function (id) {
      const native = _watchMap.get(id);
      if (native) {
        P.Geolocation.clearWatch({ id: native }).catch(() => {});
        _watchMap.delete(id);
      } else {
        try { _origClearWatch(id); } catch (e) {}
      }
    };
  }

  // ── 4. Vibration → Haptics ─────────────────────────────────────────
  if (P.Haptics) {
    navigator.vibrate = function (pattern) {
      const arr = Array.isArray(pattern) ? pattern : [pattern];
      let elapsed = 0;
      arr.forEach((dur, i) => {
        if (i % 2 === 0 && dur > 0) {
          setTimeout(() => {
            P.Haptics.impact({ style: 'HEAVY' }).catch(() => {});
          }, elapsed);
        }
        elapsed += dur;
      });
      return true;
    };
  }

  // ── 5. Share → native share sheet ──────────────────────────────────
  if (P.Share) {
    navigator.share = function (data) {
      return P.Share.share({
        title: data.title || 'Pushkaralu',
        text: data.text || '',
        url: data.url,
        dialogTitle: data.title || 'Share',
      });
    };
  }

  // ── 6. Push notifications ──────────────────────────────────────────
  if (P.PushNotifications) {
    P.PushNotifications.requestPermissions()
      .then((res) => {
        if (res.receive === 'granted') {
          return P.PushNotifications.register();
        }
      })
      .catch(() => {});

    P.PushNotifications.addListener('registration', (token) => {
      console.log('[CapBridge] push token registered');
      // Best-effort: tell the backend so it can target this device.
      // Endpoint is optional — if /register_push doesn't exist yet, the
      // POST silently fails and we fall back to in-app WS safety alerts.
      try {
        fetch(API_BASE + '/register_push', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            token: token.value,
            platform: platform,
            ts: Date.now(),
          }),
        }).catch(() => {});
      } catch (e) {}
    });

    P.PushNotifications.addListener('registrationError', (err) => {
      console.warn('[CapBridge] push registration error', err && err.error);
    });

    P.PushNotifications.addListener('pushNotificationReceived', (notif) => {
      // Foreground delivery: show a local notification (Android already
      // shows automatically when app is backgrounded; iOS does not).
      if (P.LocalNotifications && platform === 'ios') {
        P.LocalNotifications.schedule({
          notifications: [{
            title: notif.title || 'Pushkaralu Alert',
            body: notif.body || '',
            id: Math.floor(Math.random() * 100000) + 1,
            sound: 'default',
            extra: notif.data || {},
          }],
        }).catch(() => {});
      }
      // Forward to existing safety-alert handler if the page exposes one.
      if (typeof window._pushkaraSafetyAlert === 'function') {
        try {
          window._pushkaraSafetyAlert({
            title: notif.title,
            body: notif.body,
            data: notif.data || {},
            source: 'push',
          });
        } catch (e) { console.warn('[CapBridge] safety alert handler threw', e); }
      }
    });

    P.PushNotifications.addListener('pushNotificationActionPerformed', (action) => {
      const data = (action && action.notification && action.notification.data) || {};
      // If the payload includes an in-app route hint like {url: 'sos'},
      // jump to that section.
      if (data.url) {
        try { location.hash = String(data.url).replace(/^#?\/?/, ''); } catch (e) {}
      }
    });
  }

  // ── 7. Hardware back button (Android) ──────────────────────────────
  if (P.App) {
    P.App.addListener('backButton', ({ canGoBack }) => {
      // Close any visible modal first.
      const openModal = document.querySelector('.modal.show, .modal[style*="display: flex"], .modal[style*="display:flex"], [data-modal-open="1"]');
      if (openModal) {
        openModal.classList.remove('show');
        openModal.style.display = 'none';
        openModal.removeAttribute('data-modal-open');
        return;
      }
      if (location.hash && location.hash !== '#') {
        location.hash = '';
        return;
      }
      if (canGoBack && history.length > 1) {
        history.back();
      } else {
        P.App.exitApp().catch(() => {});
      }
    });

    // ── 8. Deep links ────────────────────────────────────────────────
    // pushkaralu://sos          → /user#sos
    // https://pushkara.vercel.app/user#ghats → /user#ghats
    P.App.addListener('appUrlOpen', (event) => {
      try {
        const url = new URL(event.url);
        // Custom scheme pushkaralu://sos
        if (url.protocol === 'pushkaralu:') {
          const target = (url.host || url.pathname || '').replace(/^\/+/, '');
          if (target) location.hash = target;
          return;
        }
        // App link https://pushkara.vercel.app/user#sos
        if (url.hash) {
          location.hash = url.hash.replace(/^#/, '');
        }
      } catch (e) {}
    });
  }

  // ── 9. Network state → online/offline events ───────────────────────
  if (P.Network) {
    P.Network.addListener('networkStatusChange', (status) => {
      window.dispatchEvent(new Event(status.connected ? 'online' : 'offline'));
    });
  }
})();
