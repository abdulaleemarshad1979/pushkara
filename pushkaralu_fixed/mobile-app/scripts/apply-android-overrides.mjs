#!/usr/bin/env node
/**
 * apply-android-overrides.mjs — Patch the Android project that
 * `cap add android` just generated, in-place, with our customisations:
 *   • Permissions for geolocation, camera, vibrate, push, network
 *   • App label = "Pushkaralu"
 *   • Deep-link intent filters (custom pushkaralu:// scheme + app link)
 *   • Network security config that allows only HTTPS to the backend
 *   • Background/status bar colour
 *
 * Idempotent: safe to re-run.
 *
 * Why patches instead of a full file replacement:
 *   Capacitor's manifest template evolves between minor versions. Targeted
 *   patches survive upgrades better than wholesale overwrites. Each patch
 *   guards itself with an "already applied?" check.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT      = path.resolve(__dirname, '..');
const ANDROID   = path.resolve(ROOT, 'android');

if (!fs.existsSync(ANDROID)) {
  console.error('[android-overrides] ERROR: android/ does not exist. Run `npx cap add android` first.');
  process.exit(1);
}

function log(...args) { console.log('[android-overrides]', ...args); }

// ── 1. AndroidManifest.xml ─────────────────────────────────────────
const manifestPath = path.join(ANDROID, 'app/src/main/AndroidManifest.xml');
let manifest = fs.readFileSync(manifestPath, 'utf8');

const PERMISSIONS = [
  'android.permission.INTERNET',
  'android.permission.ACCESS_NETWORK_STATE',
  'android.permission.ACCESS_FINE_LOCATION',
  'android.permission.ACCESS_COARSE_LOCATION',
  'android.permission.ACCESS_BACKGROUND_LOCATION',
  'android.permission.CAMERA',
  'android.permission.VIBRATE',
  'android.permission.POST_NOTIFICATIONS',
  'android.permission.READ_MEDIA_IMAGES',
  'android.permission.READ_EXTERNAL_STORAGE',
  'android.permission.WAKE_LOCK',
  'android.permission.RECEIVE_BOOT_COMPLETED',
  'android.permission.FOREGROUND_SERVICE',
  'android.permission.FOREGROUND_SERVICE_LOCATION',
];

const FEATURES = [
  { name: 'android.hardware.location', required: 'false' },
  { name: 'android.hardware.location.gps', required: 'false' },
  { name: 'android.hardware.camera', required: 'false' },
  { name: 'android.hardware.camera.any', required: 'false' },
];

// Insert any missing <uses-permission> just before <application>.
const newPermLines = PERMISSIONS
  .filter((p) => !manifest.includes(`android:name="${p}"`))
  .map((p) => `    <uses-permission android:name="${p}" />`);
const newFeatureLines = FEATURES
  .filter((f) => !manifest.includes(`android:name="${f.name}"`))
  .map((f) => `    <uses-feature android:name="${f.name}" android:required="${f.required}" />`);

if (newPermLines.length || newFeatureLines.length) {
  const block = [...newPermLines, ...newFeatureLines].join('\n');
  manifest = manifest.replace(/(\s*)<application/, `\n${block}\n$1<application`);
  log(`added ${newPermLines.length} permission(s) and ${newFeatureLines.length} feature(s)`);
}

// Set network security config + usesCleartextTraffic=false on <application>.
if (!/android:networkSecurityConfig=/.test(manifest)) {
  manifest = manifest.replace(
    /<application\s/,
    '<application\n        android:networkSecurityConfig="@xml/network_security_config"\n        android:usesCleartextTraffic="false"\n        ',
  );
  log('attached network_security_config to <application>');
}

// Add deep-link intent filters to MainActivity (the launcher activity).
const deepLinkBlock = `
            <intent-filter android:autoVerify="false">
                <action android:name="android.intent.action.VIEW" />
                <category android:name="android.intent.category.DEFAULT" />
                <category android:name="android.intent.category.BROWSABLE" />
                <data android:scheme="pushkaralu" />
            </intent-filter>
            <intent-filter android:autoVerify="false">
                <action android:name="android.intent.action.VIEW" />
                <category android:name="android.intent.category.DEFAULT" />
                <category android:name="android.intent.category.BROWSABLE" />
                <data android:scheme="https" android:host="pushkara.vercel.app" android:pathPrefix="/user" />
            </intent-filter>`;

if (!manifest.includes('android:scheme="pushkaralu"')) {
  // Insert just before the closing tag of MainActivity (which ends right before the next </activity> after the launcher intent-filter).
  manifest = manifest.replace(
    /(<activity[\s\S]*?android\.intent\.category\.LAUNCHER[\s\S]*?<\/intent-filter>)/,
    `$1${deepLinkBlock}`,
  );
  log('added deep-link intent filters');
}

fs.writeFileSync(manifestPath, manifest);

// ── 2. strings.xml ─────────────────────────────────────────────────
const stringsPath = path.join(ANDROID, 'app/src/main/res/values/strings.xml');
let strings = fs.readFileSync(stringsPath, 'utf8');
strings = strings.replace(/<string name="app_name">[^<]*<\/string>/, '<string name="app_name">Pushkaralu</string>');
strings = strings.replace(/<string name="title_activity_main">[^<]*<\/string>/, '<string name="title_activity_main">Pushkaralu</string>');
strings = strings.replace(/<string name="package_name">[^<]*<\/string>/, '<string name="package_name">in.gov.ap.pushkaralu</string>');
strings = strings.replace(/<string name="custom_url_scheme">[^<]*<\/string>/, '<string name="custom_url_scheme">pushkaralu</string>');
fs.writeFileSync(stringsPath, strings);
log('updated strings.xml');

// ── 3. colors.xml — match navy/saffron theme ───────────────────────
const colorsPath = path.join(ANDROID, 'app/src/main/res/values/colors.xml');
const colorsXml = `<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="colorPrimary">#0B2E6E</color>
    <color name="colorPrimaryDark">#08204D</color>
    <color name="colorAccent">#F0A830</color>
    <color name="ic_launcher_background">#0B2E6E</color>
</resources>
`;
fs.writeFileSync(colorsPath, colorsXml);
log('wrote colors.xml');

// ── 4. network_security_config.xml ─────────────────────────────────
const nscDir = path.join(ANDROID, 'app/src/main/res/xml');
fs.mkdirSync(nscDir, { recursive: true });
const nscPath = path.join(nscDir, 'network_security_config.xml');
const nscXml = `<?xml version="1.0" encoding="utf-8"?>
<!--
  HTTPS-only network policy. Cleartext is blocked everywhere except
  10.0.2.2 (Android emulator host) for local-dev convenience.
-->
<network-security-config>
    <base-config cleartextTrafficPermitted="false">
        <trust-anchors>
            <certificates src="system" />
        </trust-anchors>
    </base-config>
    <domain-config cleartextTrafficPermitted="true">
        <domain includeSubdomains="true">10.0.2.2</domain>
        <domain includeSubdomains="true">localhost</domain>
    </domain-config>
</network-security-config>
`;
fs.writeFileSync(nscPath, nscXml);
log('wrote network_security_config.xml');

// ── 5. styles.xml — splash + status bar tint ───────────────────────
const stylesPath = path.join(ANDROID, 'app/src/main/res/values/styles.xml');
if (fs.existsSync(stylesPath)) {
  let styles = fs.readFileSync(stylesPath, 'utf8');
  // Ensure status bar colour is the navy.
  if (!styles.includes('android:statusBarColor')) {
    styles = styles.replace(
      /(<style name="AppTheme.NoActionBarLaunch"[^>]*>)/,
      `$1\n        <item name="android:statusBarColor">#08204D</item>`,
    );
    fs.writeFileSync(stylesPath, styles);
    log('patched styles.xml with status bar colour');
  }
}

// ── 6. variables.gradle — bump min/target SDK if needed ────────────
const varsPath = path.join(ANDROID, 'variables.gradle');
if (fs.existsSync(varsPath)) {
  let vars = fs.readFileSync(varsPath, 'utf8');
  // Capacitor 6 default is fine (min 22, target 34); we do nothing unless
  // explicit override needed in the future.
  fs.writeFileSync(varsPath, vars);
}

log('done');
