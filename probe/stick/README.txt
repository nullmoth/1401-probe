1401 PROBE - hardware scanner · NullMoth Systems · nullmothsystems.com
=======================================================================

This USB stick starts your computer into a small, offline scanner. It reads what hardware your
computer has and writes one report file to this stick. Then it turns the computer off.

WHAT IT DOES
  - Reads: processor, motherboard and BIOS/UEFI settings, graphics cards (and their firmware),
    Wi-Fi and Bluetooth chips, sound, USB ports, drives (model and size only), ACPI tables.
  - Writes: ONE file, to THIS stick only:  REPORTS\<your-computer>.zip  (+ a readable .txt summary)

WHAT IT NEVER DOES
  - It never connects to the internet. The system on this stick starts with no network, Wi-Fi or
    Bluetooth drivers, so it cannot. At the start it asks whether to include Wi-Fi, Bluetooth and
    Ethernet (press Y within 30 seconds; anything else, or nothing, means no). If you say yes, it loads
    only your chips' drivers for about a minute, switches every radio off the moment it starts, and
    removes them again. Nothing is ever configured or sent. The report records that it checked.
  - It never reads, mounts or changes your drives or Windows. Before scanning, it locks every drive
    except this stick read-only.
  - It never changes BIOS/UEFI settings.
  - It removes serial numbers, UUIDs, MAC addresses and your Windows product key from the report.
    Open the .zip and look: everything in it is plain text or raw firmware dumps.

HOW TO RUN IT
  1. Turn Secure Boot OFF in your BIOS/UEFI setup (the scanner is not signed by Microsoft).
     You turn it back on afterwards (step 4). If Windows uses BitLocker, pause it in the 1401 Probe app
     or have your recovery key handy.
  2. Plug this stick in and restart. Press your computer's boot-menu key and pick the USB stick
     (the one that says UEFI). Common keys:
        ASUS  F8      MSI  F11      Gigabyte  F12     ASRock  F11
        Dell  F12     HP   F9       Lenovo    F12     Acer    F12
     Or from Windows: Settings > System > Recovery > Advanced startup > Use a device.
  3. Choose "1401 Probe - scan this computer". It asks the Wi-Fi/Bluetooth question above, then
     scans by itself. It takes about 5 to 20 minutes.
     The last steps test the graphics card and watch each driver start: the screen may flicker or go
     black for up to 10 minutes. Leave it running. Those steps are the ones we need most.
     If nothing happens for 15 minutes, hold the power button. Your report is already saved.
     Start the stick again: it skips the step that froze. If it freezes again, pick "safe scan".
  4. When it says DONE, it turns the computer off. PUT YOUR SETTINGS BACK: turn the computer on, go
     into BIOS setup (usually Del or F2) and set Secure Boot back to Enabled. Your boot order was
     not changed.
  5. Start Windows, open 1401 Probe, click "I already scanned - find my report", and drag BOTH files
     from the REPORTS folder (the .zip and the .txt) onto nullmothsystems.com.
     On Linux: run  python3 1401-probe-linux.py --report  and send both files the same way.

Nothing on this stick phones home. Uploading the report is something only you do.
