# Hardware release checklist

This is the current guided setup preview. Computer checks passed; the rows below are still required before calling it a fully verified hardware release. The current entry point is **START HERE.cmd**. Older CLI flags such as `-StageTestDevice`, `-CheckKit` and `-CheckPreview` are not commands for this guided package.

## Before using a test iPhone

Run `START HERE.cmd -Check` and `START HERE.cmd -SelfTest` from the complete extracted Windows download. Verify `Support/Manifest.json` against the kit files. Follow the root `DEVELOPING.md` in the source repository for source and signature checks. Keep the GitHub release marked Pre-release until the hardware flow below is recorded against the exact published hashes.

## Checks still needed on the exact release

| Check | Evidence needed |
| --- | --- |
| Clean Windows PC | Start the extracted download on an x64 Windows PC with Apple's USB drivers. Confirm the double-click starter and every instruction without developer tools. |
| Fresh phone | Begin with no Location Map files or pairing on the recipient. Approve Trust, enable Developer Mode, install both helpers, approve the VPN and import the signed launcher. |
| Transfer recovery | Disconnect during transfer. Reconnect the same phone and rerun; unrelated a-Shell files must survive. |
| First launch | Start with LocalDevVPN disconnected. The launcher connects it, the native map shows a fresh blue dot, and the Windows wizard confirms the current launch receipt. |
| Home Screen | Add Location Map, confirm first-page placement, then remove only the helper icons from the Home Screen. Both helpers must remain installed. |
| Wi-Fi without USB | Unplug the cable. Choose a point, Simulate, and Stop; check each result in a separate location display on the same phone. |
| Cellular without USB | Turn Wi-Fi off in Settings, unplug USB and cold launch on cellular. Repeat Simulate and Stop twice. Confirm Cellular Data returns after the launcher's temporary pause. |
| Interrupted cellular launch | Interrupt the pause. Check cellular recovery and another successful launch, including weak signal and Wi-Fi enabled but unassociated. |
| Force-close and restart | Close the runtime and reopen. Stale simulation status must clear or remain unconfirmed until verified. Restart the phone and repeat both operations. |
| Developer image recovery | Recover a missing developer image from the phone without USB; Apple personalization can require internet. |

Record the date, Windows version, phone model/iOS build, a-Shell and LocalDevVPN versions, exact download SHA-256 and observations for every completed row. Do not convert simulated-device results into hardware passes.

## Current signed launcher

The launcher is signed for import by Anyone. Its SHA-256 is `6baf4f0b183b7a1f9a491255cffbab1d96e67fcd02792e7f213f127768b21676`. The verified Apple leaf certificate is valid through 1 December 2027. The verifier compares all 13 actions with `Support/Sources/Location Map.unsigned.plist`, allowing only the documented signing-service metadata rewrite. Signature verification on Windows is not a substitute for import on a recipient's phone.

The launcher uses a-Shell's shared-container Put File action and LocalDevVPN's native Enable action. It does not contain a saved VPN identifier or a user's device record. The first-launch receipt requires map presentation, a fresh location seed, and a developer-service channel; setup never sends Simulate or Stop.

The developer-image fetcher uses pinned hashes from the [upstream developer images](https://github.com/doronz88/DeveloperDiskImage/tree/f0a9fb8ef81df9217fb2d2bcb4e3351af903408b/PersonalizedImages/Xcode_iOS_DDI_Cryptex). Those Apple image files are downloaded during setup and are not redistributed in this repository.
