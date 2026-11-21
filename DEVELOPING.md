# Build and check Location Map

For an iPhone owner, use **Download Windows setup** on the main page. These instructions are for maintaining the kit.

## Restore the runtime

Download `Runtime.zip` from the [preview release](https://github.com/Angry512/location-map/releases/tag/v0.1.0-preview) and put it in `Support/Runtime.zip`. Keep its contents zipped. Its SHA-256 must match `Support/runtime.sha256`. The runtime is a pinned Windows x64 Python distribution with its dependencies and license notices; it is not an iPhone app.

The source archive from GitHub deliberately omits that large release asset. **START HERE.cmd requires it.**

## Build the recipient download

With Python 3.11 or newer available on a development computer, run from the repository root:

```powershell
python tools/build_release.py
```

The standard-library builder checks the pinned runtime, application archive and signed launcher, refreshes `Support/Manifest.json`, and writes and reads back every file in `dist/Location-Map-Windows.zip`. It also writes `dist/Runtime.zip` and `dist/SHA256SUMS.txt` for the GitHub release. The Windows download has one `Location Map` folder containing `START HERE.cmd`, `READ ME.txt` and `Support`.

Do not add phone records, pairing keys, logs, location seeds or device screenshots. Do not redistribute Apple's developer images. The installer fetches the pinned images and checks their hashes.

## Check on Windows without a phone

From the complete extracted recipient folder:

```powershell
& '.\START HERE.cmd' -Check
& '.\START HERE.cmd' -SelfTest
```

`-Check` verifies and extracts the runtime and checks the application and launcher without opening a phone connection. `-SelfTest` also exercises the setup screens. Neither installs anything on a phone.

The backend tests need the bundled runtime's dependencies. After the first check, the runtime lives under `%LOCALAPPDATA%\LocationMap\Runtime\<first 20 characters of runtime.sha256>`. From the repository root, using that runtime's `python.exe` as `$python`:

```powershell
& $python -B -c "import sys,unittest; sys.path[:0]=['Support/Installer','tests']; s=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in ['test_wizard_backend','test_usb_afc_wire']); r=unittest.TextTestRunner(verbosity=1).run(s); sys.exit(not r.wasSuccessful())"
& $python -B tests/test_portable_wizard.py
& $python -B Support/Sources/verify_signed_launcher.py --allow-shortcuty-metadata 'Support/Phone/Location Map.shortcut' 'Support/Sources/Location Map.unsigned.plist'
```

The portable starter test requires Windows, a complete `Support/Runtime.zip`, free disk space, and normal temporary-folder access. It uses its own copy and cache; it never opens a phone.

The application source and its own tests are in `Support/Phone/Location-Map-source.zip`. Extract that archive separately before running its tests. The website is buildless HTML/CSS/JavaScript. Serve the repository with `python -m http.server 8091`, then check the guide tabs, keyboard controls, FAQ, download links and mobile layout in a browser.

## Publication

Upload the recipient ZIP, standalone runtime and checksum file to the exact release tag linked in `README.md` and `index.html`. Keep preview releases marked **Pre-release** while the [hardware checklist](Support/Docs/Release-checklist.md) remains open. GitHub's automatically generated source ZIP is not the ready-to-run installer.

GitHub Pages serves the `main` branch at `/` with `.nojekyll`. The guide has no analytics, third-party scripts or account sign-in.

See [computer evidence](Support/Docs/COMPUTER%20CHECKS.txt) for what passed. Passing simulated-device and desktop checks does not establish a complete fresh-iPhone install, unplugged cellular operation or compatibility with future iOS updates.
