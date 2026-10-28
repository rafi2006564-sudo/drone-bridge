# Setup

## PC (Phase 1, no hardware)

See the quick start in the README: Python 3.11+, `pip install -e ".[test]"`,
`config.yaml` copied from `config.example.yaml`.

## Phase 0: DJI developer account, SDK, RC access

Check each step against DJI's current MSDK V5 documentation; this list is a
checklist, not a substitute.

1. **Developer account and app key.** Create a DJI developer account and an
   MSDK app. Bind the app key to the Android package name the RC Bridge app
   will use (record it here: `____________`). The first SDK registration on
   the RC needs internet.
2. **Pin the MSDK version.** Pick the newest MSDK V5 release whose release
   notes list the aircraft and RC firmware from `docs/hardware.md` (5.12.0 or
   later for Matrice 4E/4T with RC Plus 2).
3. **Android Studio** with the SDK and build tools that the sample project
   for that MSDK version requires.
4. **adb access to the RC Plus 2.** Enable developer options and USB
   debugging on the RC, connect it, and check that `adb devices` lists it.
   Record the exact steps here once done.
5. **Build DJI's sample app** from `github.com/dji-sdk/Mobile-SDK-Android-V5`
   at the tag for the pinned version, with your app key, and install it:

   ```sh
   ./gradlew assembleDebug
   adb install -r path/to/app-debug.apk
   ```

6. Close DJI Pilot 2 (see `docs/hardware.md`), start the sample app, and
   power on the aircraft. **Phase 0 is done when the sample app shows the
   aircraft connected.**
7. **MediaMTX** (for Phase 3): download the prebuilt binary for this PC from
   its GitHub releases.

## Network

- Put the PC and the RC on the same Wi-Fi network (a local router, or a
  hotspot on the PC). It does not need internet access.
- Give the RC a DHCP reservation and put its IP in `config.yaml` → `rc.host`.
- The RC app listens on `rc.port` (default 8765). The PC must be able to
  reach it: check the PC firewall and that the router has no client isolation.
- Map tiles need internet on the PC. Without it, the map is blank, but the
  aircraft, home and track markers still work. For offline maps, point
  `map.tile_url` at a local tile server.
