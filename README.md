# Location Map

Set up Location Map on an iPhone using a Windows computer. The setup window guides you through the phone prompts.

## Download and start

**[Download Windows setup (88 MB)](https://github.com/Angry512/location-map/releases/download/v0.1.0-preview/Location-Map-Windows.zip)**

1. Open your computer's **Downloads** folder. Right-click **Location-Map-Windows.zip**, choose **Extract All**, then **Extract**.
2. Open the extracted **Location Map** folder. Connect **one iPhone** by USB and unlock it.
3. Double-click **START HERE.cmd**. Follow the setup window and leave the phone connected until it finishes.

**Use the download above.** GitHub's green **Code** button downloads developer source, which is missing the Windows runtime.

You need Windows 10/11 on an Intel or AMD 64-bit PC, internet access, a cable that transfers data, and enough phone storage for the full a-Shell app. The installer accepts iOS 26.4–27.x; that version check does not prove compatibility with every phone. Macs and Windows ARM computers are not supported by this installer.

## When the phone asks

Setup waits while you do these steps. Enter passcodes and Apple Account details **on the phone**, never in chat.

| Prompt | What to do |
| --- | --- |
| Trust this computer | Tap **Trust**, then enter the iPhone passcode. |
| Developer Mode | Open **Settings → Privacy & Security → Developer Mode**. Turn it on, restart, unlock, then confirm **Turn On**. If the setting is missing, keep setup open with the phone connected. |
| Download two apps | Use the codes in setup to install the full **a-Shell** and **LocalDevVPN**. Approve each download. Wait until the App Store says **Open**. |
| Allow the VPN | Open **LocalDevVPN → Connect → Allow**. Enter the iPhone passcode if asked. |
| Add Location Map | When setup says the files are ready, open **Files → Browse → On My iPhone → a-Shell**, then the shortcut at the exact location shown in setup. Tap **Add Shortcut**; if Files shows a preview, use **Share → Shortcuts**. Run **Location Map** and allow its requested access. Choose **Always Allow** when sharing location with a-Shell. |
| Add the Home Screen icon | In Shortcuts, open Location Map's menu beside its name and choose **Add to Home Screen → Add**. Follow setup's placement instructions. |
| Hide the helpers | Hold each helper icon and choose **Remove App → Remove from Home Screen**. **Do not choose Delete App.** Both helpers must stay installed. |

The app links are also here: [full a-Shell](https://apps.apple.com/us/app/a-shell/id1473805438) · [LocalDevVPN](https://apps.apple.com/us/app/localdevvpn/id6755608044).

## Use it

Open **Location Map** on the iPhone, choose a place, then tap **Simulate**. Tap **Stop** to restore the real location. Check both operations in another location display on the same phone before relying on it.

The shortcut connects the local VPN. Away from Wi-Fi, let its brief cellular pause finish; if you interrupt it, check that Cellular Data is back on. Keep a-Shell and LocalDevVPN installed.

There is **no weekly sideload signing renewal** with this App Store based setup. Future iOS or helper-app changes can still require an update or recovery.

## If you get stuck

- **The computer cannot find the phone:** unlock it, tap Trust, and try a data-capable USB cable. Use the **Apple Devices** button in setup if Windows needs Apple's USB software.
- **Setup was interrupted:** reconnect and unlock the **same iPhone**, then reopen **START HERE.cmd**. Matching files can be reused.
- **Setup will not open:** extract the ZIP first. Keep **START HERE.cmd**, **READ ME.txt** and the **Support** folder together. Check the Windows requirements above.

## What has been checked

**Preview:** 80 application tests, 29 installer tests, one AFC transfer protocol test and 10 real Windows startup/recovery checks passed. The published download was retrieved from GitHub, every packaged file was checked, and its Windows starter passed.

A complete fresh-phone installation, unplugged cellular use and reboot flow for this exact release remain unverified. [Computer check details](Support/Docs/COMPUTER%20CHECKS.txt) · [Remaining phone checks](Support/Docs/Release-checklist.md).

## For maintainers

[Build and verification instructions](DEVELOPING.md) · [Release files](https://github.com/Angry512/location-map/releases/tag/v0.1.0-preview).

The repository includes the installer, signed launcher and complete application source in `Support/Phone/Location-Map-source.zip`. The Windows runtime is a separate release asset and is included in the recipient download. Pairing is generated for each recipient; no owner's phone pairing, location or account credentials are included.

[GPL-3.0](LICENSE). Dependencies keep their own notices. Built on work from [StikDebug](https://github.com/StikDebug/StikDebug), [pymobiledevice3](https://github.com/doronz88/pymobiledevice3), [a-Shell](https://github.com/holzschu/a-shell) and [LocalDevVPN](https://github.com/jkcoxson/LocalDevVPN). No affiliation with Apple or those projects.
