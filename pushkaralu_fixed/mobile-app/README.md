# Pushkaralu — Native Android & iOS App

This folder wraps the Godavari Pushkaralu pilgrim portal (`../dashboards/user.html`)
into a native Android `.apk`/`.aab` and an iOS `.ipa` using
[Capacitor 6](https://capacitorjs.com/).

The website remains the single source of truth. `npm run sync:web`
copies the latest `dashboards/*` into `www/` and injects a small native
bridge so geolocation, vibration, share, push, deep-links, the back
button and the splash/status bar all use real platform APIs in the app.

---

## Quick path: install the APK without setting anything up

1. Push to any branch on GitHub.
2. The **Android APK** workflow runs automatically (`.github/workflows/android-apk.yml`).
3. Open the workflow run → **Artifacts** → download `pushkaralu-apk-NN`.
4. Unzip → transfer the `.apk` to your phone → tap to install (Android may
   warn about installing from an unknown source, allow it for the file
   manager you used).

You can also trigger a release build manually:
*Actions ▸ Android APK ▸ Run workflow ▸ build_type = release.*

---

## Local build (developer machine)

### Prerequisites

* Node 20+
* Java 21 (Temurin recommended)
* Android Studio with SDK 34 + build-tools 34.0.0
* `ANDROID_HOME` env var pointing at the SDK
* (iOS only) macOS + Xcode 15+

### One-time setup

```bash
cd pushkaralu_fixed/mobile-app
npm install
npm run icons:generate          # PNG icons + splash from SVG sources
npm run sync:web                # Copy dashboards/ into www/ and inject bridge
npx cap add android             # Generate android/ project
npm run android:overrides       # Apply our manifest, theme, deep-links
npx capacitor-assets generate --android  # Fan out icon.png to mipmaps
npx cap sync android
```

### Build a debug APK

```bash
npm run android:build:debug
# → android/app/build/outputs/apk/debug/app-debug.apk
```

### Build a release APK + AAB (signed)

Place a keystore at `android/release.keystore` and configure
`signingConfigs.release` in `android/app/build.gradle`, or set
the four `PUSHKARA_*` env vars used by the CI workflow, then:

```bash
npm run android:build:release
# → android/app/build/outputs/apk/release/app-release.apk
# → android/app/build/outputs/bundle/release/app-release.aab
```

### iOS

```bash
npx cap add ios
npm run ios:sync
npx cap open ios       # opens Xcode; build with Cmd+R
```

---

## Architecture

```
mobile-app/
├── package.json              Capacitor 6 + plugin deps
├── capacitor.config.ts       App ID, scheme, allow-list, plugin config
├── src-bridge/
│   └── capacitor-bridge.js   Native plugin glue, no-op on web
├── scripts/
│   ├── sync-web.mjs                Copy dashboards/ → www/ + inject bridge
│   ├── generate-icons.mjs          SVG → PNG via sharp
│   └── apply-android-overrides.mjs Patch AndroidManifest, strings, theme
├── resources/
│   ├── icon-source.svg
│   └── splash-source.svg
└── www/                       (generated, gitignored)
└── android/                   (generated, gitignored)
└── ios/                       (generated, gitignored)
```

### Why the website lives in `dashboards/` not `www/`

`www/` is treated as a build output. Editing the website only happens in
`pushkaralu_fixed/dashboards/`. CI re-runs `sync:web` before every build,
and so should you. This keeps the PWA at `pushkara.vercel.app/user`
identical to the bundled native shell.

### Why `androidScheme: 'https'`

Capacitor's default Android scheme is `capacitor://localhost`, which
blocks Service Workers. Forcing `https` lets the existing `sw.js` install
inside the WebView, so offline caching of map tiles, API responses and
the app shell works exactly as on the web.

### Native APIs the app uses

| Feature in `user.html`     | Web API                | Native via bridge                    |
|----------------------------|------------------------|--------------------------------------|
| SOS GPS lookup             | `navigator.geolocation`| `@capacitor/geolocation`             |
| Safety-alert pulse         | `navigator.vibrate`    | `@capacitor/haptics`                 |
| Trip-plan share            | `navigator.share`      | `@capacitor/share`                   |
| Live safety alerts         | WebSocket `/ws/volunteer` (kept) + push | `@capacitor/push-notifications` |
| Photo upload (issue/SOS)   | `<input type=file>`    | `@capacitor/camera` (native picker)  |
| Hardware back / exit       | n/a                    | `@capacitor/app`                     |
| Deep links `pushkaralu://` | n/a                    | `@capacitor/app` listener            |
| Online/offline             | `online`/`offline`     | `@capacitor/network`                 |
| Splash + status-bar tint   | n/a                    | `@capacitor/splash-screen` + `status-bar` |
| `tel:` links               | (browser)              | Android handles natively             |

### Backend

The app talks to the existing production backend with no changes:

* REST: `https://pushkara.onrender.com`
* WebSocket: `wss://pushkara.onrender.com/ws/volunteer`

If `/register_push` exists on the backend, the app POSTs the FCM/APNs
token there on first launch so server-side targeting of safety alerts
becomes possible. If the endpoint doesn't exist yet, the POST silently
fails — alerts continue to arrive over the existing WebSocket.

---

## Updating the app

1. Edit `pushkaralu_fixed/dashboards/user.html` (or any other dashboard
   file) — that's still where the website lives.
2. Push. CI builds a new APK with the latest portal bundled in.
3. Distribute the new APK, or for Play Store builds, upload the AAB.

For an OTA-like flow you can instead point Capacitor at a remote URL by
setting `server.url` in `capacitor.config.ts`; existing changes will
then propagate without re-installing. The default config bundles the
website to keep first launch instant and offline-capable.

---

## Play Store / App Store checklist

- [ ] Generate a release keystore: `keytool -genkey -v -keystore release.keystore -alias pushkaralu -keyalg RSA -keysize 2048 -validity 10000`
- [ ] Configure signing (env vars or `signingConfigs.release` in `android/app/build.gradle`)
- [ ] Bump `versionCode` and `versionName` in `android/app/build.gradle`
- [ ] Provide store listing assets (1024×500 feature graphic, 512×512 icon, screenshots)
- [ ] Add a privacy policy URL covering geolocation, push notifications, photo upload
- [ ] iOS: configure bundle ID, signing, push capability in Xcode
