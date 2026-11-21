# Location Map launcher candidate

Install the free App Store releases of full a-Shell and LocalDevVPN. Open
LocalDevVPN once, tap **Connect**, and approve Apple's VPN prompt. The launcher
uses LocalDevVPN's **Enable** action and the profile created by that app.

The unsigned workflow in the installer sources contains these actions.

1. Enable LocalDevVPN.
2. Get Current Location.
3. Get Maps URL from Current Location.
4. Set the name of Maps URL to `location-map-seed.txt`, using its **Text** type.
5. Put the renamed item into a-Shell with **Overwrite** on and **Show When Run** off.
6. Open full a-Shell.
7. Get the Wi-Fi network's name. If it has no value, toggle Cellular Data, wait
   eight seconds, then toggle Cellular Data again.

The two toggles restore the original setting when the shortcut completes. If
the run is interrupted, check Cellular Data in Settings. The first run may
request location and file permissions. Once the launcher works, use **Add to
Home Screen** in Shortcuts.
